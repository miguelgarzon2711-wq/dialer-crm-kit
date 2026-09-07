# 01 — Arquitectura

> Lector objetivo: un agente de IA que va a instalar o mantener este kit sin haber
> visto el código antes. Todo lo que sigue sale de leer `CLAUDE.md`, `README.md`,
> `server/django/*.py`, `server/scripts/*`, `server/asterisk/*` y `server/sql/schema.sql`
> de este mismo repositorio. Donde el código no alcanza a confirmar algo, se dice
> explícitamente — no se completa con supuestos. Los documentos `docs/03` a `docs/09`
> profundizan cada tema; este es el mapa general.

---

## 1. Panorama

### 1.1 Qué es OMniLeads

OMniLeads es un contact center de código abierto. No es parte de este kit — el kit
se instala **encima** de una instalación de OMniLeads ya funcionando. Sus piezas,
todas corriendo como contenedores Docker:

- **Asterisk**: el motor de telefonía. Marca, recibe llamadas, ejecuta el plan de
  marcación (dialplan), maneja las colas de agentes y el trunk SIP hacia el
  proveedor.
- **Django**: la aplicación web y la API REST. Consola del agente, lógica de
  negocio (campañas, contactos, disposiciones, permisos), y el punto donde AMI
  (Asterisk Manager Interface) conecta a Django con Asterisk.
- **PostgreSQL**: toda la base de datos, tanto la de OMniLeads como las tablas
  propias del kit.
- **Redis**: estado en tiempo real — qué agente está en qué estado, en qué llamada,
  las "familias" que Asterisk consulta para rutear (trunk, rutas salientes,
  campañas entrantes).
- **Kamailio**: proxy SIP sobre WebSocket/TLS. Es el punto de entrada para el
  webphone del navegador y para la app móvil — ninguno de los dos habla
  directamente con Asterisk.
- **nginx**: proxy reverso. TLS, la consola web, los estáticos, y el camino
  `/ws` hacia Kamailio.

### 1.2 Qué le agrega este kit

OMniLeads por sí solo es un dialer genérico. Este kit lo conecta a un CRM
(probado con GoHighLevel) y le agrega las reglas de negocio que un equipo de
ventas necesita:

| Módulo | Para qué |
|---|---|
| Webhooks CRM → dialer | los leads entran solos, ordenados por prioridad |
| Motor de disposiciones dialer → CRM | tags, notas y dueño del lead vuelven solos al CRM |
| Dueño permanente del lead ("sticky") | un lead que ya conversó con un vendedor es siempre suyo |
| Rotación de caller ID | los números salientes no se queman como spam |
| Transcripción con IA | notas automáticas en el CRM y auditoría de buzones de voz |
| API REST para app móvil | un vendedor sin computador puede trabajar desde el celular |
| Persistencia de parches | los cambios sobreviven a un reinicio de los contenedores |

### 1.3 Diagrama — integración con el CRM y el proveedor SIP

```
                                   CRM (ej. GoHighLevel)
                                    |            ^
                          webhooks  |            | API REST
                          (lead entra/           | (tags, notas,
                           sale de cola)          |  asignacion de dueno)
                                    v            |
                        +--------------------------------+
                        |   Django (api_app + modulos     |
                        |   propios de este kit)          |
                        +--------------------------------+
                            |                    |
                            | lee/escribe        | lee/escribe
                            v                    v
                    +---------------+     +---------------+
                    |  PostgreSQL   |     |     Redis      |
                    |  (contactos,  |     |  (estado en    |
                    |   colas,      |     |   vivo: agente,|
                    |   dueno,      |     |   llamada,     |
                    |   auditoria)  |     |   rutas)       |
                    +---------------+     +---------------+
                            ^
                            | AMI (ordenes: marcar, pausar, etc.)
                            v
                    +----------------------+
                    |   Asterisk (acd)     |
                    |   dialplan, colas    |
                    +----------------------+
                            |
                            | trunk SIP (PJSIP)
                            v
                    Proveedor SIP  --------->  Operador humano (telefono)
```

### 1.4 Diagrama — clientes del agente (navegador y app móvil)

