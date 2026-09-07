# Integración CRM (GoHighLevel) ↔ Dialer

> Lector objetivo: un agente de IA que va a instalar este kit para OTRO cliente, sin haber visto el código fuente.
> Fuente: `server/django/crm_webhooks.py`, `server/django/crm_dispositions.py`, `server/django/lead_ownership.py`, `server/scripts/backfill_prepare.py`, `server/scripts/backfill_inject.py`.
> Todo lo marcado `<PLACEHOLDER>` lo define cada instalación. Donde el código es ambiguo o parece incompleto, se dice explícitamente — no se rellena con supuestos.

---

## 1. Diagrama de flujo (texto)

```
 1. LEAD ENTRA AL CRM (GoHighLevel)
    - Anuncio / formulario / WhatsApp / llamada entrante -> se crea o actualiza
      un Contacto en GHL.
    - Una automatización de GHL (workflow) decide que ese contacto debe entrar
      al dialer y dispara un webhook saliente HTTP POST.

 2. WEBHOOK GHL -> DIALER
    - POST a  /api/v1/crm/lead_action/?campana=<clave>  con action=inject
      (ver sección 2 para el JSON exacto).
    - El dialer:
        a) valida action + ghl_id,
        b) chequea el interruptor DIALER_LIVE (si está apagado, responde
           "ok, gated" y NO hace nada — pensado para antes del lanzamiento;
           esos leads los recoge después el backfill),
        c) resuelve a qué campaña Preview (grupo de agentes) va el lead,
        d) crea o actualiza el Contacto interno de OMniLeads (por id_externo
           = ghl_id),
        e) calcula la prioridad (TIPO_ORDEN, sección 4) y decide si el lead
           ya tiene "dueño" (sticky, sección 5) para forzar que esa
           inyección caiga en su cola en vez del pool general,
        f) inserta/actualiza el registro AgenteEnContacto (AEC) que
           representa "este contacto está en la cola de esta campaña".

 3. EL LEAD QUEDA EN COLA (Preview)
    - OMniLeads reparte el AEC al primer agente libre de esa campaña
      (o, si tiene dueño, directo a él) respetando el orden de prioridad.

 4. EL VENDEDOR LLAMA
    - El agente da "Obtener contacto" (AEC pasa a estado ENTREGADO): en ese
      momento el dialer ya asigna el lead al vendedor mapeado en GHL
      (assignedTo), ANTES de que exista disposición — así "Ir al CRM" desde
      GHL siempre muestra ese lead.
    - Asterisk marca. Si contesta, AEC pasa a ASIGNADO (llamada activa).

 5. DISPOSICIÓN (el vendedor califica la llamada)
    - Se guarda un CalificacionCliente en OMniLeads. Un signal post_save
      dispara TODO lo que va de vuelta al CRM (sección 6):
        - tags que se agregan/quitan en el contacto de GHL,
        - nota en el contacto (solo para "No contesto" / "Número
          equivocado"),
        - assignedTo (owner en GHL): se confirma al vendedor si la
          disposición cuenta como "contestó", o se libera (null) si no
          contestó y el lead todavía no tiene dueño permanente,
        - si la disposición prueba que hubo conversación real, se fija el
          "dueño de por vida" del lead (sticky, sección 5) — solo la
          PRIMERA vez.

 6. VUELTA AL CRM
    - Todos los cambios de 5) se escriben en GHL vía su API REST
      (tags, notas, assignedTo). El lead sigue viviendo en GHL como fuente
      de verdad de datos de negocio; el dialer solo orquesta la cola de
      llamadas y refleja el resultado.

 7. RE-ENTRADAS AL DIALER
    - Si el lead vuelve a escribir por WhatsApp, no contesta y hay que
      reintentar, tiene una cita agendada, o llamó y no le contestaron,
      GHL dispara de nuevo el mismo webhook de inject (u otro tipo) y el
      ciclo se repite desde el paso 2, respetando el dueño permanente si
      ya existe.
```

Notas sobre el diagrama:
- El dialer (OMniLeads) NO es dueño de los datos del lead — GHL sí. El dialer solo guarda lo mínimo para poder marcar (teléfono, nombre, id externo de GHL, tipo/prioridad).
- La comunicación es en las dos direcciones por HTTP: GHL → dialer por webhooks configurados en automatizaciones de GHL; dialer → GHL por llamadas directas a la API REST de GHL (`https://services.leadconnectorhq.com`) hechas por el backend Django cuando se guarda una disposición.

---

## 2. Endpoint principal: `POST /api/v1/crm/lead_action/`

Vista: `CRMLeadActionView` en `crm_webhooks.py`.

### Autenticación
`SessionAuthentication` o `ExpiringTokenAuthentication` (token Bearer de OMniLeads). El header esperado es:
```
Authorization: Bearer <TOKEN_OML>
```
El código no muestra cómo se genera/valida ese token — es el mecanismo estándar de autenticación por token de OMniLeads (`ExpiringTokenAuthentication`), no algo específico de esta integración.

### Query params
- `campana` (opcional si viene en el body): clave de la campaña destino. Ver tabla `CAMPANAS` abajo. También acepta el valor especial `auto`.

### Body JSON — campos

GHL puede mandar los "Custom Data" de la automatización ANIDADOS bajo una clave `customData` (o `custom_data`). Si el body no trae `action` en el nivel superior, el endpoint busca esa clave, la "aplana" (mezcla sus campos con el nivel superior) y sigue. Es decir: tanto un body plano como uno con `customData: {...}` funcionan.

