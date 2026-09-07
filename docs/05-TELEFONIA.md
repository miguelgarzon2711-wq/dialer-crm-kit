# 05 — Telefonía

Este documento explica cómo viaja una llamada dentro del kit, cómo funciona la
rotación de caller ID, cómo se enruta lo entrante por vendedor, y los errores
clásicos de configuración del trunk. Está escrito a partir del código real del
kit (`server/asterisk/*.conf`, `server/scripts/did_picker.py` y afines). Donde
el código referencia algo que **no está incluido** en este kit, se marca
explícitamente — no se inventa el contenido faltante.

---

## 1. Arquitectura de la llamada

### 1.1 Saliente (agente → celular del lead)

```
Agente aprieta "Llamar" (consola web o app)
        │
        ▼
Django (AgentActivityAmiManager) — ordena por AMI a Asterisk que origine la llamada
        │
        ▼
Asterisk ejecuta el dialplan de la campaña Preview
        │  (el contexto que decide el Caller ID va ANTES de marcar — ver 1.3)
        ▼
Contexto de ruta saliente  [oml-outr-1]   (server/asterisk/extensions_outbound_route.conf)
  - hace match del número marcado contra 3 patrones:
      _XXXXXXXXXX      (10 dígitos)              -> orden de patrón 1
      _1XXXXXXXXXX     (11 dígitos, con "1")      -> orden de patrón 2
      _+1XXXXXXXXXX    (11 dígitos, con "+1")     -> orden de patrón 2
  - Gosub(sub-oml-dialout,s,1(1, <orden>))  -> subrutina nativa de OMniLeads
    que lee de AstDB/Redis el trunk configurado (${DB(OML/OUTR/1/NAME)},
    familias OML:OUTR / OML:TRUNK) y arma el Dial() hacia el trunk PJSIP.
  - si no matchea ningún patrón: exten `i` marca DIALSTATUS=NONDIALPLAN y
    cuelga con "no hay ruta para <numero>" (Gosub sub-oml-hangup).
        │
        ▼
Trunk SIP (PJSIP) hacia el proveedor (probado con Telnyx)
        │
        ▼
Operador / carrier → celular del lead
```

El contexto genérico `[oml-outr]` (mismo archivo) incluye a `[oml-outr-1]` y lo
usan las llamadas manuales ("Llamar fuera de campaña") y las transferencias
externas — si no se define, esas dos funciones fallan en silencio con el mismo
"no hay ruta".

### 1.2 Entrante (celular del lead u operador → agente)

```
INVITE SIP entra al trunk
        │
        ▼
Contexto [from-pstn]   (server/asterisk/extensions_override.conf)
  - Workaround Telnyx: el Request-URI del INVITE a veces no trae el DID
    (llega como sip:usuario@ o sip:s@), así que el DID real se saca del
    header To: (CUT por "@" y por ":").
  - Si el To: viene con "+" al inicio, se lo recorta.
  - Guarda el caller real en __DLRCALLER y engancha un hangup handler
    (dialer-missed) para detectar llamadas NO contestadas al colgar.
  - CURL a did-picker: GET /inbound_campana?from=<caller>
      - si responde un DID  -> Goto(oml-dial-in, <DID>, 1)
      - si no responde nada -> Goto(oml-dial-in, <DID original>, 1)
        ▼
oml-dial-in (nativo de OMniLeads, no incluido en este kit) resuelve el DID
contra RutaEntrante/DestinoEntrante y hace sonar la cola/campaña
correspondiente (personal del vendedor dueño, o del grupo — ver sección 5).
        ▼
Softphone/hardphone del agente (vía Kamailio si es webphone WSS, o PJSIP directo)
```

Si la llamada NO terminó en estado `CONNECT` (nadie contestó, o el que llama
colgó esperando), el hangup handler `[dialer-missed]` dispara
`GET /missed?from=<caller>`, que hace que el lead entre a la cola como
"Llamada Perdida" (ver 2.4).

### 1.3 Dónde entra el Caller ID rotado

