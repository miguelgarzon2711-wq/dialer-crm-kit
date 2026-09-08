# CRM Integration (GoHighLevel) ↔ Dialer

> Target reader: an AI agent that is going to install this kit for ANOTHER client, without having seen the source code.
> Source: `server/django/crm_webhooks.py`, `server/django/crm_dispositions.py`, `server/django/lead_ownership.py`, `server/scripts/backfill_prepare.py`, `server/scripts/backfill_inject.py`.
> Everything marked `<PLACEHOLDER>` is defined by each installation. Where the code is ambiguous or looks incomplete, that is stated explicitly — it is not filled in with assumptions.

---

## 1. Flow diagram (text)

```
 1. LEAD ENTERS THE CRM (GoHighLevel)
    - Ad / form / WhatsApp / inbound call -> a Contact is created or
      updated in GHL.
    - A GHL automation (workflow) decides that this contact must enter
      the dialer and fires an outbound HTTP POST webhook.

 2. GHL WEBHOOK -> DIALER
    - POST to  /api/v1/crm/lead_action/?campana=<key>  with action=inject
      (see section 2 for the exact JSON).
    - The dialer:
        a) validates action + ghl_id,
        b) checks the DIALER_LIVE switch (if it is off, it responds
           "ok, gated" and does NOT do anything — meant for before
           launch; those leads are picked up later by the backfill),
        c) resolves which Preview campaign (agent group) the lead goes
           to,
        d) creates or updates the internal OMniLeads Contact (by
           id_externo = ghl_id),
        e) calculates the priority (TIPO_ORDEN, section 4) and decides
           whether the lead already has an "owner" (sticky, section 5)
           to force this injection into their queue instead of the
           general pool,
        f) inserts/updates the AgenteEnContacto (AEC) record that
           represents "this contact is in this campaign's queue".

 3. THE LEAD SITS IN QUEUE (Preview)
    - OMniLeads hands the AEC to the first free agent in that campaign
      (or, if it has an owner, straight to them) respecting priority
      order.

 4. THE SALES REP CALLS
    - The agent hits "Get contact" (the AEC moves to state ENTREGADO
      [DELIVERED]): at that moment the dialer already assigns the lead
      to the seller mapped in GHL (assignedTo), BEFORE any disposition
      exists — so "Go to CRM" from GHL always shows that lead.
    - Asterisk dials. If answered, the AEC moves to ASIGNADO [ASSIGNED]
      (active call).

 5. DISPOSITION (the seller qualifies the call)
    - A CalificacionCliente is saved in OMniLeads. A post_save signal
      fires EVERYTHING that goes back to the CRM (section 6):
        - tags added/removed on the GHL contact,
        - a note on the contact (only for "No contesto" (didn't answer)
          / "Numero equivocado" (wrong number)),
        - assignedTo (owner in GHL): confirmed to the seller if the
          disposition counts as "answered", or cleared (null) if it
          didn't answer and the lead still has no permanent owner,
        - if the disposition proves there was a real conversation, the
          lead's "owner for life" is set (sticky, section 5) — only
          the FIRST time.

 6. BACK TO THE CRM
    - All the changes from 5) are written to GHL via its REST API
      (tags, notes, assignedTo). The lead keeps living in GHL as the
      source of truth for business data; the dialer only orchestrates
      the call queue and reflects the outcome.

 7. RE-ENTRIES INTO THE DIALER
    - If the lead writes on WhatsApp again, doesn't answer and needs a
      retry, has an appointment booked, or called and wasn't answered,
      GHL fires the same inject webhook again (or another type) and
      the cycle repeats from step 2, respecting the permanent owner if
      one already exists.
```

Notes on the diagram:
- The dialer (OMniLeads) is NOT the owner of the lead's data — GHL is. The dialer only stores the minimum needed to dial (phone, name, GHL external id, type/priority).
- Communication runs in both directions over HTTP: GHL → dialer via webhooks configured in GHL automations; dialer → GHL via direct calls to the GHL REST API (`https://services.leadconnectorhq.com`) made by the Django backend when a disposition is saved.

---

## 2. Main endpoint: `POST /api/v1/crm/lead_action/`

View: `CRMLeadActionView` in `crm_webhooks.py`.

### Authentication
`SessionAuthentication` or `ExpiringTokenAuthentication` (OMniLeads Bearer token). The expected header is:
```
Authorization: Bearer <TOKEN_OML>
```
The code does not show how that token is generated/validated — it is OMniLeads' standard token authentication mechanism (`ExpiringTokenAuthentication`), not something specific to this integration.

### Query params
- `campana` (optional if it comes in the body): key of the destination campaign. See the `CAMPANAS` table below. Also accepts the special value `auto`.

### Body JSON — fields