```
   Navegador (webphone)          App movil (vendedor)
          |                             |
          |  HTTPS (consola, API)       |  HTTPS (API propia del kit,
          |  WSS  (audio SIP)           |   agent_api.py) + WSS (audio SIP)
          v                             v
   +---------------------------------------------+
   |                nginx (TLS)                   |
   +---------------------------------------------+
       |  /api/, consola web       |  /ws
       v                           v
   +----------------+      +------------------------+
   |    Django      |      |  Kamailio (proxy SIP   |
   +----------------+      |  sobre WebSocket/TLS)  |
                            +------------------------+
                                       |
                                       | SIP/RTP interno
                                       v
                            +----------------------+
                            |   Asterisk (acd)      |
                            +----------------------+
```

El navegador y la app **nunca hablan SIP directo con Asterisk**: siempre pasan por
Kamailio. Las credenciales SIP que usa cada cliente son efímeras (las genera
`KamailioService`, ver `server/django/agent_api.py`, endpoint `AppSessionView`).

---

## 2. Los contenedores

Nombres reales encontrados en los scripts del kit. Llevan el prefijo del proyecto
docker-compose (`prod-env-` en esta instalación de referencia) — **ese prefijo
puede ser distinto en cada servidor**. Antes de asumir un nombre, confirmalo con:

```bash
docker ps --format '{{.Names}}'
```

| Contenedor (nombre visto en el kit) | Qué es | Rol en este stack |
|---|---|---|
| `prod-env-django-app-1` | Django (uwsgi) | Consola web, API nativa de OMniLeads y los endpoints que agrega el kit (webhooks CRM, disposiciones, sticky, API de la app móvil). Corre con varios workers uwsgi (parche `processes=4`, ver `restore_patches.sh`). |
| `prod-env-acd-1` | Asterisk | El motor de llamadas: dialplan, colas (`queues.conf`), trunk SIP, servidor AMI. "acd" = Automatic Call Distribution. |
| `prod-env-postgresql-1` | PostgreSQL | Toda la base de datos: tablas nativas de OMniLeads + las 3 tablas propias del kit (`server/sql/schema.sql`). |
| `prod-env-redis-1` | Redis | Estado en vivo: agentes conectados, colas, sesión única, pausa por "lead en pantalla", caché de notas del CRM. |
| `prod-env-kamailio-webrtc-1` | Kamailio | Proxy SIP sobre WebSocket/TLS para el webphone del navegador y la app móvil. |
| `prod-env-nginx-1` | nginx | Reverse proxy: TLS, `/ws` hacia Kamailio, estáticos, consola web. El nombre real se busca dinámicamente en `restore_patches.sh` (`docker ps | grep nginx`) porque no siempre es fijo. |
| `prod-env-dialer-acd-dialplan-1` | Servicio propio de OMniLeads (`/app/app.py`) | Puente entre Asterisk y el resto del sistema durante el ciclo de post-llamada (ACW); el kit le aplica un parche (`_delayed_decr`) para una condición de carrera de 5 segundos. |
| (sin nombre fijo en el kit) | MinIO | Almacenamiento de las grabaciones de llamada (`.mp3`, bucket `omnileads`). Se referencia por endpoint HTTP (puerto 9000/9001) y credenciales en `server/scripts/transcribe_calls.py`, no por nombre de contenedor. |

Procesos que **no** corren dentro de Docker, sino en el host:

| Proceso | Qué es | Dónde se define |
|---|---|---|
| `did-picker` (servicio systemd) | Servidor HTTP propio (`server/scripts/did_picker.py`), escucha en `10.22.22.1:8055` | Rotador de caller ID + resolución de campaña entrante + registro de llamada perdida |
| Firewall (`iptables`/`DOCKER-USER`) | `server/scripts/firewall.sh`, pensado como servicio systemd | Cierra los puertos internos de Docker y del host al exterior |
| `redis_watchdog.sh` | Daemon en bucle (`while true; sleep 15`) | Repara claves críticas de Redis si se pierden |
| `watch_django_restart.sh` | Daemon que escucha `docker events` | Re-loguea agentes en las colas cuando Django reinicia |
| Crons varios | `server/scripts/*.sh`, `*.py` | Ver `docs/07-OPERACION.md` para la tabla completa con frecuencias |

---

## 3. Recorrido de un lead, de punta a punta