```jsonc
{
  "action": "inject",              // "inject" | "remove" — OBLIGATORIO
  "ghl_id": "<ID_CONTACTO_GHL>",    // OBLIGATORIO siempre
  "phone": "<PLACEHOLDER_10_DIGITOS>", // OBLIGATORIO solo si el contacto es nuevo para el dialer
  "nombre": "<NOMBRE_LEAD>",        // opcional
  "tipo": "Nuevo Lead",             // ver sección 4 — condiciona prioridad y reglas
  "campana": "grupo1",              // alternativa a pasarlo por query string

  // Solo si tipo = "Recordatorio Cita":
  "cita_inicio": "<FECHA_HORA_CITA>",   // texto libre, se intenta parsear (ver formatos abajo)
  "vendedor_ghl": "<ID_USUARIO_GHL>",   // para resolver el agente dueño de la cita
  "vendedor_email": "<EMAIL_VENDEDOR>"  // alternativa a vendedor_ghl
}
```

Detalle de campos:
- **`action`** (string, obligatorio): `"inject"` para meter/actualizar el lead en cola, `"remove"` para sacarlo.
- **`ghl_id`** (string, obligatorio): id del contacto en GHL. Es la clave con la que el dialer busca/crea el Contacto interno (`Contacto.id_externo`).
- **`phone`**: obligatorio ÚNICAMENTE cuando no existe todavía un Contacto interno con ese `ghl_id` (lead nuevo para el dialer). Se normaliza a 10 dígitos (se le quita el prefijo `1` si viene como 11 dígitos tipo E.164 de EE. UU.). Si tras normalizar no quedan exactamente 10 dígitos, el endpoint responde error 400. **Este normalizador asume formato de EE. UU. (10 dígitos)** — si el cliente nuevo es de otro país hay que adaptar `_normalize_phone()`.
- **`nombre`**: nombre del lead. Si el contacto ya existe y no se manda `nombre`, se conserva el nombre que ya tenía guardado.
- **`tipo`**: define la prioridad de entrega y activa reglas especiales (sección 4). Los valores documentados en el docstring original del endpoint son `"Nuevo Lead"`, `"WA Respondio"`, `"Callback"`, `"Seguimiento"`; además el código maneja explícitamente `"Recordatorio Cita"`. `"WA Cita Pendiente"` y `"Llamada Perdida"` existen como tipos internos que el propio dialer asigna automáticamente en ciertos casos (no se documentan como valores que GHL deba mandar directamente para inject normal, aunque técnicamente si se manda ese string se le da el orden correspondiente).
- **`campana`**: la clave de campaña (ver tabla abajo) o `"auto"`. Si no viene ni por query param ni por body y no es `"auto"`, y la clave no está en el mapeo, responde 400.
- **`cita_inicio`, `vendedor_ghl`, `vendedor_email`**: solo relevantes cuando `tipo == "Recordatorio Cita"` (ver reglas especiales más abajo).

### Mapeo de campañas (`CAMPANAS`)

```python
CAMPANAS = {
    "grupo1": 1,
    "grupo2": 2,
    "grupo3": 3,
    "mixto":  4,
    "v25":    5,
}
```
Esto traduce la clave estable que usan las automatizaciones de GHL a un `campana_id` interno de OMniLeads. **Si el cliente reorganiza sus grupos de agentes, solo se cambia este diccionario — las URLs/automatizaciones de GHL no se tocan.** Cada instalación nueva debe redefinir este diccionario con sus propias claves/ids de campaña Preview.

`campana=auto`: si no se especifica campaña, el dialer reparte el lead solo entre las campañas listadas en `AUTO_WEIGHTS` (peso = número de agentes de ese grupo), enviándolo al grupo con menor cola pendiente por agente. Si el contacto ya estuvo antes en una de esas campañas, repite la misma (coherencia). Cada instalación debe redefinir `AUTO_WEIGHTS` con sus propias campañas/pesos.

### Reglas especiales dentro de `inject`

- **Upgrade automático `WA Respondio` → `WA Cita Pendiente`**: si la última disposición de ese contacto en esa campaña fue `"Va a agendar"`, un nuevo inject con `tipo="WA Respondio"` se reescribe internamente a `"WA Cita Pendiente"` (prioridad más alta, sticky al mismo agente).
- **`tipo="Recordatorio Cita"`**:
  - Si `cita_inicio` ya pasó (se interpreta contra la zona horaria `America/New_York` — **ajustar esta zona horaria al mercado de cada cliente**), el endpoint NO inyecta nada y responde `{"status":"skipped","reason":"cita ya paso", "cita_inicio": ...}` con HTTP 200.
  - Si el lead todavía no tiene "dueño" (sticky, sección 5), el endpoint intenta resolver el agente dueño de la cita a partir de `vendedor_ghl` o `vendedor_email` (tabla `dialer_agent_crm_map`, ver sección 5). Si lo resuelve, fija ese agente como dueño permanente. Si NO lo puede resolver, NO inyecta y responde `{"status":"skipped","reason":"sin dueno: manda vendedor_ghl o vendedor_email"}` con HTTP 200.
  - Si el lead no tiene dueño y este tipo llega sin poder resolver agente, tampoco se inyecta (ver `_inject_in_preview`: sin dueño, un `"Recordatorio Cita"` no se mete a la cola bajo ninguna circunstancia — es una regla dura: el recordatorio es solo para el dueño de la cita).
  - Formatos de fecha que el parser intenta (en orden), antes de caer a un parser genérico (`dateutil`): ISO con/sin offset, `YYYY-MM-DD HH:MM[:SS]`, `Mon DD, YYYY HH:MM AM/PM` (variantes con día de la semana, con/sin coma, con "at"), `MM/DD/YYYY HH:MM`, `DD/MM/YYYY HH:MM`, `YYYY-MM-DD`. Si nada matchea, se loguea el valor crudo (`logger.warning`) para poder ajustar el formato real que manda GHL.

