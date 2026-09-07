# 07 — Operación

Cómo se mantiene vivo el dialer día a día: qué corre solo, cómo sobreviven
los parches a un reinicio, qué reglas no se pueden romper al operar, qué
vigilan los watchdogs, y con qué comandos concretos se diagnostica un
problema. Todo lo de abajo sale del código real en `server/scripts/`. Donde
un valor (frecuencia, nombre de contenedor, interfaz de red) depende de la
instalación puntual, se marca como tal — no se inventa un número que el
código no dice.

---

## 1. Tabla de tareas programadas

| Script | Tipo | Frecuencia (según el propio código) | Qué hace | Log |
|---|---|---|---|---|
| `normalize_phones.sh` | cron | Cada 10 min (comentario del script) + en el pre-chequeo de lanzamiento | Limpia teléfonos con formato no marcable en contactos EN COLA; reporta los que no se pudieron arreglar | Solo imprime por stdout — **no escribe archivo propio**; quien lo agende debe redirigir la salida |
| `sync_agent_pause.py` | cron | Cada minuto (comentario del script) | Reconcilia pausa real de Asterisk vs. la que debería tener el agente (lead en pantalla, app dormida) en ambas direcciones | Solo imprime por stdout — redirigir en el cron |
| `auto_release_leads.sh` | cron | No especificada en el script (solo el umbral: contactos ENTREGADO hace más de 10 min) — definir cadencia según necesidad, ej. cada 1–5 min | Libera contactos ENTREGADO abandonados: sin dueño vuelven al pool con prioridad 1 y se desasignan en el CRM; con dueño vuelven al dueño | `/var/log/auto_release_leads.log` |
| `sync_lead_owner.sh` | cron | No especificada en el script — definir según necesidad | Reasigna al pool los leads de contactos que ya tienen dueño; cierra Recordatorios de Cita vencidos (>12h sin llamar) | `/var/log/sync_lead_owner.log` |
| `check_did_picker.sh` | cron | No especificada en el script — definir según necesidad (servicio crítico: conviene frecuente) | Prueba `GET /pick?tel=123`; si no responde en 3s, reinicia el servicio systemd `did-picker` | `/var/log/did_picker_watchdog.log` (solo si reinició) |
| `redis_watchdog.sh` | **daemon** (`while true; sleep 15`), no cron | Loop propio de 15s | Verifica llaves críticas de Redis (salientes e inbound) y las regenera si faltan (ver sección 4) | `/var/log/redis_watchdog.log` |
| `watch_django_restart.sh` | **daemon** (`docker events` en streaming), no cron | Reacciona a eventos, no a intervalo | Detecta el arranque del contenedor de Django y re-loguea agentes en las colas de Asterisk 45s después | `/var/log/django_restart_watcher.log` |
| `restore_patches.sh` | `@reboot` (según `CLAUDE.md` del kit: `@reboot sleep 90 && bash restore_patches.sh`) | Al arrancar el servidor (también se puede correr a mano) | Reaplica todos los parches que se hayan perdido (ver sección 2) | `/var/log/restore_patches.log` (la mayoría de bloques; alguno no loguea) |
| `sync_agents_pjsip.sh` | Se corre desde `restore_patches.sh` (al final, siempre) | Cada vez que corre `restore_patches.sh` | Agrega (append-only) el endpoint PJSIP de cualquier agente activo que no lo tenga en `oml_pjsip_agents.conf` | Imprime a stdout; `restore_patches.sh` lo redirige a su propio log |
| `firewall.sh` | Servicio systemd al boot (según `CLAUDE.md`), no cron | Al arrancar / bajo demanda | Cierra puertos internos y bloquea REGISTER/5160 externos (ver `05-TELEFONIA.md` sección 7) | Solo imprime por stdout |
| `purge_sip_intruders.sh` | Manual / bajo demanda | Ad-hoc (endurecimiento inicial o ante sospecha) | Borra registros SIP que no vengan de Kamailio (127.0.0.1) | Solo imprime por stdout |
| `create_agent_inbound.sh` | Manual | "Después de crear agentes nuevos" (comentario del script) | Crea/asegura la campaña inbound personal de cada agente activo | `/root/create_agent_inbound.out`, `/root/agent_inbound.json` + stdout |