| Paso | Qué pasa | Componente | Archivo del kit |
|---|---|---|---|
| 1 | El lead entra o cambia de estado en el CRM (formulario, WhatsApp, llamada). Una automatización del CRM decide inyectarlo al dialer. | CRM (externo al kit) | — |
| 2 | El CRM dispara `POST /api/v1/ghl/lead_action/?campana=<clave>` con `action=inject`. El dialer valida, chequea el interruptor `DIALER_LIVE`, resuelve la campaña destino, calcula la prioridad (`TIPO_ORDEN`) y respeta el dueño permanente si ya existe. | Django | `server/django/crm_webhooks.py` (`CRMLeadActionView`, `_inject_in_preview`) |
| 3 | El lead queda como fila `AgenteEnContacto` en estado INICIAL, en la cola de la campaña que corresponda. Si tiene dueño, la fila ya sale asignada a ese agente. | PostgreSQL (nativo OMniLeads) + `dialer_lead_owner` (kit) | `server/django/lead_ownership.py` (`get_owner`) |
| 4 | El vendedor pide un lead ("Obtener contacto", o el botón único de la app). El motor nativo de OMniLeads entrega el de mayor prioridad; el kit ya lo asigna al vendedor mapeado en el CRM y lo pausa en las colas entrantes mientras lo tiene en pantalla. | Django + Redis | `server/django/agent_api.py` (`AppLeadView`), `server/django/crm_dispositions.py` (`on_aec_saved`, `pausa_gestion`) |
| 5 | El vendedor llama (consola o app). El servidor marca primero al teléfono del agente y después al lead, normalizado a solo dígitos, por la ruta saliente hacia el trunk SIP. El caller ID lo elige el rotador. El lead ve el número del agente enmascarado en su pantalla. | Django (AMI) → Asterisk → trunk SIP | `server/django/agent_api.py` (`AppLlamarView`), `server/asterisk/extensions_outbound_route.conf`, `server/scripts/did_picker.py` |
| 6 | Al colgar, el agente entra en ACW (post-llamada) y no puede marcar de nuevo hasta calificar. | Django + Redis | `server/django/agent_api.py` (`AppLlamadaTerminadaView`), consola web nativa |
| 7 | El vendedor guarda la disposición. Un signal dispara la vuelta al CRM: tags, nota simple (solo para "no contestó"), y el ciclo de dueño (asignar si contestó, liberar si no y no tiene dueño todavía). Si la disposición prueba que hubo conversación, se fija el dueño permanente. | Django (signal `post_save`) | `server/django/crm_dispositions.py` (`on_calificacion_saved`, `_procesar`), `server/django/lead_ownership.py` (`set_owner`) |
| 8 | El resultado ya vive en el CRM (tags, nota, `assignedTo`) y en las tablas del kit (`dialer_lead_owner`, `dialer_agent_crm_map`). Asterisk registra el evento de la llamada. | CRM + PostgreSQL | `reportes_app_llamadalog` (nativo OMniLeads) |
| 9 | Un cron revisa las llamadas recientes, busca la grabación en MinIO, la transcribe con Whisper, detecta si fue un buzón de voz, redacta una nota con GPT y la publica en el CRM (y en la tabla propia, para que la app la muestre al instante). | Cron (host) + OpenAI | `server/scripts/transcribe_calls.py`, `server/scripts/run_transcribe.sh` |
| 10 | Si el lead quedó "entregado" sin que lo llamaran en 10 minutos, vuelve a la cola: al dueño si lo tiene, o al pool con prioridad alta y desasignado del CRM si no. | Cron (host) | `server/scripts/auto_release_leads.sh` |
| 11 | Si el lead llama antes de ser re-entregado (entrante), Asterisk consulta si tiene dueño: si sí, timbra solo en su cola personal; si no, en el grupo. Si nadie contesta, se re-inyecta como "Llamada Perdida" con la prioridad más alta. | Asterisk + Django | `server/asterisk/extensions_override.conf` (`[from-pstn]`, `[dialer-missed]`), `server/django/crm_webhooks.py` (`CRMMissedCallView`) |

Ver `docs/03-INTEGRACION-CRM.md` para el detalle campo por campo del webhook, y
`docs/05-TELEFONIA.md` para el detalle de la llamada saliente/entrante.

---

## 4. Los módulos del kit