### Respuestas

| Caso | HTTP | Body |
|---|---|---|
| Falta `action` o `ghl_id` | 400 | `{"error": "action y ghl_id son requeridos"}` |
| `campana` inválida o faltante (y no es `auto`) | 400 | `{"error": "campana invalida o faltante", "validas": [...claves..., "auto"]}` |
| Contacto nuevo sin teléfono válido (10 dígitos) | 400 | `{"error": "phone valido (10 dig) requerido para contacto nuevo"}` |
| Dialer todavía no está "vivo" (`DIALER_LIVE` apagado) | 200 | `{"status": "ok", "gated": true}` — no se hizo nada |
| `Recordatorio Cita` con fecha ya pasada | 200 | `{"status": "skipped", "reason": "cita ya paso", "cita_inicio": "<valor recibido>"}` |
| `Recordatorio Cita` sin dueño ni vendedor resoluble | 200 | `{"status": "skipped", "reason": "sin dueno: manda vendedor_ghl o vendedor_email"}` |
| Inject correcto | 200 | `{"status": "ok", "contact_id": <id interno>, "campana": "<clave>", "orden": <prioridad numérica>}` |
| Inject falló (excepción interna) | 500 | `{"error": "inject failed"}` |
| `remove` sin contacto encontrado | 404 | `{"error": "contacto no encontrado"}` |
| `remove` correcto | 200 | `{"status": "ok", "contact_id": <id>, "campanas": [<ids removidos>]}` |
| `remove` falló | 500 | `{"error": "remove failed"}` |
| `action` inválido (no es inject/remove) | 400 | `{"error": "action invalido"}` |

`remove` sin `campana` especificada saca el contacto de TODAS las campañas del mapeo (útil para "booked"/"DQ": el lead ya no debe seguir en ninguna cola). `remove` respeta los AEC en estado ASIGNADO (llamada activa: no se toca).

---

## 3. Otros endpoints

### `POST /api/v1/dialer/missed_call/`
Vista: `CRMMissedCallView`. **No pasa por el gate `DIALER_LIVE`** (siempre actúa).

Para qué sirve: cuando entra una llamada al dialer (inbound) y nadie contesta, se reinyecta el lead a la cola con prioridad máxima (tipo `"Llamada Perdida"`, orden 0) para que se le devuelva la llamada cuanto antes.

Body/query esperado:
```json
{ "from": "<TELEFONO_QUE_LLAMO>" }
```
(también acepta `?from=...` como query param). El teléfono se normaliza a 10 dígitos; si no cumple, responde 400 `{"error": "from invalido"}`.

Comportamiento:
- Busca contactos internos con ese teléfono; si hay varios, prioriza el que ya tenga "dueño" (sticky).
- Si el contacto no existe en GHL (o su `id_externo` empieza con `DEMO`), lo busca/crea en GHL por teléfono (`POST /contacts/upsert`); si es contacto nuevo en GHL le pone `firstName="Llamada perdida <telefono>"` y `source="Llamada perdida (dialer)"`. En cualquier caso le agrega el tag `llamada-perdida`.
- Si el contacto no existía en el dialer, lo crea. La campaña destino es la última en la que ese contacto tuvo actividad (o `grupo1` por defecto si nunca tuvo).
- Inyecta con `tipo="Llamada Perdida"` (orden 0 — máxima prioridad) respetando el dueño si lo tiene.

Respuesta:
```json
{"status": "ok", "contacto_id": 123, "campana": 1, "dueno": -1}
```
(`dueno` es el `agente_id` dueño permanente, o `-1` si no tiene). `status` puede ser `"error"` si falló la inyección, con el mismo resto de campos.

### `GET /api/v1/agente/call_outcome/?contacto_id=<ID>`
Vista: `CRMCallOutcomeView`. La usa el formulario de disposición del agente para decidir qué opciones de calificación mostrarle (por ejemplo, solo dejar "No contestó"/"Número equivocado" si la llamada no conectó).

- Si el usuario logueado no tiene perfil de agente, o no se manda `contacto_id` válido: `{"answered": true, "attempts": 0}`.
- Si sí: delega en `crm_dispositions.resumen_llamadas(agente_id, contacto_id)`.
  - **Importante:** en este kit el "gate" que compara contra llamadas contestadas está **desactivado por defecto** (`GATE_ACTIVO = False`, ver sección 6). Mientras esté desactivado, este endpoint SIEMPRE responde `{"answered": true, "attempts": 0, "last_event": null, "last_duration": null, "gate": "off"}`, sin consultar nada.
  - Si se activa el gate, la respuesta real es `{"answered": <bool>, "attempts": <nº de intentos DIAL>, "last_event": "<último evento de LlamadaLog>", "last_duration": <duración del último COMPLETEAGENT/COMPLETEOUTNUM o null>}`, calculado sobre una ventana de 3 horas (`GATE_VENTANA`).

