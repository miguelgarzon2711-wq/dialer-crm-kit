# 02 — Instalación paso a paso

Runbook desde un servidor vacío hasta el dialer funcionando. El orden importa:
cada paso depende del anterior.

Antes de empezar, leé [`../CLAUDE.md`](../CLAUDE.md) completo. Este documento asume
que ya conocés las reglas duras que hay ahí.

Convención: todo lo que aparezca como `<ASI>` lo reemplazás con datos reales.

---

## Paso 0 — Datos que necesitás del cliente

No arranques sin esto. Si el cliente no puede responder algo, anotalo como pendiente
y seguí, pero no lo adivines.

**Del negocio:**

| Pregunta | Para qué la necesitás |
|---|---|
| ¿De dónde vienen los leads? | define cómo se conecta el CRM |
| ¿Cuántos leads por día y cuántos vendedores? | dimensiona el servidor y el pool de números |
| ¿Qué busca lograr una llamada? | define las disposiciones |
| ¿Un lead que ya habló con un vendedor se queda con él? | define el sticky (casi siempre sí) |
| ¿Qué resultados posibles tiene una llamada? | esa lista son las disposiciones exactas |
| ¿En qué horario y zona horaria se llama? | configura el sistema y los reportes |
| ¿Qué datos necesita el vendedor de cada llamada? | define las instrucciones de la transcripción |

**Accesos:**

- Servidor con acceso `root` y su llave SSH.
- Cuenta del CRM con permiso para crear un token de API.
- Cuenta del proveedor SIP con saldo.
- Un dominio o subdominio apuntando al servidor.
- Opcional: clave de OpenAI para la transcripción.

---

## Paso 1 — Servidor y OMniLeads

Referencia de tamaño: **4 vCPU / 8 GB de RAM** para unos 20 agentes simultáneos.
Servidor dedicado, no compartido.

Antes de instalar nada, fijá la zona horaria del sistema:

```bash
timedatectl set-timezone <TU_ZONA>        # ej: America/New_York
date                                       # verificá
```

Instalá OMniLeads siguiendo su documentación oficial. Cuando termine, verificá que:

- Entrás a la consola web por el dominio, con HTTPS.
- Podés crear un agente y ese agente puede iniciar sesión.
- El teléfono del navegador se conecta (no dice "SIP Proxy no responde").

Anotá los nombres reales de los contenedores, porque los scripts del kit los usan:

```bash
docker ps --format '{{.Names}}'
```

Si no coinciden con `prod-env-django-app-1`, `prod-env-acd-1`,
`prod-env-postgresql-1` y `prod-env-redis-1`, hacé un reemplazo global en
`server/scripts/` antes de continuar.

**No sigas hasta que esto funcione.** Todo lo demás se apoya acá.

---

## Paso 2 — Cerrar el servidor

Hacelo ahora, no al final. Un dialer con puertos abiertos recibe intentos de
registro automáticos desde el primer día.

Leé los dos scripts antes de correrlos y ajustá el nombre de tu interfaz de red:

```bash
ip -o link show | awk -F': ' '{print $2}'    # ver el nombre de la interfaz
```

```bash
bash server/scripts/firewall.sh
bash server/scripts/purge_sip_intruders.sh
```

Dejalo como servicio para que sobreviva a los reinicios:

```bash
cat > /etc/systemd/system/dialer-firewall.service <<'EOF'
[Unit]
Description=Cierre de puertos internos del dialer
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/bin/bash /opt/dialer-kit/scripts/firewall.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now dialer-firewall
```

**Verificalo desde otra máquina**, no desde el servidor:

```bash
nmap -Pn -p 5038,5060,5160,6379,9000 <IP_DEL_SERVIDOR>
```

Los puertos internos tienen que aparecer cerrados o filtrados. Desde el propio
servidor siempre se ven bien: no sirve como prueba.

---

## Paso 3 — Telefonía

Leé [`05-TELEFONIA.md`](05-TELEFONIA.md) completo antes de este paso.

1. Contratá el proveedor SIP y comprá los números del área que vas a llamar.
2. Configurá el trunk en OMniLeads. **El nombre del endpoint tiene que ser el
   nombre del trunk**, y no lleva usuario de origen: es el error más común.
3. Creá la ruta de salida. El generador de OMniLeads no arma bien este contexto:
   usá `server/asterisk/extensions_outbound_route.conf` como base.
4. Configurá el identificador de llamada del negocio con tu proveedor. Suele ser
   gratis y sube bastante la tasa de contestación.

Cargá los números en el pool (el Paso 4 crea la tabla):