| Archivo | Qué hace | De qué depende |
|---|---|---|
| `server/django/crm_webhooks.py` | Endpoint de entrada `POST /api/v1/ghl/lead_action/`: inyecta/remueve leads, calcula prioridad (`TIPO_ORDEN`), maneja recordatorio de cita y llamada perdida (`CRMMissedCallView`). | `lead_ownership.py` (dueño), tabla `dialer_agent_crm_map`, modelos nativos `Contacto`/`AgenteEnContacto`/`CalificacionCliente` |
| `server/django/crm_dispositions.py` | Motor de salida dialer → CRM: tags, nota simple, ciclo de asignación de dueño en el CRM, pausa por "lead en pantalla" (`pausa_gestion`), gate opcional "solo contestó si conectó" (`GATE_ACTIVO`, apagado por defecto). | `lead_ownership.py`, `/opt/omnileads/.env_dialer`, tabla `dialer_agent_crm_map`, `reportes_app.LlamadaLog` |
| `server/django/lead_ownership.py` | Dueño permanente del lead: fija el primer dueño, sincroniza el pool hacia el dueño, libera leads de agentes inactivos. | Solo PostgreSQL, tabla `dialer_lead_owner` |
| `server/django/agent_api.py` | API REST completa para la app móvil: sesión SIP efímera, obtener lead, llamar, liberar, opciones de calificación, calificar, notas del CRM (con caché), estado del agente, sesión única navegador/celular, latido de la app. | `crm_dispositions.py` (pausa/gate), `KamailioService` nativo, Redis, `dialer_call_audit` |
| `server/scripts/did_picker.py` | Servidor HTTP interno (`10.22.22.1:8055`): elige caller ID saliente (`/pick`), resuelve la cola entrante por dueño (`/inbound_campana`), registra llamada perdida (`/missed`). | Función SQL `pick_did()` **no incluida en este kit** (ver `docs/05-TELEFONIA.md`), tabla `dialer_lead_owner` |
| `server/scripts/auto_release_leads.sh` | Libera leads "entregados" sin llamar en más de 10 minutos; desasigna del CRM si no tienen dueño. | PostgreSQL, `dialer_lead_owner`, API de GHL |
| `server/scripts/sync_lead_owner.sh` | Red de seguridad: reasigna al dueño los leads sueltos en el pool; cierra recordatorios de cita vencidos (+12h). | PostgreSQL, `dialer_lead_owner` |
| `server/scripts/sync_agent_pause.py` | Reconcilia la pausa real en Asterisk contra la deseada (lead en pantalla, app dormida, llamada en curso) en ambos sentidos. | Asterisk (CLI), Redis, PostgreSQL |
| `server/scripts/normalize_phones.sh` | Red de seguridad: limpia teléfonos con formato no marcable en contactos que están en cola. | PostgreSQL |
| `server/scripts/check_did_picker.sh` | Watchdog de `did-picker`: si no responde en 3s, reinicia el servicio systemd. | `did_picker.py`, systemd |
| `server/scripts/redis_watchdog.sh` | Daemon que repara claves críticas de Redis si un `FLUSHALL`/reinicio las borra. | Redis, Django (management shell) |
| `server/scripts/watch_django_restart.sh` | Daemon que re-loguea agentes en las colas de Asterisk cuando Django reinicia. | `docker events`, Django |
| `server/scripts/restore_patches.sh` | Reaplica todos los parches de esta instalación al arrancar (o a mano). Es el corazón de la persistencia. | `/opt/dialer-kit/patches/*` (no incluidos en este repo; los arma cada instalación) |
| `server/scripts/sync_agents_pjsip.sh` | Append-only: agrega el endpoint PJSIP de cualquier agente activo que falte en Asterisk. | PostgreSQL, Asterisk |
| `server/scripts/create_agent_inbound.py` / `.sh` | Crea, por cada agente, su campaña entrante personal (clon de una campaña base) con DID virtual propio. | Modelos nativos de OMniLeads, Redis |
| `server/scripts/inbound_redis_sync.py` | Asegura en Redis las familias de las campañas entrantes personales. | Redis, `/root/agent_inbound.json` |
| `server/scripts/backfill_prepare.py` / `backfill_inject.py` | Carga inicial de leads históricos al lanzar (lee Supabase/CRM, escribe una lista; la inyecta dentro de Django). Ver `docs/09-PROBLEMAS-CONOCIDOS.md` — el paso intermedio de copiar el archivo es manual. | Supabase (opcional), API de GHL, `crm_webhooks.py` |
| `server/scripts/call_report.py` | Reporte de llamadas bajo demanda: tasa de contacto, buzones vs. humanos, alertas. | PostgreSQL |
| `server/scripts/transcribe_calls.py` / `run_transcribe.sh` | Transcribe llamadas nuevas con Whisper, detecta buzón de voz, redacta nota con GPT y la publica en el CRM. | MinIO, OpenAI, API de GHL, tabla `dialer_call_audit` |
| `server/scripts/firewall.sh` / `purge_sip_intruders.sh` | Cierre de seguridad: puertos internos, bloqueo de REGISTER externo, limpieza de registros SIP intrusos. | `iptables`, Asterisk (CLI) |
| `server/sql/schema.sql` | Crea las 3 tablas propias del kit (idempotente). | Solo PostgreSQL |
| `server/asterisk/extensions_outbound_route.conf` | Ruta saliente hacia el trunk (escrita a mano porque el generador nativo de OMniLeads no la arma bien). | Trunk PJSIP configurado en OMniLeads |
| `server/asterisk/extensions_override.conf` | Entrante: extrae el DID real del header `To:` (workaround para el proveedor probado), detecta llamada perdida, aplica la máscara de número hacia el agente, y trae contextos de prueba (`test-trunk`, `test-inbound`, `test-leads`). | `did_picker.py` |
| `server/asterisk/queues.conf.example` | Ejemplo de las colas de las campañas Preview e Inbound de una instalación de referencia — **es un ejemplo, no un archivo a copiar tal cual**. | — |