GHL can send the automation's "Custom Data" nested under a `customData` (or `custom_data`) key. If the body doesn't carry `action` at the top level, the endpoint looks for that key, "flattens" it (merges its fields with the top level) and continues. In other words: both a flat body and one with `customData: {...}` work.

```jsonc
{
  "action": "inject",              // "inject" | "remove" — OBLIGATORIO
  "ghl_id": "<ID_CONTACTO_GHL>",    // OBLIGATORIO siempre
  "phone": "<PLACEHOLDER_10_DIGITOS>", // REQUIRED only if the contact is new to the dialer
  "nombre": "<NOMBRE_LEAD>",        // opcional
  "tipo": "Nuevo Lead",             // ver sección 4 — condiciona prioridad y reglas
  "campana": "grupo1",              // alternativa a pasarlo por query string

  // Solo si tipo = "Recordatorio Cita":
  "cita_inicio": "<FECHA_HORA_CITA>",   // texto libre, se intenta parsear (ver formatos abajo)
  "vendedor_ghl": "<ID_USUARIO_GHL>",   // used to resolve which agent owns the appointment
  "vendedor_email": "<EMAIL_VENDEDOR>"  // alternativa a vendedor_ghl
}
```

Field details:
- **`action`** (string, required): `"inject"` to put/update the lead in queue, `"remove"` to take it out.
- **`ghl_id`** (string, required): id of the contact in GHL. It is the key the dialer uses to find/create the internal Contact (`Contacto.id_externo`).
- **`phone`**: required ONLY when there is no internal Contact yet with that `ghl_id` (a lead new to the dialer). It is normalized to 10 digits (the `1` prefix is stripped if it comes as 11 digits, US E.164 style). If, after normalizing, it doesn't end up as exactly 10 digits, the endpoint responds with a 400 error. **This normalizer assumes US format (10 digits)** — if the new client is in another country, `_normalize_phone()` needs to be adapted.
- **`nombre`**: lead's name. If the contact already exists and `nombre` is not sent, the previously stored name is kept.
- **`tipo`**: defines delivery priority and triggers special rules (section 4). The values documented in the endpoint's original docstring are `"Nuevo Lead"` (new lead), `"WA Respondio"` (WA replied), `"Callback"`, `"Seguimiento"` (follow-up); the code also explicitly handles `"Recordatorio Cita"` (appointment reminder). `"WA Cita Pendiente"` (WA pending appointment) and `"Llamada Perdida"` (missed call) exist as internal types that the dialer itself assigns automatically in certain cases (they are not documented as values GHL should send directly for a normal inject, although technically, if that string is sent, it gets the corresponding order).
- **`campana`**: the campaign key (see table below) or `"auto"`. If it doesn't come by query param or body and it isn't `"auto"`, and the key isn't in the mapping, it responds 400.
- **`cita_inicio`, `vendedor_ghl`, `vendedor_email`**: only relevant when `tipo == "Recordatorio Cita"` (see special rules below).

### Campaign mapping (`CAMPANAS`)

```python
CAMPANAS = {
    "grupo1": 1,
    "grupo2": 2,
    "grupo3": 3,
    "mixto":  4,
    "v25":    5,
}
```
This translates the stable key used by GHL automations into an internal OMniLeads `campana_id`. **If the client reorganizes its agent groups, only this dictionary needs to change — GHL URLs/automations are not touched.** Every new installation must redefine this dictionary with its own Preview campaign keys/ids.

`campana=auto`: if no campaign is specified, the dialer distributes the lead only among the campaigns listed in `AUTO_WEIGHTS` (weight = number of agents in that group), sending it to the group with the lowest pending queue per agent. If the contact was already in one of those campaigns before, it repeats the same one (consistency). Every installation must redefine `AUTO_WEIGHTS` with its own campaigns/weights.

### Special rules inside `inject`