> Los scripts que solo "imprimen por stdout" no tienen ruta de log escrita en
> el propio código — la persistencia del log depende de cómo se agenden
> (`>> /var/log/algo.log` en la línea de cron, o journal de systemd si son
> servicios).

---

## 2. Persistencia de parches (`restore_patches.sh`)

### El problema que resuelve

Los contenedores de OMniLeads se reinician (reboot del servidor, `docker
restart`, actualizaciones) y **cualquier cambio hecho con `docker exec`
directo sobre el filesystem del contenedor se pierde** en ese momento. El
síntoma es desconcertante: algo que "ya estaba arreglado" vuelve a fallar
solo, sin que nadie haya tocado nada a propósito.

### El patrón idempotente

Cada bloque del script sigue la misma forma:

```bash
# 1) MARCA: ¿el parche ya está aplicado?
if ! docker exec <contenedor> grep -q '<texto único del parche>' <ruta dentro del contenedor> 2>/dev/null; then
  # 2) FALTA -> copiar la versión buena desde el host y recargar lo mínimo necesario
  docker cp /opt/dialer-kit/patches/<archivo>.patched <contenedor>:<ruta destino>
  docker restart <contenedor>            # o el reload puntual: asterisk -rx 'dialplan reload',
                                          # 'module reload app_queue.so', nginx -s reload, pjsip reload
  echo "$(date): <descripcion> restaurado" >> /var/log/restore_patches.log
fi
```

La marca puede ser un `grep` de un string distintivo dentro de un archivo, o
directamente `test -f` sobre un archivo que en el stock de OMniLeads no
existe. Si la marca YA está, el bloque no hace nada — por eso es seguro
correr el script entero en cada reinicio, o incluso a mano en cualquier
momento, sin efectos secundarios.

Dos bloques rompen el patrón a propósito, por diseño:

- **`sync_agents_pjsip.sh`** — se llama siempre, sin marca, porque es
  *append-only*: solo agrega los endpoints PJSIP que falten, nunca reescribe
  ni borra, así que correrlo de más no puede romper nada.
- **`inbound_redis_sync.py`** — se llama si existe `/root/agent_inbound.json`
  (no hay marca dentro de un contenedor porque lo que asegura son claves de
  Redis, no un archivo).

### Cómo agregar un parche nuevo — paso a paso

1. Hacé el cambio en una copia del archivo y **dejá esa copia en el host**,
   en `/opt/dialer-kit/patches/` (nunca edites solo dentro del contenedor).
2. Metele al archivo un texto único que no exista en la versión original de
   OMniLeads (ej. un comentario `# PATCH MI-COSA`) — es tu marca.
3. Validá sintaxis ANTES de tocar el contenedor:
   ```bash
   python3 -c "import ast; ast.parse(open('archivo.py').read())" && echo OK
   bash -n script.sh && echo OK
   ```
4. Agregá un bloque nuevo al final de `restore_patches.sh` con la forma de
   arriba: chequeo de marca → `docker cp` → reload mínimo → log.
5. Corré el script dos veces seguidas: la primera debe aplicar y loguear; la
   segunda no debe hacer nada (así confirmás que quedó idempotente).
6. Probá que sobrevive de verdad: `docker restart <contenedor>` (o reiniciar
   todo el servidor) y confirmar que `restore_patches.sh` lo vuelve a aplicar
   solo.

---

## 3. Reglas duras de operación

| Regla | Por qué |
|---|---|
| **Nunca `docker compose up`** | OMniLeads levanta contenedores con prefijo de hash; `compose up` los mata y recrea, y se pierde todo lo de adentro |
| **Siempre operaciones individuales**: `docker exec`, `docker cp`, `docker restart <nombre>`, `docker start/stop` | Cambian solo lo necesario sin tocar el resto de los contenedores |
| **Validar sintaxis antes de copiar al contenedor** (`ast.parse` para Python, `bash -n` para shell) | Un archivo Python con error de sintaxis tumba Django entero |
| **Guardar copia local de todo lo que se edite dentro de un contenedor** (en `/opt/dialer-kit/patches/`) | Si no hay copia afuera, el próximo reinicio borra el cambio sin aviso |
| **Nunca tocar el ruteo de nginx** (proxy/reglas) — solo cache headers | Un cambio de ruteo deja el webphone sin señal (`/ws` 502) y nadie puede llamar |