### `GET /api/v1/contact_history/?contacto_id=<ID>`
Vista: `ContactoHistorialView`. Devuelve las últimas 10 disposiciones de ese contacto (para mostrarlas en el panel de preview antes de llamar).

Respuesta:
```json
{
  "history": [
    {
      "fecha": "07/09 14:32",
      "disposition": "Va a agendar",
      "notes": "texto de observaciones libres del agente",
      "agent": "Nombre Apellido del agente"
    }
  ]
}
```
Las fechas se formatean en la zona horaria `America/New_York` (**ajustar según el mercado del cliente**). Si no se manda `contacto_id`, responde `{"history": []}`. Si `contacto_id` no es numérico, responde 400.

### `GET /api/v1/agente/en_llamada/`
Vista: `EnLlamadaView`. Dice si el agente logueado tiene una llamada activa en este momento (se usa para controlar el flujo de la pantalla de disposición).

Respuesta: `{"en_llamada": true|false}`. Internamente lee de Redis la clave `OML:AGENT:<agente_id>` (campos `CONTACT_NUMBER` y `STATUS`); se considera en llamada si hay `CONTACT_NUMBER` seteado y el `STATUS` no contiene `"ACW"` (after-call-work). Si el usuario no tiene perfil de agente, responde `false`.

### `POST /api/v1/agente/skip_lead/`
Vista: `SkipLeadView`. **Está desactivada en este kit**: el `post()` empieza con:
```python
logger.warning(...)
return Response({"error": "Saltar leads esta desactivado"}, status=403)
```
y todo el código de abajo (que implementaría "saltar" un lead `WA Respondio`/`WA Cita Pendiente` sin marcarlo ni disponerlo) es inalcanzable — queda como referencia de diseño, no como funcionalidad activa. Si una instalación nueva quiere reactivar esta función hay que borrar ese `return` temprano y revisar la lógica de abajo (valida `contacto_id`/`campana_id`/`razon`, bloquea si hay llamada activa (`ASIGNADO`), solo permite tipos `WA Respondio`/`WA Cita Pendiente`, marca el AEC como `FINALIZADO` y deja un registro en `/var/log/skip_leads.log`).

---

## 4. Tipos de lead y prioridades (`TIPO_ORDEN`)

```python
TIPO_ORDEN = {
    "Recordatorio Cita": 0,   # día de la cita (mañana + 1h antes) -> SOLO al vendedor dueño de la cita
    "Llamada Perdida":   1,   # el lead llamó y nadie contestó
    "WA Cita Pendiente": 2,   # ya habló, quedó de confirmar hora y escribió por WA
    "Callback":          3,   # "llámame a las 5"
    "WA Respondio":      4,   # respondió por WA y no está asignado
    "Nuevo Lead":        5,
    "Seguimiento":       6,
}
```

- **Menor número = mayor prioridad = se entrega primero.**
- Cualquier `tipo` que no esté en el diccionario recibe orden `6` por defecto (mismo nivel que `Seguimiento`, el más bajo documentado).
- Razonamiento de negocio detrás del orden (según los comentarios del código): una cita ya agendada (recordatorio) es lo más urgente y solo le sirve al dueño; una llamada perdida hay que devolverla ya; un lead que ya casi tiene cita pero falta confirmar hora es más urgente que uno que recién escribió; un callback pactado tiene hora comprometida; un "WA respondió" sin asignar es más urgente que un lead nuevo frío; seguimiento es lo de menor prioridad.
- **Blindaje de prioridad 0** (`TIPOS_PROTEGIDOS = ("Recordatorio Cita", "Llamada Perdida", "WA Cita Pendiente")`): si un contacto ya está en cola con uno de estos tipos y hace menos de 24 horas que se actualizó, una inyección posterior de MENOR prioridad (ej. un `"Nuevo Lead"` que llega porque se recreó el contacto en GHL) NO lo degrada — se conserva el tipo/orden protegido.
- **Una sola campaña Preview a la vez**: al inyectar un contacto en una campaña, cualquier cola pendiente (`ESTADO_INICIAL`) que tuviera en las OTRAS campañas del mapeo se cierra (pasa a `FINALIZADO`). Un lead nunca queda "duplicado" en dos colas.

**Cómo cambiar las prioridades**: editar directamente el diccionario `TIPO_ORDEN` en `crm_webhooks.py`. Es un mapeo estático — no hay configuración por base de datos ni panel. Si se agregan tipos nuevos, agregarlos acá con su número; si no se agregan, caerán en `6` (última prioridad) por el `.get(tipo, 6)`.

---

## 5. Ciclo de asignación del lead (dueño / sticky)

Hay **dos conceptos de "dueño" distintos que trabajan juntos**:

1. **Dueño permanente interno del dialer** (`lead_ownership.py`, tabla `dialer_lead_owner`): determina a qué agente se le entrega la COLA de ese lead dentro de OMniLeads.
2. **`assignedTo` (owner) dentro de GHL**: es el campo nativo de GHL que muestra quién es el vendedor responsable del contacto en el CRM. Se actualiza desde `crm_dispositions.py` en cada disposición.

### 5.1 Cuándo se fija el dueño permanente (sticky)