- **Automatic upgrade `WA Respondio` → `WA Cita Pendiente`**: if that contact's last disposition in that campaign was `"Va a agendar"`, a new inject with `tipo="WA Respondio"` is internally rewritten to `"WA Cita Pendiente"` (higher priority, sticky to the same agent).
- **`tipo="Recordatorio Cita"`**:
  - If `cita_inicio` has already passed (interpreted against the `America/New_York` timezone — **adjust this timezone to each client's market**), the endpoint does NOT inject anything and responds `{"status":"skipped","reason":"cita ya paso", "cita_inicio": ...}` with HTTP 200.
  - If the lead doesn't have an "owner" yet (sticky, section 5), the endpoint tries to resolve the owner of the appointment from `vendedor_ghl` or `vendedor_email` (table `dialer_agent_crm_map`, see section 5). If it resolves it, it sets that agent as the permanent owner. If it CANNOT resolve it, it does NOT inject and responds `{"status":"skipped","reason":"sin dueno: manda vendedor_ghl o vendedor_email"}` with HTTP 200.
  - If the lead has no owner and this type arrives without being able to resolve an agent, it also isn't injected (see `_inject_in_preview`: with no owner, a `"Recordatorio Cita"` is never put in queue under any circumstance — it is a hard rule: the reminder is only for the owner of the appointment).
  - Date formats the parser tries (in order), before falling back to a generic parser (`dateutil`): ISO with/without offset, `YYYY-MM-DD HH:MM[:SS]`, `Mon DD, YYYY HH:MM AM/PM` (variants with day of week, with/without comma, with "at"), `MM/DD/YYYY HH:MM`, `DD/MM/YYYY HH:MM`, `YYYY-MM-DD`. If nothing matches, the raw value is logged (`logger.warning`) so the real format GHL sends can be adjusted.

### Responses

| Case | HTTP | Body |
|---|---|---|
| Missing `action` or `ghl_id` | 400 | `{"error": "action y ghl_id son requeridos"}` |
| Invalid or missing `campana` (and it isn't `auto`) | 400 | `{"error": "campana invalida o faltante", "validas": [...claves..., "auto"]}` |
| New contact without a valid phone (10 digits) | 400 | `{"error": "phone valido (10 dig) requerido para contacto nuevo"}` |
| Dialer not "live" yet (`DIALER_LIVE` off) | 200 | `{"status": "ok", "gated": true}` — nothing was done |
| `Recordatorio Cita` with a past date | 200 | `{"status": "skipped", "reason": "cita ya paso", "cita_inicio": "<received value>"}` |
| `Recordatorio Cita` with no owner and no resolvable seller | 200 | `{"status": "skipped", "reason": "sin dueno: manda vendedor_ghl o vendedor_email"}` |
| Inject succeeded | 200 | `{"status": "ok", "contact_id": <id interno>, "campana": "<clave>", "orden": <prioridad numérica>}` |
| Inject failed (internal exception) | 500 | `{"error": "inject failed"}` |
| `remove` with contact not found | 404 | `{"error": "contacto no encontrado"}` |
| `remove` succeeded | 200 | `{"status": "ok", "contact_id": <id>, "campanas": [<ids removidos>]}` |
| `remove` failed | 500 | `{"error": "remove failed"}` |
| Invalid `action` (not inject/remove) | 400 | `{"error": "action invalido"}` |

`remove` with no `campana` specified takes the contact out of ALL campaigns in the mapping (useful for "booked"/"DQ": the lead must no longer stay in any queue). `remove` respects AECs in the ASIGNADO state (active call: not touched).

---

## 3. Other endpoints

### `POST /api/v1/dialer/missed_call/`
View: `CRMMissedCallView`. **Does not go through the `DIALER_LIVE` gate** (it always acts).

What it's for: when an inbound call comes into the dialer and nobody answers, the lead is re-injected into the queue with maximum priority (type `"Llamada Perdida"`, order 0) so the call gets returned as soon as possible.

Expected body/query:
```json
{ "from": "<TELEFONO_QUE_LLAMO>" }
```
(also accepts `?from=...` as a query param). The phone is normalized to 10 digits; if it doesn't meet that, it responds 400 `{"error": "from invalido"}`.

Behavior:
- Looks up internal contacts with that phone; if there are several, it prioritizes the one that already has an "owner" (sticky).
- If the contact doesn't exist in GHL (or its `id_externo` starts with `DEMO`), it looks it up/creates it in GHL by phone (`POST /contacts/upsert`); if it's a new contact in GHL it sets `firstName="Llamada perdida <telefono>"` and `source="Llamada perdida (dialer)"`. In any case it adds the `llamada-perdida` tag.
- If the contact didn't exist in the dialer, it creates it. The destination campaign is the last one that contact had activity in (or `grupo1` by default if it never had any).
- Injects with `tipo="Llamada Perdida"` (order 0 — maximum priority) respecting the owner if it has one.

Response:
```json
{"status": "ok", "contacto_id": 123, "campana": 1, "dueno": -1}
```
(`dueno` is the permanent owner's `agente_id`, or `-1` if it has none). `status` can be `"error"` if the injection failed, with the rest of the fields the same.

### `GET /api/v1/agente/call_outcome/?contacto_id=<ID>`
View: `CRMCallOutcomeView`. Used by the agent's disposition form to decide which qualification options to show (for example, only offering "Didn't answer"/"Wrong number" if the call didn't connect).

- If the logged-in user has no agent profile, or no valid `contacto_id` is sent: `{"answered": true, "attempts": 0}`.
- If it does: it delegates to `crm_dispositions.resumen_llamadas(agente_id, contacto_id)`.
  - **Important:** in this kit, the "gate" that checks against answered calls is **disabled by default** (`GATE_ACTIVO = False`, see section 6). While it's disabled, this endpoint ALWAYS responds `{"answered": true, "attempts": 0, "last_event": null, "last_duration": null, "gate": "off"}`, without querying anything.
  - If the gate is enabled, the real response is `{"answered": <bool>, "attempts": <number of DIAL attempts>, "last_event": "<last event in the call log>", "last_duration": <duration of the last COMPLETEAGENT/COMPLETEOUTNUM, or null>}`, calculated over a 3-hour window (`GATE_VENTANA`).

### `GET /api/v1/contact_history/?contacto_id=<ID>`
View: `ContactoHistorialView`. Returns that contact's last 10 dispositions (to show them in the preview panel before calling).

Response:
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
Dates are formatted in the `America/New_York` timezone (**adjust according to the client's market**). If no `contacto_id` is sent, it responds `{"history": []}`. If `contacto_id` is not numeric, it responds 400.

### `GET /api/v1/agente/en_llamada/`
View: `EnLlamadaView`. Says whether the logged-in agent has an active call right now (used to control the disposition screen flow).

Response: `{"en_llamada": true|false}`. Internally it reads the Redis key `OML:AGENT:<agente_id>` (fields `CONTACT_NUMBER` and `STATUS`); it's considered "in call" if `CONTACT_NUMBER` is set and `STATUS` doesn't contain `"ACW"` (after-call-work). If the user has no agent profile, it responds `false`.

### `POST /api/v1/agente/skip_lead/`
View: `SkipLeadView`. **It is disabled in this kit**: `post()` starts with:
```python
logger.warning(...)
return Response({"error": "Saltar leads esta desactivado"}, status=403)
```
and all the code below (which would implement "skipping" a `WA Respondio`/`WA Cita Pendiente` lead without marking or disposing it) is unreachable — it stays as design reference, not active functionality. If a new installation wants to re-enable this feature, that early `return` needs to be deleted and the logic below reviewed (it validates `contacto_id`/`campana_id`/`razon`, blocks if there's an active call (`ASIGNADO`), only allows `WA Respondio`/`WA Cita Pendiente` types, marks the AEC as `FINALIZADO` and logs an entry to `/var/log/skip_leads.log`).

---

## 4. Lead types and priorities (`TIPO_ORDEN`)

```python
TIPO_ORDEN = {
    "Recordatorio Cita": 0,   # appointment day (morning + 1h before) -> ONLY to the rep who owns it
    "Llamada Perdida":   1,   # the lead called in and nobody answered
    "WA Cita Pendiente": 2,   # ya habló, quedó de confirmar hora y escribió por WA
    "Callback":          3,   # "call me back at five"
    "WA Respondio":      4,   # respondió por WA y no está asignado
    "Nuevo Lead":        5,
    "Seguimiento":       6,
}
```

- **Lower number = higher priority = delivered first.**
- Any `tipo` not in the dictionary receives order `6` by default (same level as `Seguimiento`, the lowest documented).
- Business reasoning behind the order (per the code's comments): an already-booked appointment (reminder) is the most urgent and only serves the owner; a missed call has to be returned right away; a lead that's almost at appointment stage but still needs a time confirmed is more urgent than one that just wrote in; a scheduled callback has a committed time; an unassigned "WA replied" lead is more urgent than a cold new lead; follow-up is the lowest priority.
- **Priority-0 shielding** (`TIPOS_PROTEGIDOS = ("Recordatorio Cita", "Llamada Perdida", "WA Cita Pendiente")`): if a contact is already in queue with one of these types and it was updated less than 24 hours ago, a later injection of LOWER priority (e.g. a `"Nuevo Lead"` that arrives because the contact was recreated in GHL) does NOT downgrade it — the protected type/order is kept.
- **One Preview campaign at a time**: when a contact is injected into a campaign, any pending queue entry (`ESTADO_INICIAL`) it had in the OTHER campaigns in the mapping gets closed (moves to `FINALIZADO`). A lead is never left "duplicated" in two queues.

**How to change priorities**: edit the `TIPO_ORDEN` dictionary directly in `crm_webhooks.py`. It's a static mapping — there is no database configuration or panel. If new types are added, add them here with their number; if they aren't added, they will fall to `6` (last priority) via `.get(tipo, 6)`.

---

## 5. Lead assignment cycle (owner / sticky)

There are **two different "owner" concepts that work together**:

1. **Permanent internal dialer owner** (`lead_ownership.py`, table `dialer_lead_owner`): determines which agent that lead's QUEUE is delivered to within OMniLeads.
2. **`assignedTo` (owner) inside GHL**: GHL's native field that shows who is the responsible seller for the contact in the CRM. It is updated from `crm_dispositions.py` on every disposition.

### 5.1 When the permanent owner (sticky) is set

It is set the FIRST time a disposition with `agente_id > 0` has a name within:
```python
DISPOS_CONVERSACION = {
    "Agendo cita", "Va a agendar", "Llamada de vuelta programada", "Prefiere WhatsApp",
    "Solo queria precio", "Colgo", "Error mio de ventas", "No interesado", "Ya compro",
}
```
It's stored in the table `dialer_lead_owner(contacto_id PK, agente_id, since, motivo)` with `INSERT ... ON CONFLICT (contacto_id) DO NOTHING` — meaning **whoever "conversed" with the lead first wins forever**; later dispositions (even from another agent) do NOT change the owner.

The owner can also be set for `tipo="Recordatorio Cita"` with no prior owner, by resolving the seller via `vendedor_ghl`/`vendedor_email` against the table `dialer_agent_crm_map` (section 2).

### 5.2 What "forever" means

- The owner is **NOT lost** even if the agent is marked inactive or deleted from the dialer (`lead_ownership.agente_inactivo()` is used to decide whether to HAND OUT new assignments to that agent, but it does not delete the existing owner record).
- Every subsequent injection of that contact — whether `Seguimiento`, `Nuevo Lead`, `WA Respondio`, `Callback`, `WA Cita Pendiente`, in any campaign — is delivered ONLY to the owner (`_inject_in_preview` checks `lead_ownership.get_owner()` first, before any other assignment rule).
- If the owner is not in the Preview campaign where the injection arrived (for example, they changed groups), the lead is moved to a campaign where the owner IS present (code: "PATCH CAMPANA DEL DUEÑO").
- If an AEC goes back to the pool (`agente_id = -1`, state `INICIAL`) for any reason (releasing the contact, logout, etc.), a signal immediately returns it to the owner if one exists. There is also a sweep function (`sync_pool()` / `sync_contacto()`) meant to run periodically as a safety net.

### 5.3 Disposition sets used for the `assignedTo` cycle in GHL

```python
CONTESTO = {
    "Agendo cita", "Va a agendar", "Llamada de vuelta programada", "Prefiere WhatsApp",
    "Solo queria precio", "No interesado", "Ya compro", "Colgo", "Error mio de ventas",
}
NO_CONTESTO = {"No contesto", "Numero equivocado"}
```
**Explicit ambiguity note**: `CONTESTO` and `DISPOS_CONVERSACION` (section 5.1) contain exactly the same 9 names — they are two separate variables in two different files that today have the same content. If a disposition is added to or removed from one list, check whether the other should also change; the code doesn't derive one from the other.

There is a third set, similar but NOT identical, in `crm_webhooks.py`:
```python
STICKY_SI_HABLARON = {
    "Agendo cita", "Va a agendar", "Llamada de vuelta programada", "Prefiere WhatsApp",
    "Solo queria precio", "Colgo", "Error mio de ventas",
}
```
It's the same as `CONTESTO`/`DISPOS_CONVERSACION` but **without** "No interesado" (not interested) or "Ya compro" (already purchased). It's used only to decide whether a `WA Respondio` with no owner in the pool should go straight to the last agent who conversed with that lead (`_agent_si_conversaron`): it makes sense to exclude those two because they're closed outcomes (not interested / already bought elsewhere) where "recovering" the same seller doesn't apply. It's documented exactly as it is in the code, without assuming whether it's intentional or not for every new installation.

### 5.4 When `assignedTo` is assigned / unassigned in GHL

- **On "Get contact"** (AEC moves to state `ENTREGADO`): GHL's `assignedTo` is immediately set to the mapped seller of the agent who took it (table `dialer_agent_crm_map`), BEFORE any disposition exists. This is so "Go to CRM" from the lead always shows it assigned to whoever is working it.
- **On saving the disposition**:
  - If the disposition is in `CONTESTO`: `assignedTo` is confirmed to the mapped seller of the **permanent owner** if one already exists, otherwise the agent who disposed it.
  - If the disposition is in `NO_CONTESTO` **and the lead still has NO permanent owner**: `assignedTo` is cleared (`null`) in GHL — the lead goes back to being "without a seller" in the CRM.
  - If the disposition is in `NO_CONTESTO` but the lead **already has an owner**: it is NOT unassigned (the owner stays as the responsible party in GHL even if this time they didn't answer).
  - If the agent has no row in `dialer_agent_crm_map`, a warning is logged and `assignedTo` is not touched.

### 5.5 Related behavior: pause for "lead on screen"

Not strictly CRM, but it's part of the same file: while an agent has an AEC in `ENTREGADO` or `ASIGNADO` (lead on screen, not yet disposed), the dialer pauses them in the Asterisk queues so inbound calls don't reach them; it unpauses when finished. There's a toggle (`PAUSA_GESTION_ACTIVA`) and a sweep function (`revisar_pausas_gestion`) meant to run every minute as a safeguard against a stuck agent.

---

## 6. What gets written back to the CRM on each disposition

All of this happens in `crm_dispositions.py`, triggered by the `post_save` signal on `CalificacionCliente`, in a separate thread (it doesn't block saving the disposition). It requires the contact to have `id_externo` (ghl_id) set; if not, it does nothing.

### 6.1 Tags that get added (`TAG_ADD`)

| Disposition | Tag(s) added |
|---|---|
| Agendo cita (booked appointment) | `cita-agendada-dialer` |
| Va a agendar (going to book) | `va-a-agendar` |
| Llamada de vuelta programada (scheduled callback) | `llamar-mas-tarde` |
| Prefiere WhatsApp (prefers WhatsApp) | `prefiere-wa` |
| No interesado (not interested) | `dq-no-interesado` |
| Ya compro (already purchased) | `dq-ya-compro` |
| Numero equivocado | `numero-malo` |

The dispositions "Solo queria precio" (just wanted pricing), "Colgo" (hung up), "Error mio de ventas" (sales rep error) and "No contesto" **don't add any tag** (they don't appear in `TAG_ADD`).

### 6.2 Tags that get removed

- `llamar-mas-tarde` is removed on EVERY disposition **except** when the disposition being saved is exactly `"Llamada de vuelta programada"`. Along with that tag, `callback` and `requested callback` are also removed (tags that GHL's calendar workflow sets when the lead books a callback).
- `va-a-agendar` is removed on EVERY disposition **except** when the disposition is `"Va a agendar"`.

### 6.3 Notes on the contact

An automatic note is only added when the disposition is `"No contesto"` or `"Numero equivocado"`:
```
<Disposition> - <Agent name>
```
(uses the telephone emoji, U+1F4DE). For the rest of the dispositions **no note is written from this file** — the code comment clarifies that those are covered by another mechanism (transcription/Whisper) that is outside the scope of these files.

### 6.4 Owner change (`assignedTo`)

See section 5.4 (same mechanism, it's the same `_procesar` function).

### 6.5 GHL endpoints used

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

## 7. Backfill (loading old leads at launch)

Two separate scripts, meant to run once at launch (or whenever leads that went unworked before the dialer was active need to be recovered).

### 7.1 `backfill_prepare.py` — builds the list, does NOT inject anything

Runs on the dialer **host** (outside Django), reads variables from `/root/.env_dialer`:
```
SUPABASE_URL=<PLACEHOLDER>
SUPABASE_SERVICE_KEY=<PLACEHOLDER>
GHL_API_TOKEN=<PLACEHOLDER>
GHL_LOCATION_ID=<PLACEHOLDER>
```
**Depends on an external Supabase** (the client's own dashboard, NOT GHL) with tables `leads`, `appointments`, `ads`, `ad_sets`, `campaigns`, `sellers`. This dependency is specific to how this particular client fed its dashboard — an installation without that Supabase needs another source for historical leads (see section 8).

Command:
```bash
python3 /root/backfill_prepare.py --dias 30
# --sin-ghl : skips the CRM API (faster, but does not filter dq/booked/conversation)
```

Exclusion criteria (per lead, applied in this order):
1. **`excluido_cita`**: the lead has an appointment in `appointments` with `ghl_status` different from `cancelled` → not called.
2. **`excluido_dq`**: the GHL contact has some tag starting with any of:
   ```python
   DQ_PREFIX = ("dq", "booked", "comprador", "solo whatsapp", "solo-whatsapp",
                "wa-prefiere", "prefiere whatsapp", "cita agendada", "numero-malo")
   ```
3. **`excluido_sin_tel`**: the phone doesn't normalize to 10 digits (US format).
4. **`excluido_conversacion`**: the lead already has a real WhatsApp conversation (they wrote AFTER an outbound message from the seller/bot — the lead's first message, the one from the ad, doesn't count). Business rule: those are already being converted by the seller via WhatsApp, they shouldn't enter the dialer.
5. **`ghl_error`**: if the GHL query (tags or conversation) doesn't respond correctly, the lead is counted separately and **is not blindly injected** (fail-safe).
6. What survives ends up in the **`pool`** bucket (delivered to the campaign's general queue, type `Seguimiento`).

**Ambiguity detected in the code**: the script's docstring describes an additional `"asignado"` bucket (the GHL seller already replied to the lead → it should go to THAT seller's queue). However, in the loop body the code always does `bucket = "pool"` unconditionally — it never evaluates the condition to assign `"asignado"`. In other words: **in this kit, as it stands, every lead that passes the filters ends up in `pool`, even though the output JSON does include the `seller_ghl`/`assigned_to_ghl` field in case someone wants to implement that logic**. Anyone installing this kit who wants automatic assignment to the seller who already replied has to complete that condition in `backfill_prepare.py`.

Output: `/root/backfill_leads.json` (list) + a summary JSON printed to the console with counts per bucket and per campaign.

### 7.2 `backfill_inject.py` — injects the list from inside Django

**Path ambiguity detected**: `backfill_prepare.py` writes to `/root/backfill_leads.json`, but `backfill_inject.py` reads from `/tmp/backfill_leads.json`. These are different paths — the kit doesn't copy the file automatically between one step and the other. It has to be done by hand:
```bash
cp /root/backfill_leads.json /tmp/backfill_leads.json
```

Runs inside the Django container/environment:
```bash
BACKFILL_DRY=1 BACKFILL_MODO=asignar \
  python3 manage.py shell -c "exec(open('/tmp/backfill_inject.py').read())"
```
Environment variables (read with `os.environ`, not from a file):
- `BACKFILL_DRY` (default `"1"`): with `"1"` it only counts what it would do, doesn't write anything (dry run). Set to `"0"` to actually execute.
- `BACKFILL_MODO` (default `"asignar"`): `"asignar"` tries to send the `"asignado"` bucket to the mapped seller's queue (if active in `dialer_agent_crm_map`); `"pool"` sends everything to the general pool regardless of bucket. **Given the previous point (bucket always `"pool"` in `prepare.py`), today `MODO=asignar` has no lead to assign to a specific seller — it behaves the same as `MODO=pool` while `backfill_prepare.py` isn't fixed.**

What it does per lead:
- If the contact already has a permanent owner (`lead_ownership.get_owner() > 0`), it's counted in `con_dueno` — the injection still respects the owner (via `_inject_in_preview`).
- If it's already in an active queue in some Preview campaign (`ya_en_cola`), in `DRY` mode it doesn't keep counting it as a new injection.
- Creates the internal Contact if it doesn't exist, or updates its data (`nombre`, type `"Seguimiento"`).
- Calls the same `_inject_in_preview` function the live webhook uses (reuses all the priority/owner/owner's-campaign logic).
- Prints a final summary: `total`, `inyectados`, `asignados_a_vendedor`, `al_pool`, `ya_en_cola`, `con_dueno`, `errores`, `por_campana`.

Requires the table `dialer_agent_crm_map(ghl_user_id, agente_id)` to already exist and be populated for `MODO=asignar` to work (that table isn't created by any of these files — it's an external dependency of the full kit).

---

## 8. How to adapt this to a CRM other than GoHighLevel

| File / function | GHL-specific — needs rewriting | Generic — can be reused as is |
|---|---|---|
| `crm_webhooks.py` → `CRMLeadActionView` | The "flattening" of `customData`/`custom_data` (GHL's own format for sending automation variables). The rest of the business logic (priority, owner, campaign) is CRM-agnostic. | All of `_inject_in_preview`, `_remove_from_preview`, `TIPO_ORDEN`, `CAMPANAS`, `AUTO_WEIGHTS`, `_resolver_auto` — none of them call any CRM API. |
| `crm_webhooks.py` → `_ghl_contacto_por_telefono` | 100% GHL (`/contacts/upsert`, `/contacts/{id}/tags`, phone format `+1<10dig>`). Rewrite completely for the other CRM's contacts API. | — |
| `crm_webhooks.py` → `_agente_por_vendedor`, `_campanas_preview_del_agente` | Column name `ghl_user_id` in `dialer_agent_crm_map` — rename to something generic (`crm_user_id`) for tidiness if desired, but functionally it just does a `SELECT` against the dialer's own table. | The query logic itself. |
| `crm_webhooks.py` → remaining views (`EnLlamadaView`, `ContactoHistorialView`, `SkipLeadView`, `CRMCallOutcomeView`) | Nothing — they don't call any CRM, only OMniLeads' internal Redis/Postgres/Asterisk. | Reuse unchanged. |
| `crm_dispositions.py` → `GHL_BASE`, `GHL_VERSION`, all the `requests.*` calls inside `_procesar` and `_asignar_on_entrega` | 100% GHL. Has to be rewritten against the other CRM's API: how "tags" (or the equivalent concept) are added/removed, how a note is added, how the contact's "owner"/responsible party is changed. **Careful**: not every CRM has the concept of tags or a free-text note — it may require mapping to custom fields. | `TAG_ADD`, `QUITA_*`, `CONTESTO`, `NO_CONTESTO` are just dictionaries/sets of disposition names → reusable, only HOW they are applied against the CRM changes. |
| `crm_dispositions.py` → gate (`GATE_ACTIVO`, `validar_gate`, `llamada_contestada`, `resumen_llamadas`) | Nothing — it works on the dialer's internal `LlamadaLog`, doesn't touch the CRM. | Reuse unchanged. |
| `crm_dispositions.py` → pause for lead on screen | Nothing — internal Asterisk AMI + Redis. | Reuse unchanged. |
| `lead_ownership.py` (complete) | Nothing — it's 100% internal to the dialer (its own Postgres table `dialer_lead_owner`), it makes no CRM calls at all. | **Reuse unchanged.** |
| `backfill_prepare.py` → `ghl_tags`, `tiene_conversacion`, `ghl_get`, `DQ_PREFIX` | 100% GHL (contacts and conversations API). Rewrite against the other CRM's "tags"/"messages" API, or drop the filter if the other CRM doesn't expose that easily. | The Supabase dependency is separate (see below) — it's not GHL's. |
| `backfill_prepare.py` → lead source (Supabase) | Not GHL's, but IS specific to this client's own dashboard. A new installation without that Supabase needs to completely replace where `leads`/`appointments`/`ads`/`sellers` are read from (could be the CRM itself, another database, a CSV, etc.). | — |
| `backfill_inject.py` (complete) | Nothing — it doesn't call any CRM directly, it only uses `_inject_in_preview` (internal) and reads from a JSON. | **Reuse unchanged.** |

Summary for the agent adapting this: the only things that talk directly to GoHighLevel are `_ghl_contacto_por_telefono` (in `crm_webhooks.py`) and the block of `requests.*` calls inside `_procesar`/`_asignar_on_entrega` (in `crm_dispositions.py`), plus the tag/conversation reading functions in `backfill_prepare.py`. Everything else — priorities, permanent owner, queue cycle, pause for lead on screen, answer gate — is internal dialer logic and doesn't depend on which CRM is on the other side.

---

## 9. Environment variables

### Read from `/opt/omnileads/.env_dialer` (inside the dialer's container/host, parsed line by line, NOT `os.environ`)

| Variable | Used in | What for |
|---|---|---|
| `DIALER_LIVE` | `crm_webhooks.py` (`_dialer_live()`) | Launch switch. If the exact line `DIALER_LIVE=1` isn't present, ALL injects via `/api/v1/crm/lead_action/` respond `{"status":"ok","gated":true}` without doing anything (except `missed_call`, which doesn't go through this gate). |
| `GHL_API_TOKEN` | `crm_dispositions.py`, `crm_webhooks.py` | Bearer token for calling the GHL API. |
| `GHL_LOCATION_ID` | `crm_webhooks.py` | GHL's `locationId`, used in `/contacts/upsert`. |

### Read from `/root/.env_dialer` (on the HOST, for the backfill script that runs outside Django)

| Variable | What for |
|---|---|
| `SUPABASE_URL` | Base URL of the Supabase REST API (client's dashboard). |
| `SUPABASE_SERVICE_KEY` | Supabase service key (broad read permissions — treat as a secret). |
| `GHL_API_TOKEN` | Same as above, to query tags/conversations during the backfill. |
| `GHL_LOCATION_ID` | Same as above. |

Note: these are two different files (`/opt/omnileads/.env_dialer` vs `/root/.env_dialer`) in two different contexts (Django container vs host). Each installation must decide whether to unify them or keep them separate, but **the `GHL_API_TOKEN`/`GHL_LOCATION_ID` values must stay in sync between both** if both are used.

### Process environment variables (not file-based), only for `backfill_inject.py`

| Variable | Default | Values |
|---|---|---|
| `BACKFILL_DRY` | `"1"` | `"1"` = dry-run (only counts), `"0"` = actually executes |
| `BACKFILL_MODO` | `"asignar"` | `"asignar"` \| `"pool"` (see limitation in section 7.2) |

### Django/infrastructure configuration these files assume already exists (not variables of this integration itself, but dependencies)

- `settings.REDIS_HOSTNAME` and `settings.CONSTANCE_REDIS_CONNECTION['port']`: Redis connection used by `EnLlamadaView`, `pausa_gestion` and the disposition gate.
- Table `dialer_agent_crm_map(ghl_user_id, agente_id)`: mapping between the GHL user and the dialer agent. It isn't created by any file in this list — it must already exist (populated manually or by another script in the kit).
- `DB_ID = 1` (constant hardcoded in `crm_webhooks.py` and used by `backfill_inject.py`): id of the OMniLeads `BaseDatosContacto` where this integration's contacts live. **Every new installation must verify/adjust this number to its actual contact database id.**

---