---

## 5. Dónde vive el estado

### 5.1 PostgreSQL

**Tablas propias del kit** (`server/sql/schema.sql`):

| Tabla | Para qué |
|---|---|
| `dialer_lead_owner` | Dueño permanente del lead: `contacto_id` → `agente_id`, desde cuándo, por qué disposición. |
| `dialer_agent_crm_map` | Mapeo agente del dialer ↔ usuario del CRM. Columna del vendedor: ver nota abajo. |
| `dialer_call_audit` | Una fila por llamada transcrita: duración, si es buzón, idioma, alerta, nota redactada por la IA. |

> **Nota:** el código (`crm_dispositions.py`, `crm_webhooks.py`, `backfill_inject.py`)
> lee y escribe la columna `ghl_user_id` de `dialer_agent_crm_map`, pero
> `schema.sql` crea esa columna con el nombre `crm_user_id`. Es una
> inconsistencia real del kit — ver `docs/02-INSTALACION.md`, sección de
> problemas frecuentes, antes de cargar el mapeo de vendedores.

**Tablas nativas de OMniLeads** que el kit lee o escribe directamente (fuera del
ORM, con SQL crudo en varios scripts):

| Tabla | Para qué la usa el kit |
|---|---|
| `ominicontacto_app_contacto` | El lead: teléfono, `id_externo` (id del CRM), datos (nombre/tipo en JSON). |
| `ominicontacto_app_agenteencontacto` (AEC) | La fila "este contacto está en esta cola, en este estado, para este agente". Es el corazón de la asignación. |
| `ominicontacto_app_calificacioncliente` / `_historicalcalificacioncliente` | La disposición guardada y su historial (django-simple-history). |
| `ominicontacto_app_opcioncalificacion` | Catálogo de disposiciones por campaña. |
| `ominicontacto_app_agenteprofile` / `_user` | El agente: extensión SIP, estado activo/inactivo, grupo. |
| `reportes_app_llamadalog` | Eventos de cada llamada (DIAL, ANSWER, COMPLETEAGENT, etc.) — insumo de `call_report.py` y `transcribe_calls.py`. |
| `queue_table` / `queue_member_table` | Colas de Asterisk y sus miembros. |
| `configuracion_telefonia_app_rutaentrante` / `_destinoentrante` | DIDs y a qué campaña rutean. |

### 5.2 Redis