Se fija la PRIMERA vez que una disposición con `agente_id > 0` tiene un nombre dentro de:
```python
DISPOS_CONVERSACION = {
    "Agendo cita", "Va a agendar", "Llamada de vuelta programada", "Prefiere WhatsApp",
    "Solo queria precio", "Colgo", "Error mio de ventas", "No interesado", "Ya compro",
}
```
Se guarda en la tabla `dialer_lead_owner(contacto_id PK, agente_id, since, motivo)` con `INSERT ... ON CONFLICT (contacto_id) DO NOTHING` — es decir, **el primero que "conversó" con el lead gana para siempre**; disposiciones posteriores (incluso de otro agente) NO cambian el dueño.

También se puede fijar el dueño para `tipo="Recordatorio Cita"` sin dueño previo, resolviendo el vendedor por `vendedor_ghl`/`vendedor_email` contra la tabla `dialer_agent_crm_map` (sección 2).

### 5.2 Qué significa "para siempre"

- El dueño **NO se pierde** aunque el agente se marque inactivo o se borre del dialer (`lead_ownership.agente_inactivo()` se usa para decidir si REPARTIR nuevas asignaciones a ese agente, pero no borra el registro de dueño ya existente).
- Toda inyección posterior de ese contacto — sea `Seguimiento`, `Nuevo Lead`, `WA Respondio`, `Callback`, `WA Cita Pendiente`, en cualquier campaña — se entrega SOLO al dueño (`_inject_in_preview` consulta `lead_ownership.get_owner()` primero, antes de cualquier otra regla de asignación).
- Si el dueño no está en la campaña Preview donde llegó la inyección (por ejemplo cambió de grupo), el lead se mueve a una campaña donde el dueño SÍ esté (código: "PATCH CAMPANA DEL DUEÑO").
- Si un AEC vuelve al pool (`agente_id = -1`, estado `INICIAL`) por cualquier motivo (liberar contacto, logout, etc.), un signal lo regresa inmediatamente al dueño si existe. Además hay una función de barrido (`sync_pool()` / `sync_contacto()`) pensada para correrse periódicamente como red de seguridad.

### 5.3 Conjuntos de disposición usados para el ciclo de `assignedTo` en GHL

```python
CONTESTO = {
    "Agendo cita", "Va a agendar", "Llamada de vuelta programada", "Prefiere WhatsApp",
    "Solo queria precio", "No interesado", "Ya compro", "Colgo", "Error mio de ventas",
}
NO_CONTESTO = {"No contesto", "Numero equivocado"}
```
**Nota de ambigüedad explícita**: `CONTESTO` y `DISPOS_CONVERSACION` (sección 5.1) contienen exactamente los mismos 9 nombres — son dos variables separadas en dos archivos distintos que hoy tienen el mismo contenido. Si se agrega o quita una disposición de una lista hay que revisar si también corresponde cambiar la otra; el código no las deriva una de la otra.

Hay un tercer conjunto, parecido pero NO igual, en `crm_webhooks.py`:
```python
STICKY_SI_HABLARON = {
    "Agendo cita", "Va a agendar", "Llamada de vuelta programada", "Prefiere WhatsApp",
    "Solo queria precio", "Colgo", "Error mio de ventas",
}
```
Es igual a `CONTESTO`/`DISPOS_CONVERSACION` pero **sin** `"No interesado"` ni `"Ya compro"`. Se usa solo para decidir si un `WA Respondio` sin dueño en pool debe ir directo al último agente que conversó con ese lead (`_agent_si_conversaron`): tiene sentido excluir esos dos porque son desenlaces cerrados (no interesado / ya compró en otro lado) donde no aplica "recuperar" al mismo vendedor. Queda documentado tal cual está en el código, sin asumir que sea o no intencional en cada instalación nueva.

### 5.4 Cuándo se asigna / desasigna `assignedTo` en GHL

- **Al "Obtener contacto"** (AEC pasa a estado `ENTREGADO`): se asigna inmediatamente el `assignedTo` de GHL al vendedor mapeado del agente que lo tomó (tabla `dialer_agent_crm_map`), ANTES de que exista disposición. Esto es para que "Ir al CRM" desde el lead siempre lo muestre asignado a quien lo está trabajando.
- **Al guardar la disposición**:
  - Si la disposición está en `CONTESTO`: se confirma `assignedTo` = vendedor mapeado del **dueño permanente** si ya existe, si no, del agente que disposicionó.
  - Si la disposición está en `NO_CONTESTO` **y el lead todavía NO tiene dueño permanente**: se limpia `assignedTo` (`null`) en GHL — el lead vuelve a estar "sin vendedor" en el CRM.
  - Si la disposición está en `NO_CONTESTO` pero el lead **ya tiene dueño**: NO se desasigna (el dueño se queda como responsable en GHL aunque esta vez no haya contestado).
  - Si el agente no tiene fila en `dialer_agent_crm_map`, se loguea warning y no se toca `assignedTo`.

### 5.5 Comportamiento asociado: pausa por "lead en pantalla"

No es estrictamente CRM, pero es parte del mismo archivo: mientras un agente tiene un AEC en `ENTREGADO` o `ASIGNADO` (lead en pantalla sin disposicionar), el dialer lo pausa en las colas de Asterisk para que no le entren llamadas entrantes; se despausa al finalizar. Hay un control activable/desactivable (`PAUSA_GESTION_ACTIVA`) y una función de barrido (`revisar_pausas_gestion`) pensada para correr cada minuto como seguro anti-agente-colgado.

---