---

## 4. Vigilancia (watchdogs)

| Watchdog | Qué revisa | Qué síntoma tapa |
|---|---|---|
| `redis_watchdog.sh` (loop 15s) | Existencia de `OML:OUTR:1`, `OML:TRUNK:2`, `OML:CAMP:9` (familias salientes) y `OML:INR:<DID>` (familia entrante) en Redis. Si faltan, corre desde Django `RutaSalienteFamily().regenerar_families()`, `TrunkFamily().regenerar_families()`, `RegenerarAsteriskFamilysOML().regenerar_asterisk()` (salientes) o `SincronizadorDeConfiguracionTelefonicaEnAsterisk().sincronizar_en_asterisk()` (entrante) | Un `FLUSHALL`/reinicio de Redis que borra las tablas de ruteo en memoria que usa el dialplan en tiempo real — sin esto, las llamadas fallan con "sin ruta" aunque la configuración en la base de datos esté perfecta |
| `check_did_picker.sh` | `GET /pick?tel=123` responde en menos de 3s | El proceso Python de `did_picker.py` colgado o caído — rompería en silencio la rotación de Caller ID y el ruteo de entrantes, porque el dialplan depende de que ese CURL conteste |
| `watch_django_restart.sh` | Reinicios del contenedor `prod-env-django-app-1` (vía `docker events`) | Django reiniciando saca a los agentes de las colas de Asterisk (la membresía vive en sesiones AMI manejadas por Django) — el agente ve la consola "conectada" pero no le entran llamadas. **Nota:** la lista de IDs de agente a re-loguear está *hardcodeada* en el script (`[2, 4]` en esta copia) — hay que actualizarla si se agregan agentes nuevos, o reemplazarla por una consulta dinámica a la tabla de agentes activos |
| `sync_agent_pause.py` (cada minuto) | Compara pausa real en Asterisk vs. estado esperado: lead en pantalla, marca propia en `OML:DIALER:PAUSA_GESTION`, llamada en curso (`OML:AGENT:*`), heartbeat de la app (`OML:DIALER:SESION:*`) | Agentes que quedan pausados de más (bloqueando su cola) o despausados de más (les entra una llamada mientras tienen un lead en pantalla); apps de celular que dejaron de latir 3+ minutos y siguen "conectadas" sin que nadie las atienda |

---

## 5. Diagnóstico

### "¿Por qué no entró la llamada?" (entrante)

```bash
# ¿el servicio did-picker está vivo y responde?
curl -s --max-time 3 'http://10.22.22.1:8055/pick?tel=123'
systemctl status did-picker

# ¿a qué DID/cola está ruteando esta llamada entrante?
curl 'http://10.22.22.1:8055/inbound_campana?from=<numero10digitos>'

# ¿hay registros SIP externos (no de Kamailio) que puedan estar interfiriendo?
docker exec prod-env-acd-1 asterisk -rx "database show registrar/contact"
docker exec prod-env-acd-1 asterisk -rx "pjsip show contacts"

# ¿el lead ya tiene dueño? (misma consulta que hace did_picker.py)
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads -tAc \
  "SELECT r.telefono FROM dialer_lead_owner o \
   JOIN ominicontacto_app_contacto c ON c.id=o.contacto_id \
   JOIN configuracion_telefonia_app_rutaentrante r ON r.telefono='900001'||lpad(o.agente_id::text,2,'0') \
   WHERE c.telefono='<numero10digitos>' ORDER BY o.since DESC LIMIT 1;"

# ¿quedó registrada como llamada perdida?
tail -n 50 /var/log/missed_calls.log

# recargar dialplan si se acaba de tocar un contexto
docker exec prod-env-acd-1 asterisk -rx "dialplan reload"
```

### "¿Por qué el agente no recibe leads?"