`did_picker.py` expone `/pick?tel=<numero>` para elegir el Caller ID saliente
(sección 2). El propio `restore_patches.sh` referencia un parche llamado
**"caller ID sticky+rampa en dialplan"** sobre un archivo
`oml_extensions_precall.conf` (marcador `PATCH CID`) — es decir, el contexto
que llama a `/pick` antes de marcar **no viene incluido en este kit**: solo
se referencia su ruta de destino y el hecho de que existe. Quien instale el
kit tiene que escribir (o traer de otro lado) ese contexto "precall" que:

1. Llama `CURL(http://10.22.22.1:8055/pick?tel=${OMLOUTNUM})`.
2. Usa el resultado como `CALLERID(num)` antes del `Dial()` hacia el trunk.

> ⚠️ **Ambigüedad real:** ni el contexto `oml_extensions_precall.conf` ni la
> función SQL `pick_did()` (ver sección 2) están en este kit. Solo está el
> servidor HTTP que las envuelve (`did_picker.py`) y la evidencia de que algo
> las llama (comentarios en `restore_patches.sh`).

---

## 2. Rotación de caller ID (`did_picker.py`)

Es un servidor HTTP mínimo (`http.server`, sin frameworks) que escucha en
**`10.22.22.1:8055`** — una IP interna, nunca debe quedar expuesta a
internet (ver sección 7). Corre como servicio systemd `did-picker`.

### 2.1 Qué hace `pick(tel)` — endpoint `/pick`

```python
tel = re.sub(r'\D', '', tel)[-10:]     # se queda con los últimos 10 dígitos
if len(tel) < 10: return ''
SELECT pick_did('<tel>');              # vía docker exec psql
```

Normaliza el teléfono a 10 dígitos y delega TODA la decisión a la función de
PostgreSQL `pick_did(tel)`.

> ⚠️ **`pick_did()` no está incluida en `server/sql/schema.sql` ni en ningún
> otro archivo de este kit.** El docstring del módulo dice
> *"sticky+cap+rampa"* y el nombre de los parches (`caller ID sticky+rampa`)
> confirma la intención, pero el **algoritmo real vive en una función SQL que
> hay que escribir aparte**. Por el contrato de uso puede inferirse que debe:
> - **Sticky por lead:** devolver siempre el mismo DID para el mismo `tel`
>   mientras ese DID siga vigente (para que el lead reconozca quién lo llama).
> - **Tope diario por número:** no elegir un DID que ya alcanzó su cupo de
>   marcaciones del día.
> - **Rampa de calentamiento:** limitar cuánto se usa un DID recién agregado,
>   subiendo el tope gradualmente con los días.
>
> Esto es una inferencia a partir del nombre y el uso, **no un hecho
> verificado en código**. Quien instale el kit debe escribir esta función (o
> conseguir la versión completa) antes de que la rotación funcione.

### 2.2 `inbound_campana(caller)` — endpoint `/inbound_campana`

Implementa el ruteo entrante "sticky por vendedor" (detalle en sección 5):

1. Busca en `dialer_lead_owner` si el teléfono ya tiene dueño → si sí, arma
   el DID virtual `900001<agente_id 2 dígitos>` y lo devuelve.
2. Si no hay dueño, busca la campaña (1 a 5) en la que ese contacto fue
   asignado más recientemente (`ominicontacto_app_agenteencontacto`) y
   devuelve el DID virtual de grupo `9000000<campaña>`.
3. Si nada matchea, devuelve `''` (el dialplan usa el DID original).

### 2.3 `missed(caller)` — endpoint `/missed`

Lee el token desde `/root/.env_dialer` (línea `DIALER_API_TOKEN=`) y hace
`POST https://<dominio-dialer>/api/v1/dialer/missed_call/` con
`{"from": tel}` y `Authorization: Bearer <token>`. Loguea éxito o error en
`/var/log/missed_calls.log`. El dominio en el código es un placeholder
(`dialer.example.com`) — hay que apuntarlo al dominio real de la instalación.

### 2.4 Cómo lo consulta el dialplan (CURL)

El patrón que usa `extensions_override.conf` (y que se asume igual para
`/pick`, aunque ese contexto no esté en el kit) es siempre el mismo:

```
same => n,Set(CURLOPT(conntimeout)=2)
same => n,Set(CURLOPT(timeout)=2)
same => n,Set(DLRINB=${CURL(http://10.22.22.1:8055/inbound_campana?from=${CALLERID(num)})})
same => n,ExecIf($["${DLRINB}" != ""]?Goto(oml-dial-in,${DLRINB},1))
```