## 6. Qué se escribe de vuelta al CRM en cada disposición

Todo esto ocurre en `crm_dispositions.py`, disparado por el signal `post_save` de `CalificacionCliente`, en un hilo aparte (no bloquea el guardado de la disposición). Requiere que el contacto tenga `id_externo` (ghl_id) seteado; si no, no hace nada.

### 6.1 Tags que se agregan (`TAG_ADD`)

| Disposición | Tag(s) agregados |
|---|---|
| Agendo cita | `cita-agendada-dialer` |
| Va a agendar | `va-a-agendar` |
| Llamada de vuelta programada | `llamar-mas-tarde` |
| Prefiere WhatsApp | `prefiere-wa` |
| No interesado | `dq-no-interesado` |
| Ya compro | `dq-ya-compro` |
| Numero equivocado | `numero-malo` |

Las disposiciones `"Solo queria precio"`, `"Colgo"`, `"Error mio de ventas"` y `"No contesto"` **no agregan ningún tag** (no aparecen en `TAG_ADD`).

### 6.2 Tags que se quitan

- `llamar-mas-tarde` se quita en TODA disposición **excepto** cuando la disposición que se está guardando es justamente `"Llamada de vuelta programada"`. Junto con ese tag también se quitan `callback` y `requested callback` (tags que pone el workflow de calendario de GHL cuando el lead agenda un callback).
- `va-a-agendar` se quita en TODA disposición **excepto** cuando la disposición es `"Va a agendar"`.

### 6.3 Notas en el contacto

Solo se agrega una nota automática cuando la disposición es `"No contesto"` o `"Numero equivocado"`:
```
📞 <Disposición> — <Nombre del agente>
```
(usa el emoji de teléfono, U+1F4DE). Para el resto de disposiciones **no se escribe nota desde este archivo** — el comentario del código aclara que esas quedan cubiertas por otro mecanismo (transcripción/Whisper) que está fuera del alcance de estos archivos.

### 6.4 Cambio de owner (`assignedTo`)

Ver sección 5.4 (mismo mecanismo, es la misma función `_procesar`).

### 6.5 Endpoints de GHL usados

```
Base:    https://services.leadconnectorhq.com
Version header: 2021-07-28   (header "Version")
Auth:    Authorization: Bearer <GHL_API_TOKEN>

POST   /contacts/upsert                 (buscar/crear contacto por teléfono)
PUT    /contacts/{id}                   (actualizar firstName/source/assignedTo)
POST   /contacts/{id}/tags              (agregar tags)
DELETE /contacts/{id}/tags              (quitar tags)
POST   /contacts/{id}/notes             (agregar nota)
GET    /contacts/{id}                   (leer tags/assignedTo — usado en backfill)
GET    /conversations/search?locationId=&contactId=
GET    /conversations/{id}/messages?limit=100
```

---

## 7. Backfill (carga inicial de leads viejos)

Dos scripts separados, pensados para correr una sola vez en el lanzamiento (o cuando se necesite recuperar leads que quedaron sin trabajar antes de que el dialer estuviera activo).

### 7.1 `backfill_prepare.py` — arma la lista, NO inyecta nada

Corre en el **host** del dialer (fuera de Django), lee variables desde `/root/.env_dialer`:
```
SUPABASE_URL=<PLACEHOLDER>
SUPABASE_SERVICE_KEY=<PLACEHOLDER>
GHL_API_TOKEN=<PLACEHOLDER>
GHL_LOCATION_ID=<PLACEHOLDER>
```
**Depende de un Supabase externo** (el dashboard propio del cliente, NO GHL) con tablas `leads`, `appointments`, `ads`, `ad_sets`, `campaigns`, `sellers`. Esta dependencia es específica de cómo este cliente en particular alimentaba su dashboard — una instalación sin ese Supabase necesita otra fuente de leads históricos (ver sección 8).

Comando:
```bash
python3 /root/backfill_prepare.py --dias 30
# --sin-ghl : no consulta la API de GHL (más rápido, pero no filtra dq/booked/conversación)
```

Criterios de exclusión (por lead, aplicados en este orden):
1. **`excluido_cita`**: el lead tiene una cita en `appointments` con `ghl_status` distinto de `cancelled` → no se llama.
2. **`excluido_dq`**: el contacto en GHL tiene algún tag que empiece con alguno de:
   ```python
   DQ_PREFIX = ("dq", "booked", "comprador", "solo whatsapp", "solo-whatsapp",
                "wa-prefiere", "prefiere whatsapp", "cita agendada", "numero-malo")
   ```
3. **`excluido_sin_tel`**: el teléfono no normaliza a 10 dígitos (formato EE. UU.).
4. **`excluido_conversacion`**: el lead ya tiene una conversación real por WhatsApp (escribió DESPUÉS de un mensaje saliente del vendedor/bot — el primer mensaje del lead, el del anuncio, no cuenta). Regla de negocio: esos ya los está convirtiendo el vendedor por WhatsApp, no deben entrar al dialer.
5. **`ghl_error`**: si la consulta a GHL (tags o conversación) no responde correctamente, el lead se cuenta aparte y **no se inyecta a ciegas** (fail-safe).
6. Lo que sobrevive queda en el bucket **`pool`** (se entrega a la cola general de la campaña, tipo `Seguimiento`).