```sql
INSERT INTO numbers_pool (did) VALUES
  ('<NUMERO_1>'), ('<NUMERO_2>')
ON CONFLICT DO NOTHING;
```

**Terminá con una llamada real:** que suene, que se escuche en los dos sentidos,
y que al colgar quede el registro guardado. Si no hiciste esa llamada, este paso
no está hecho.

---

## Paso 4 — Base de datos

```bash
docker exec -i prod-env-postgresql-1 psql -U omnileads -d omnileads \
  < server/sql/schema.sql
```

Verificá:

```bash
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads \
  -c "\dt dialer_*" -c "\df pick_did"
```

Tenés que ver tres tablas `dialer_*`, las tres de números, y la función `pick_did`.

Probá el rotador antes de seguir:

```bash
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads \
  -c "SELECT pick_did('5551234567')"
```

Debe devolver uno de tus números. Si devuelve vacío, el pool está vacío.

---

## Paso 5 — Credenciales

```bash
mkdir -p /opt/dialer-kit
cp .env.example /root/.env_dialer
chmod 600 /root/.env_dialer
```

Completá cada variable. Las que no uses, dejalas vacías: el sistema funciona sin
transcripción y sin las opcionales.

El token con el que el CRM le habla al dialer se genera así:

```bash
docker exec prod-env-django-app-1 python3 \
  /opt/omnileads/ominicontacto/manage.py shell -c \
  "from rest_framework.authtoken.models import Token; \
   from django.contrib.auth import get_user_model; \
   u=get_user_model().objects.get(username='<USUARIO_API>'); \
   print(Token.objects.get_or_create(user=u)[0].key)"
```

Ese valor va en `DIALER_API_TOKEN` y en el encabezado `Authorization` de los
webhooks del CRM. **Es lo único que impide que un tercero te inyecte leads falsos.**

---

## Paso 6 — Módulos de la integración

Copiá los cuatro módulos al contenedor de Django:

```bash
mkdir -p /opt/dialer-kit/patches
cp server/django/*.py /opt/dialer-kit/patches/

for f in crm_webhooks crm_dispositions lead_ownership agent_api; do
  python3 -c "import ast; ast.parse(open('/opt/dialer-kit/patches/$f.py').read())" || exit 1
  docker cp /opt/dialer-kit/patches/$f.py \
    prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/views/$f.py
done
```

Agregá las rutas: copiá el archivo de urls desde el contenedor, pegale el bloque de
`server/django/urls_patch.py` al final, validá y devolvelo.

```bash
docker cp prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/urls.py \
          /opt/dialer-kit/patches/urls.py
# ... editar y pegar el bloque ...
python3 -c "import ast; ast.parse(open('/opt/dialer-kit/patches/urls.py').read())"
docker cp /opt/dialer-kit/patches/urls.py \
          prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/urls.py
docker restart prod-env-django-app-1
```

Esperá que levante y verificá:

```bash
for i in $(seq 1 30); do
  sleep 3
  c=$(curl -s -o /dev/null -w "%{http_code}" https://<TU_DOMINIO>/accounts/login/)
  [ "$c" = "200" ] && { echo "arriba en $((i*3))s"; break; }
done

docker exec prod-env-django-app-1 python3 \
  /opt/omnileads/ominicontacto/manage.py shell -c \
  "from api_app.views import crm_webhooks, crm_dispositions, lead_ownership, agent_api; print('OK')"
```

Si el import falla, revisá el error y corregí **antes** de seguir.

### Probar el webhook con un lead falso

```bash
curl -X POST https://<TU_DOMINIO>/api/v1/crm/lead_action/ \
  -H "Authorization: Bearer <DIALER_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"action":"add","tipo":"Nuevo Lead","telefono":"5551234567","nombre":"Prueba"}'
```

La estructura exacta del cuerpo está en [`03-INTEGRACION-CRM.md`](03-INTEGRACION-CRM.md).
Con `DIALER_LIVE=0` la petición responde bien pero no inyecta: es lo correcto
mientras probás.

---

## Paso 7 — Parches sobre OMniLeads

Leé [`../server/patches/README.md`](../server/patches/README.md) y aplicalos **de a
uno**, verificando después de cada uno. Si aplicás cinco juntos y algo se rompe, no
vas a saber cuál fue.

Los que no podés saltear:

- No auto-finalizar campañas (si no, la campaña se cierra sola a media mañana).
- Solo dígitos al marcar (si no, las llamadas mueren en silencio).
- Enmascarado del número, en los cinco lugares.

---

## Paso 8 — Tareas programadas