Timeouts cortos (2s) a propósito: si `did-picker` no responde, la llamada
tiene que poder seguir con el número/ruta original en vez de quedarse
colgada esperando.

### 2.5 Por qué importa

Un número que marca demasiado seguido a las mismas redes móviles queda
marcado como **"Spam Likely"** por los propios operadores/celulares — a
partir de ahí el answer rate se cae aunque el vendedor haga todo bien. La
rotación con tope diario reparte el volumen entre varios DIDs y el
"sticky" evita que un mismo lead vea números distintos cada vez que lo
llaman (lo cual también genera desconfianza).

---

## 3. Dimensionar el pool de números

La cuenta es simple una vez que se sabe el tope diario por número:

```
números necesarios = marcaciones salientes por día ÷ tope diario por número
```

> ⚠️ El **tope diario por número** y los valores de la **rampa de
> calentamiento** viven dentro de `pick_did()`, que no está en este kit (ver
> 2.1). No hay un número concreto que documentar aquí sin inventarlo — hay
> que definirlos según la política del proveedor SIP (Telnyx y otros
> carriers recomiendan subir el volumen de un número nuevo de forma gradual
> durante 1–2 semanas antes de llevarlo a tope) y dejarlos codificados en
> `pick_did()`.

Recomendaciones generales de warm-up (no específicas de este kit, sentido
común de la industria):
- Empezar un número nuevo con un tope bajo y subirlo día a día.
- No poner en rotación varios números nuevos el mismo día — escalonarlos.
- Vigilar el answer rate por número (ver notas de reputación/CNAM del
  proveedor) para sacar de rotación el que empiece a caer.

---

## 4. Normalización de teléfonos (`normalize_phones.sh`)

**Por qué existe:** Asterisk marca dígitos, nada más. Si al número le queda
un `+`, un espacio, un guion o el "1" de país pegado sin normalizar, **la
llamada muere en silencio**: no hay excepción, no hay log de error, el
vendedor simplemente ve que "no pasó nada" y asume que el número no existe.

**Qué hace el script** (corre cada 10 minutos según su propio comentario, y
también en el pre-chequeo de lanzamiento):

1. Busca contactos que están EN COLA (`estado IN (0,1,3)` en
   `ominicontacto_app_agenteencontacto`) cuyo teléfono no matchea
   `^[0-9]{10}$`.
2. Limpia todo lo que no sea dígito. Si el resultado queda en 11 dígitos y
   empieza con `1` (código de país USA), le saca ese `1`.
3. Si el resultado limpio SÍ queda en 10 dígitos, actualiza el contacto y
   loguea `contacto <id>: '<antes>' -> '<despues>'`.
4. Lo que no se pudo arreglar (no es un número de 10 dígitos USA) se reporta
   aparte con `⚠ contacto <id> en cola con teléfono no marcable: '<tel>'` —
   sigue en cola, pero alguien tiene que revisarlo a mano.

El script solo imprime por stdout — no escribe a un archivo de log por sí
mismo; quien lo agende por cron debe redirigir la salida (ver 07-OPERACION).

---

## 5. Entrantes por vendedor ("sticky" también en inbound)

El kit crea, por cada agente activo, una **campaña entrante personal** que
clona la campaña base `id=7` ("Inbound G1"): mismas opciones de calificación,
mismos parámetros CRM, mismos supervisores, pero con un solo miembro (ese
agente) y un DID virtual propio.

### 5.1 `create_agent_inbound.py` (corre dentro del contenedor Django)

Para cada `AgenteProfile` activo:
- Nombre de campaña: `Inbound A<id> <username>`.
- DID virtual: `900001<id agente, 2 dígitos>` (ej. agente id 7 → `90000107`).
- Clona `Campana` id 7 → nueva `Campana` (mismas `OpcionCalificacion` y
  `ParametrosCrm`, para que el motor de disposiciones/CRM las reconozca).
- Copia los supervisores de la campaña 7 (detecta la columna dinámicamente).
- Crea `queue_table` (wait=20s) y `queue_member_table` para ese agente.
- Crea `DestinoEntrante` (tipo=1, apunta a la campaña) y `RutaEntrante` con
  el DID virtual.