**Ambigüedad detectada en el código**: el docstring del script describe un bucket adicional `"asignado"` (el vendedor de GHL ya le respondió al lead → debería ir a la cola de ESE vendedor). Sin embargo, en el cuerpo del bucle el código siempre hace `bucket = "pool"` de forma fija — nunca evalúa la condición para asignar `"asignado"`. Es decir: **en este kit, tal como está, todos los leads que pasan los filtros terminan en `pool`, aunque el JSON de salida sí incluye el campo `seller_ghl`/`assigned_to_ghl` por si se quiere implementar esa lógica**. Cualquiera que instale este kit y quiera la asignación automática al vendedor que ya respondió tiene que completar esa condición en `backfill_prepare.py`.

Salida: `/root/backfill_leads.json` (lista) + un resumen JSON impreso por consola con conteos por bucket y por campaña.

### 7.2 `backfill_inject.py` — inyecta la lista dentro de Django

**Ambigüedad de rutas detectada**: `backfill_prepare.py` escribe en `/root/backfill_leads.json`, pero `backfill_inject.py` lee de `/tmp/backfill_leads.json`. Son rutas distintas — el kit no copia el archivo automáticamente entre un paso y otro. Hay que hacerlo a mano:
```bash
cp /root/backfill_leads.json /tmp/backfill_leads.json
```

Corre dentro del contenedor/entorno Django:
```bash
BACKFILL_DRY=1 BACKFILL_MODO=asignar \
  python3 manage.py shell -c "exec(open('/tmp/backfill_inject.py').read())"
```
Variables de entorno (leídas con `os.environ`, no de archivo):
- `BACKFILL_DRY` (default `"1"`): con `"1"` solo cuenta qué haría, no escribe nada (dry-run). Poner `"0"` para ejecutar de verdad.
- `BACKFILL_MODO` (default `"asignar"`): `"asignar"` intenta mandar el bucket `"asignado"` a la cola del vendedor mapeado (si está activo en `dialer_agent_crm_map`); `"pool"` manda todo al pool general sin importar el bucket. **Dado el punto anterior (bucket siempre `"pool"` en `prepare.py`), hoy `MODO=asignar` no tiene ningún lead para asignar a un vendedor específico — se comporta igual que `MODO=pool` mientras no se corrija `backfill_prepare.py`.**

Qué hace por cada lead:
- Si el contacto ya tiene dueño permanente (`lead_ownership.get_owner() > 0`), lo cuenta en `con_dueno` — la inyección de todas formas respeta al dueño (vía `_inject_in_preview`).
- Si ya está en cola activa en alguna campaña Preview (`ya_en_cola`), en modo `DRY` no sigue contando como nueva inyección.
- Crea el Contacto interno si no existe, o actualiza sus datos (`nombre`, tipo `"Seguimiento"`).
- Llama a la misma función `_inject_in_preview` que usa el webhook en vivo (reutiliza toda la lógica de prioridad/dueño/campaña-del-dueño).
- Imprime un resumen final: `total`, `inyectados`, `asignados_a_vendedor`, `al_pool`, `ya_en_cola`, `con_dueno`, `errores`, `por_campana`.

Requiere que la tabla `dialer_agent_crm_map(ghl_user_id, agente_id)` ya exista y esté poblada para que `MODO=asignar` funcione (esa tabla no la crea ninguno de estos archivos — es una dependencia externa del kit completo).

---

## 8. Cómo adaptar esto a un CRM distinto de GoHighLevel

| Archivo / función | Específico de GHL — hay que reescribir | Genérico — se puede reusar tal cual |
|---|---|---|
| `crm_webhooks.py` → `CRMLeadActionView` | El "aplanado" de `customData`/`custom_data` (formato propio de cómo GHL manda variables de automatización). El resto de la lógica de negocio (prioridad, dueño, campaña) es agnóstica de CRM. | Toda la lógica de `_inject_in_preview`, `_remove_from_preview`, `TIPO_ORDEN`, `CAMPANAS`, `AUTO_WEIGHTS`, `_resolver_auto` — no llaman a ningún API de CRM. |
| `crm_webhooks.py` → `_ghl_contacto_por_telefono` | 100% GHL (`/contacts/upsert`, `/contacts/{id}/tags`, formato de teléfono `+1<10dig>`). Reescribir completo para el API de contactos del otro CRM. | — |
| `crm_webhooks.py` → `_agente_por_vendedor`, `_campanas_preview_del_agente` | Nombre de columna `ghl_user_id` en `dialer_agent_crm_map` — renombrar a algo genérico (`crm_user_id`) si se quiere prolijidad, pero funcionalmente solo hace un `SELECT` a una tabla propia del dialer. | La lógica de consulta en sí. |
| `crm_webhooks.py` → resto de vistas (`EnLlamadaView`, `ContactoHistorialView`, `SkipLeadView`, `CRMCallOutcomeView`) | Nada — no llaman a ningún CRM, solo a Redis/Postgres/Asterisk internos de OMniLeads. | Reusar sin cambios. |
| `crm_dispositions.py` → `GHL_BASE`, `GHL_VERSION`, todas las llamadas `requests.*` dentro de `_procesar` y `_asignar_on_entrega` | 100% GHL. Hay que reescribir contra el API del otro CRM: cómo se agregan/quitan "tags" (o el concepto equivalente), cómo se agrega una nota, cómo se cambia el "owner"/responsable del contacto. **Ojo**: no todos los CRM tienen el concepto de tags o de nota libre — puede requerir mapear a campos custom. | `TAG_ADD`, `QUITA_*`, `CONTESTO`, `NO_CONTESTO` son solo diccionarios/sets de nombres de disposición → se reusan, solo cambia CÓMO se aplican contra el CRM. |
| `crm_dispositions.py` → gate (`GATE_ACTIVO`, `validar_gate`, `llamada_contestada`, `resumen_llamadas`) | Nada — trabaja sobre `LlamadaLog` interno del dialer, no toca CRM. | Reusar sin cambios. |
| `crm_dispositions.py` → pausa por lead en pantalla | Nada — Asterisk AMI + Redis internos. | Reusar sin cambios. |
| `lead_ownership.py` (completo) | Nada — es 100% interno del dialer (tabla Postgres propia `dialer_lead_owner`), no hace ninguna llamada a CRM. | **Reusar sin cambios.** |
| `backfill_prepare.py` → `ghl_tags`, `tiene_conversacion`, `ghl_get`, `DQ_PREFIX` | 100% GHL (API de contactos y de conversaciones). Reescribir contra el API de "tags"/"mensajes" del otro CRM, o quitar el filtro si el otro CRM no expone eso fácilmente. | La dependencia de Supabase es aparte (ver abajo) — no es de GHL. |
| `backfill_prepare.py` → fuente de leads (Supabase) | No es de GHL, pero SÍ es específica del dashboard propio de este cliente. Una instalación nueva sin ese Supabase necesita reemplazar por completo de dónde se leen `leads`/`appointments`/`ads`/`sellers` (podría ser el propio CRM, otra base, un CSV, etc.). | — |
| `backfill_inject.py` (completo) | Nada — no llama a ningún CRM directamente, solo usa `_inject_in_preview` (interno) y lee de un JSON. | **Reusar sin cambios.** |