| Clave (patrón) | Quién la usa | Para qué |
|---|---|---|
| `OML:AGENT:<agente_id>` | Nativo OMniLeads | Estado en vivo del agente: `STATUS`, `CONTACT_NUMBER`, `PAUSE_ID`. El kit la lee para saber si está en llamada o en ACW. |
| `OML:CAMP:<id>`, `OML:CAMPAIGN-AGENTS:<id>`, `OML:INR:<did>`, `OML:OUTR:<id>`, `OML:TRUNK:<id>` | Nativo OMniLeads (regeneradas por sus propios servicios) | "Familias" que el dialplan de Asterisk consulta en tiempo real para saber cómo rutear. `redis_watchdog.sh` las vigila y las regenera si faltan. |
| `OML:DIALER:PAUSA_GESTION` (set) | Kit | Agentes pausados en las colas porque tienen un lead en pantalla. |
| `OML:DIALER:SESION:<agente_id>` (hash) | Kit | Sesión única navegador/app: qué dispositivo está activo, último latido (heartbeat) de la app móvil. |
| `OML:DIALER:NOTAS:<contacto_id>` | Kit | Caché de 60s de las notas del CRM para la app móvil. |

### 5.3 Archivos

| Archivo | Para qué |
|---|---|
| `/root/.env_dialer` (host) y `/opt/omnileads/.env_dialer` (copia dentro del contenedor de Django) | Credenciales del kit. Ver `docs/02-INSTALACION.md` sobre nombres de variables. |
| `/opt/dialer-kit/patches/` | Copia en el host de cada archivo parcheado dentro de un contenedor — la fuente de verdad de `restore_patches.sh`. **No viene incluida en este repositorio**: cada instalación la arma a medida que parchea. |
| `/var/log/*.log` | Un log por script (`restore_patches.log`, `auto_release_entregados.log`, `sync_lead_owner.log`, `did_picker_watchdog.log`, `redis_watchdog.log`, `django_restart_watcher.log`, `transcripciones.log`, `missed_calls.log`, `voicemail_alerts.log`, `skip_leads.log`). Varios scripts solo imprimen por stdout y dependen de que el cron redirija la salida. |
| `/root/agent_inbound.json`, `/root/backfill_leads.json` | Estado intermedio de `create_agent_inbound.sh` y `backfill_prepare.py` respectivamente. |
| `/var/log/transcripciones_procesadas.json`, `/var/log/transcripts_cache.json` | Deduplicación y caché de transcripciones — cada audio se transcribe una sola vez en su vida. |
| `/tmp/transcribe_calls.lock` | `flock` para que dos corridas del cron de transcripción no se pisen. |

---

## 6. Decisiones de arquitectura y su porqué

**El servidor decide, el cliente solo pinta.** Ni la consola web ni la app móvil
calculan prioridad, dueño, ni permisos: piden datos al servidor y muestran botones.
Toda la lógica de negocio vive en `crm_webhooks.py`, `crm_dispositions.py`,
`lead_ownership.py` y `agent_api.py`. Consecuencia práctica: una regla se cambia en
un solo lugar y se refleja igual en navegador y celular (ver el comentario al
inicio de `agent_api.py`: "la app NO decide nada, solo pinta").

**El número del lead nunca sale del servidor.** El teléfono real vive en la base
de datos y en las señales SIP; lo que llega al cliente (web o app) es
`***-***-1234` (`_enmascarar()` en `agent_api.py`) y en el propio audio del
webphone el Caller-ID que ve el agente también va enmascarado
(`[mask-agent]` en `extensions_override.conf`). El vendedor marca por id de
contacto, nunca por número. Esto es lo que impide que alguien se lleve la base de
datos de teléfonos en el celular.

**Los parches se reaplican al arranque.** Los contenedores de OMniLeads se
recrean (reboot, `docker restart`, actualización) y pierden cualquier cambio
hecho directo con `docker exec`. `restore_patches.sh` sigue siempre el mismo
patrón — marca (`grep`/`test -f`) → si falta, copiar desde el host y recargar lo
mínimo → registrar en el log — y se agenda con `@reboot`. Es idempotente: correrlo
de más nunca rompe nada.

**Los automatismos se auto-reparan en los dos sentidos.** Ningún cron asume que
una orden anterior llegó: compara el estado real contra el deseado y corrige en
ambas direcciones. Ejemplo explícito en el propio código
(`sync_agent_pause.py`): si un agente tiene un lead en pantalla y no está
pausado, lo pausa; si no tiene lead y sigue pausado, lo libera. Sin esta simetría,
cualquier reinicio de Django, Asterisk o Redis deja el sistema en un estado a
medias que nadie nota hasta que alguien pregunta por qué no le llega trabajo.