- Al final llama `RegenerarAsteriskFamilysOML().regenerar_asterisk()` para
  empujar las familias de Redis que Asterisk usa en tiempo real.
- Es **idempotente**: cada paso primero busca si ya existe antes de crear.
- Imprime `RESULT:<json>` con `[{agente_id, username, campana_id, queue, did, ruta_id}, ...]`.

### 5.2 `create_agent_inbound.sh` (orquestador, correr en el host)

```
docker cp create_agent_inbound.py  -> contenedor Django
manage.py shell -c "exec(...)"     -> corre el script de arriba
extrae RESULT: -> /root/agent_inbound.json
python3 inbound_redis_sync.py      -> asegura Redis (ver 5.3)
agrega bloques faltantes a queues.conf (uno por campaña nueva)
docker cp queues.conf -> Asterisk como oml_queues_override.conf
asterisk -rx "queue reload all"
```

Se corre **manualmente después de crear agentes nuevos** (no es un cron).

### 5.3 `inbound_redis_sync.py`

Asegura en Redis, a partir de `/root/agent_inbound.json`:
- `OML:CAMP:<id>` — clonado de `OML:CAMP:7` con `QNAME`, `SHOWCAMPNAME` y
  `QUEUETIME=20` propios (solo si no existía).
- `OML:INR:<did>` — `NAME`, `DST=1,<campana_id>`, `ID=<ruta_id>`, `LANG=es`
  (siempre se refresca).
- `OML:CAMPAIGN-AGENTS:<id>` — agrega al agente como miembro (solo si el
  set no existía).

Lo llama tanto `create_agent_inbound.sh` como `restore_patches.sh` (si
`/root/agent_inbound.json` existe), para que sobreviva a un reinicio de
Redis.

### 5.4 Cómo rutea el selector

`inbound_campana()` en `did_picker.py` decide, en este orden:
1. **¿El que llama tiene dueño?** (`dialer_lead_owner`) → devuelve el DID
   personal `900001<agente_id>` del dueño → **solo esa cola personal suena**.
2. **Si no tiene dueño** → busca la campaña (1–5) donde el contacto fue
   asignado más recientemente → devuelve el DID de grupo `9000000<campaña>`
   → suena la cola compartida del grupo (cualquier agente libre atiende).

---

## 6. Enmascarado del número

El vendedor **nunca ve el número completo del lead**. Decisión de producto
explícita en el código (comentarios "MASCARA"). El número real solo circula
hacia el trunk/carrier y queda íntegro en los registros (`LlamadaLog`,
grabaciones).

Se enmascara en tres capas:

| Dónde | Archivo | Qué hace |
|---|---|---|
| **Webphone** (tramo hacia el agente) | `[mask-agent]` en `extensions_override.conf` | Antes de marcar hacia el agente, si el contacto está identificado (`OMLCODCLI` seteado y válido), reemplaza `CALLERID` y `CONNECTEDLINE` por `"Lead ***-***-<4 últimos>" <XXXXXX+4 últimos>`. Sin identificar (número tecleado a mano, o entrante de desconocido), pasa el número real. |
| **Tarjeta / formulario de la consola web** | `views_campana_preview.py.patched`, `forms_base.py.patched`, `views_calificacion_cliente.py.patched`, `views_agente.py.patched` (referenciados en `restore_patches.sh`, marcador `PATCH MASCARA`) | Según los comentarios del script de persistencia, enmascaran el número en la tarjeta de Preview y en el formulario de disposición. **Estos archivos parcheados no están incluidos en este kit** (solo se referencian su ruta destino y su marcador) — no se puede documentar el detalle exacto de la implementación. |
| **API para clientes móviles** | `_enmascarar()` en `server/django/agent_api.py` | `'***-***-' + telefono[-4:]` — usada al devolver el contexto del lead (`telefono_contacto`) a la app del vendedor. Esta sí está completa en el kit. |

---

## 7. Seguridad SIP

**El riesgo real:** un puerto SIP (5060/5160) abierto a internet sin
autenticación deja que cualquiera registre un softphone haciéndose pasar por
un agente (`REGISTER` a una extensión tipo 1100–1110) y origine llamadas que
se facturan a la cuenta del dueño del dialer. Esto ya pasó en producción
(ver `firewall.sh`, comentario del 2026-09-05).