```bash
mkdir -p /opt/dialer-kit/scripts
cp server/scripts/* /opt/dialer-kit/scripts/
chmod +x /opt/dialer-kit/scripts/*.sh
```

La tabla completa con qué hace cada uno está en [`07-OPERACION.md`](07-OPERACION.md).
Punto de partida:

```cron
@reboot sleep 90 && bash /opt/dialer-kit/scripts/restore_patches.sh
*/10 * * * * bash /opt/dialer-kit/scripts/normalize_phones.sh >> /var/log/normalize_phones.log 2>&1
*   * * * * bash /opt/dialer-kit/scripts/sync_agent_pause.sh  >> /var/log/sync_agent_pause.log 2>&1
*/5 * * * * bash /opt/dialer-kit/scripts/auto_release_leads.sh
*   * * * * bash /opt/dialer-kit/scripts/sync_lead_owner.sh
*   * * * * bash /opt/dialer-kit/scripts/check_did_picker.sh
```

Agregá la transcripción solo si configuraste la clave de OpenAI:

```cron
* * * * * bash /opt/dialer-kit/scripts/run_transcribe.sh >> /var/log/transcripciones.log 2>&1
```

El selector de números va como servicio:

```bash
cat > /etc/systemd/system/did-picker.service <<'EOF'
[Unit]
Description=Selector de caller ID del dialer
After=docker.service
Requires=docker.service

[Service]
ExecStart=/usr/bin/python3 /opt/dialer-kit/scripts/did_picker.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now did-picker
curl -s "http://127.0.0.1:8055/pick?tel=5551234567"    # debe devolver un número
```

Dejá pasar unos minutos y revisá que los logs se estén escribiendo. Un cron que
falla en silencio es peor que no tenerlo.

---

## Paso 9 — Persistencia

Adaptá `restore_patches.sh` a tu instalación: rutas, nombres de contenedor y un
bloque por cada parche que hayas aplicado.

**Después probalo de verdad:**

```bash
reboot
# esperar, entrar de nuevo y verificar:
tail -30 /var/log/restore_patches.log
docker exec prod-env-django-app-1 python3 \
  /opt/omnileads/ominicontacto/manage.py shell -c \
  "from api_app.views import crm_webhooks; print('OK')"
```

Un kit que no sobrevive un reinicio no está terminado.

---

## Paso 10 — Verificación final

Criterios objetivos. Si alguno falla, no está listo.

| # | Prueba | Aprobado si |
|---|---|---|
| 1 | Llamada saliente real | suena, se escucha en los dos sentidos, queda el registro |
| 2 | Llamada entrante real | llega al vendedor correcto y se escucha |
| 3 | Webhook con lead de prueba | el lead aparece en la cola en segundos |
| 4 | Ciclo completo | el vendedor lo toma, llama, dispone, y el resultado aparece en el CRM |
| 5 | Sticky | un lead dispuesto como "contestó" vuelve siempre al mismo vendedor |
| 6 | Dos vendedores a la vez | ninguno recibe el mismo lead |
| 7 | Reinicio | todo vuelve solo, sin intervención |
| 8 | Puertos | escaneo desde afuera: internos cerrados |
| 9 | Credenciales | `grep -rn` en el código: sin resultados |
| 10 | Crons | los logs se están escribiendo |

Recién cuando pasen los diez, abrí la llave: `DIALER_LIVE=1`.

---

## Problemas frecuentes durante la instalación

| Síntoma | Causa probable | Solución |
|---|---|---|
| La llamada no hace nada, sin error | el número tiene `+`, espacios o guiones | normalizá a solo dígitos; verificá que el cron esté corriendo |
| El teléfono del navegador dice "SIP Proxy no responde" | certificados del proxy SIP mal configurados | revisá la configuración TLS del proxy y recargá |
| Error 502 en la conexión del teléfono | ruteo de nginx modificado | restaurá la configuración original |
| Django no levanta después de un parche | error de sintaxis en el archivo copiado | revisá los registros del contenedor y restaurá la copia previa |
| El lead se entrega y desaparece | un error de SQL abortó la transacción | envolvé las consultas opcionales en savepoints |
| La campaña se cierra sola | falta el parche de no auto-finalizar | aplicalo |
| Las llamadas salen sin identificador | el pool está vacío o el selector no responde | cargá números y verificá el servicio |
| El vendedor no recibe leads | quedó pausado | revisá el log del sincronizador de pausas |
| Nada vuelve al CRM | falta el import del motor de disposiciones en las rutas | agregá la última línea de `urls_patch.py` |
| Un parche desaparece solo | no está en `restore_patches.sh` | agregalo |