```bash
# ¿está pausado en Asterisk? ¿hace cuánto?
docker exec prod-env-acd-1 asterisk -rx "queue show"
# buscar la línea "... (paused was N secs ago)" del agente

# ¿la pausa la puso el propio sistema? (si está acá, sync_agent_pause.py la va a levantar sola)
docker exec prod-env-redis-1 redis-cli SMEMBERS OML:DIALER:PAUSA_GESTION

# ¿tiene un lead entregado sin calificar trabado en pantalla?
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads -tAc \
  "SELECT * FROM ominicontacto_app_agenteencontacto WHERE agente_id=<id> AND estado IN (1,3);"

# ¿la app dejó de latir? (heartbeat del celular)
docker exec prod-env-redis-1 redis-cli HGETALL OML:DIALER:SESION:<agente_id>
# revisar 'ultimo_visto' y 'dispositivo' (si dice 'app_dormida', se reengancha solo al volver a latir)

# ¿tiene endpoint PJSIP creado en Asterisk? (puede faltar tras recrear el contenedor)
docker exec prod-env-acd-1 asterisk -rx "pjsip show endpoint <extension>"
# si falta:
bash /root/sync_agents_pjsip.sh

# correr el reconciliador a mano y ver qué haría
python3 server/scripts/sync_agent_pause.py
```

### "¿Por qué no suena el audio?"

```bash
# error típico "SIP Proxy no responde" en el webphone -> certificados TLS de Kamailio
docker exec prod-env-kamailio-webrtc-1 grep 'certs/cert.pem' /etc/kamailio/tls.cfg

# /ws con 502 -> config TLS del proxy en nginx hacia Kamailio
docker exec prod-env-nginx-1 grep proxy_ssl_protocols /etc/nginx/conf.d/environment/oml_env.conf

# si cualquiera de los dos falta, restore_patches.sh los reaplica:
bash /opt/dialer-kit/scripts/restore_patches.sh

# probar audio sin depender de un lead real (contextos de prueba del kit)
# 5550000001 contesta y hace eco de tu propia voz -> valida audio ida y vuelta
# channel originate Local/s@test-trunk application Wait 30      (saliente real por el trunk)
# channel originate Local/s@test-inbound application Wait 40    (entrante, ruteo a cola)
```

> Para audio que "se corta" en llamadas que sí conectan (RTP/NAT), este kit
> no trae un script propio de diagnóstico — se resuelve con las herramientas
> estándar de Asterisk (ej. `rtp set debug on` desde la consola de Asterisk),
> que no son parte de este kit sino de Asterisk en general.

---

## 6. Logs

| Archivo | Lo escribe | Qué buscar ahí |
|---|---|---|
| `/var/log/missed_calls.log` | `did_picker.py` (`missed()`) | Una línea por llamada entrante no contestada reenviada a `/api/v1/dialer/missed_call/`, con el resultado (`ok` o `ERROR <detalle>`) |
| `/var/log/restore_patches.log` | `restore_patches.sh` | Qué parche se reaplicó y cuándo — si está vacío en un reinicio, es buena señal (nada se había perdido) |
| `/var/log/auto_release_leads.log` | `auto_release_leads.sh` | Cada lead liberado por abandono (`aec_id`, `contacto_id`, nombre, si tenía dueño o no) y el resultado de la desasignación en el CRM |
| `/var/log/sync_lead_owner.log` | `sync_lead_owner.sh` | Leads reasignados a su dueño desde el pool, y recordatorios de cita cerrados por vencidos |
| `/var/log/redis_watchdog.log` | `redis_watchdog.sh` | Alertas de llaves de Redis faltantes y si la regeneración automática funcionó |
| `/var/log/did_picker_watchdog.log` | `check_did_picker.sh` | Cada vez que tuvo que reiniciar el servicio `did-picker` porque no respondía |
| `/var/log/django_restart_watcher.log` | `watch_django_restart.sh` | Reinicios detectados del contenedor de Django y si el re-login de agentes en las colas salió bien |
| `/root/create_agent_inbound.out`, `/root/agent_inbound.json` | `create_agent_inbound.sh` | Salida cruda y JSON estructurado de la última corrida de creación de campañas inbound personales |

Los scripts que solo imprimen por stdout (`normalize_phones.sh`,
`sync_agent_pause.py`, `firewall.sh`, `purge_sip_intruders.sh`) no generan
archivo de log por sí mismos — si se agendan por cron hay que redirigir la
salida a mano (`>> /var/log/<nombre>.log 2>&1`).