### `firewall.sh`

Corre `iptables` idempotente (`-C` para chequear, `-I` para insertar si
falta):

1. **Puertos internos de OMniLeads** — nunca deben ser públicos:
   `1440 4573 4730 5038 6379 7088 8000 8098 8099 8888 9000 9001 9191 22223`
   → `DROP` en `DOCKER-USER` para tráfico entrante por la interfaz WAN
   (`enp1s0` en el ejemplo — **ajustar al nombre real de la interfaz del
   servidor**). Solo bloquea lo que entra desde internet; el tráfico interno
   entre contenedores no pasa por esa interfaz y no se ve afectado.
2. **Puertos de servicios del host** (dashboard, etc.):
   `8001 8002 3001 6379 5038 9000 9001` → `DROP` en `INPUT` desde la WAN.
3. **Anti-registro externo:** bloquea paquetes `REGISTER sip:` en el puerto
   5060 desde internet (match de string, `INPUT` y `DOCKER-USER`, tcp y udp)
   y bloquea el puerto **5160 completo** desde internet — porque Telnyx
   nunca manda `REGISTER` (solo INVITE entrante) y los agentes legítimos
   entran por Kamailio en loopback (`127.0.0.1`), no directo desde afuera.

Según el propio `CLAUDE.md` del kit, hay que dejar este script como servicio
de systemd para que sobreviva a reinicios.

### `purge_sip_intruders.sh`

Limpieza reactiva, para correr si ya hubo intrusos o como chequeo periódico:

1. Lista `database show registrar/contact` en Asterisk, filtra los que **no**
   tengan `"via_addr":"127.0.0.1"` (es decir, no vinieron por Kamailio) y los
   borra uno por uno (`database del registrar/contact <key>`).
2. Reporta cuántos contactos externos (rango 10xx/11xx) quedan.
3. Muestra el estado de la extensión 1100 como chequeo puntual.

Este script es de limpieza — el que previene que vuelva a pasar es
`firewall.sh`. Usar los dos juntos.

---

## 8. Configuración del trunk

Errores clásicos, sacados directo del código:

| Error | Dónde se ve | Cómo evitarlo |
|---|---|---|
| **Nombre del endpoint mal puesto** | `[test-trunk]` en `extensions_override.conf` marca `Dial(PJSIP/+15551000001@telnyx,30)` — el endpoint se llama literalmente `telnyx` (el nombre configurado en el trunk de OMniLeads), **sin** `from_user` aparte | El endpoint PJSIP debe llamarse exactamente igual a como está configurado en OMniLeads; no hace falta (ni corresponde) setear `from_user` por separado |
| **Formato no E.164 / inconsistente** | Los 3 patrones de `[oml-outr-1]`: `_XXXXXXXXXX`, `_1XXXXXXXXXX`, `_+1XXXXXXXXXX` | Los tres deben quedar cubiertos en la ruta saliente (10 dígitos, 11 con "1", o con "+1") — si falta alguno, ese formato de número cae al `exten => i` y muere con "no hay ruta" |
| **Contexto de ruta saliente que hay que crear a mano** | Comentario en la primera línea de `extensions_outbound_route.conf`: *"generador de config roto: se escribe a mano"* | El generador nativo de rutas salientes de OMniLeads no arma bien este archivo — hay que escribirlo/copiarlo a mano como `oml_extensions_outr_override.conf` (vía `restore_patches.sh`) |
| **Falta el contexto genérico `[oml-outr]`** | Mismo archivo | Sin él, "Llamar fuera de campaña" y las transferencias externas no tienen a dónde ir |

**Antes de dar por terminada la telefonía:** probar con una llamada real —
`channel originate Local/s@test-trunk application Wait 30` prueba el trunk
saliente con un Caller ID de prueba; `channel originate Local/s@test-inbound
application Wait 40` prueba el ruteo entrante. El contexto `[test-leads]`
trae tres números de prueba ya armados para validar audio sin llamar a un
lead real:

| Número de prueba | Comportamiento |
|---|---|
| `5550000001` | Contesta, hace eco de tu propia voz (valida audio ida y vuelta) |
| `5550000002` | Timbra 12s y cae a buzón de voz |
| `5550000003` | Timbra 30s y nadie contesta |