Resumen para el agente que adapte esto: lo único que habla directamente con GoHighLevel son `_ghl_contacto_por_telefono` (en `crm_webhooks.py`) y el bloque de `requests.*` dentro de `_procesar`/`_asignar_on_entrega` (en `crm_dispositions.py`), más las funciones de lectura de tags/conversaciones en `backfill_prepare.py`. Todo lo demás — prioridades, dueño permanente, ciclo de colas, pausa por lead en pantalla, gate de contestación — es lógica interna del dialer y no depende de qué CRM esté del otro lado.

---

## 9. Variables de entorno

### Leídas desde `/opt/omnileads/.env_dialer` (dentro del contenedor/host del dialer, parseo línea por línea, NO `os.environ`)

| Variable | Usada en | Para qué |
|---|---|---|
| `DIALER_LIVE` | `crm_webhooks.py` (`_dialer_live()`) | Interruptor de lanzamiento. Si la línea exacta `DIALER_LIVE=1` no está presente, TODOS los inject vía `/api/v1/crm/lead_action/` responden `{"status":"ok","gated":true}` sin hacer nada (excepto `missed_call`, que no pasa por este gate). |
| `GHL_API_TOKEN` | `crm_dispositions.py`, `crm_webhooks.py` | Token Bearer para llamar al API de GHL. |
| `GHL_LOCATION_ID` | `crm_webhooks.py` | `locationId` de GHL, usado en `/contacts/upsert`. |

### Leídas desde `/root/.env_dialer` (en el HOST, para el script de backfill que corre fuera de Django)

| Variable | Para qué |
|---|---|
| `SUPABASE_URL` | Base del REST API de Supabase (dashboard del cliente). |
| `SUPABASE_SERVICE_KEY` | Service key de Supabase (permisos de lectura amplios — tratar como secreto). |
| `GHL_API_TOKEN` | Igual que arriba, para consultar tags/conversaciones durante el backfill. |
| `GHL_LOCATION_ID` | Igual que arriba. |

Nota: son dos archivos distintos (`/opt/omnileads/.env_dialer` vs `/root/.env_dialer`) en dos contextos distintos (contenedor Django vs host). Cada instalación debe decidir si los unifica o los mantiene separados, pero **hay que mantener los valores de `GHL_API_TOKEN`/`GHL_LOCATION_ID` sincronizados entre ambos** si se usan los dos.

### Variables de entorno de proceso (no archivo), solo para `backfill_inject.py`

| Variable | Default | Valores |
|---|---|---|
| `BACKFILL_DRY` | `"1"` | `"1"` = dry-run (solo cuenta), `"0"` = ejecuta de verdad |
| `BACKFILL_MODO` | `"asignar"` | `"asignar"` \| `"pool"` (ver limitación en sección 7.2) |

### Configuración de Django / infraestructura que estos archivos asumen ya existente (no son variables propias de esta integración, pero son dependencias)

- `settings.REDIS_HOSTNAME` y `settings.CONSTANCE_REDIS_CONNECTION['port']`: conexión a Redis usada por `EnLlamadaView`, `pausa_gestion` y el gate de disposición.
- Tabla `dialer_agent_crm_map(ghl_user_id, agente_id)`: mapeo entre usuario de GHL y agente del dialer. No la crea ningún archivo de esta lista — debe existir previamente (poblada manualmente o por otro script del kit).
- `DB_ID = 1` (constante hardcodeada en `crm_webhooks.py` y usada por `backfill_inject.py`): id de la `BaseDatosContacto` de OMniLeads donde viven los contactos de esta integración. **Cada instalación nueva debe verificar/ajustar este número al id real de su base de datos de contactos.**

---
