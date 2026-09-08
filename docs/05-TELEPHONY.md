# 05 — Telephony

This document explains how a call travels through the kit, how caller ID
rotation works, how inbound calls are routed by salesperson, and the
classic trunk configuration mistakes. It is written from the actual code of
the kit (`server/asterisk/*.conf`, `server/scripts/did_picker.py` and
related files). Where the code references something that **is not included**
in this kit, it is marked explicitly — missing content is not invented.

---

## 1. Call architecture

### 1.1 Outbound (agent → lead's cell phone)

```
Agent presses "Call" (web console or app)
        │
        ▼
Django (AgentActivityAmiManager) — orders Asterisk via AMI to originate the call
        │
        ▼
Asterisk runs the Preview campaign dialplan
        │  (the context that decides the Caller ID runs BEFORE dialing — see 1.3)
        ▼
Outbound route context  [oml-outr-1]   (server/asterisk/extensions_outbound_route.conf)
  - matches the dialed number against 3 patterns:
      _XXXXXXXXXX      (10 digits)                -> pattern order 1
      _1XXXXXXXXXX     (11 digits, with "1")       -> pattern order 2
      _+1XXXXXXXXXX    (11 digits, with "+1")      -> pattern order 2
  - Gosub(sub-oml-dialout,s,1(1, <order>))  -> native OMniLeads subroutine
    that reads the configured trunk from AstDB/Redis (${DB(OML/OUTR/1/NAME)},
    OML:OUTR / OML:TRUNK families) and builds the Dial() to the PJSIP trunk.
  - if no pattern matches: exten `i` sets DIALSTATUS=NONDIALPLAN and hangs up
    with "no route for <number>" (Gosub sub-oml-hangup).
        │
        ▼
SIP trunk (PJSIP) to the provider (tested with Telnyx)
        │
        ▼
Carrier / operator → lead's cell phone
```

The generic context `[oml-outr]` (same file) includes `[oml-outr-1]` and is
used by manual calls ("Call outside campaign") and external transfers — if
it is not defined, those two features fail silently with the same
"no route" error.

### 1.2 Inbound (lead's or operator's cell phone → agent)

```
SIP INVITE enters the trunk
        │
        ▼
Context [from-pstn]   (server/asterisk/extensions_override.conf)
  - Telnyx workaround: the INVITE's Request-URI sometimes does not carry the
    DID (it arrives as sip:user@ or sip:s@), so the real DID is pulled from
    the To: header (CUT on "@" and on ":").
  - If the To: header starts with "+", it is trimmed off.
  - Stores the real caller in __DLRCALLER and attaches a hangup handler
    (dialer-missed) to detect calls that go UNANSWERED at hangup.
  - CURLs did-picker: GET /inbound_campana?from=<caller>
      - if it returns a DID  -> Goto(oml-dial-in, <DID>, 1)
      - if it returns nothing -> Goto(oml-dial-in, <original DID>, 1)
        ▼
oml-dial-in (native to OMniLeads, not included in this kit) resolves the DID
against RutaEntrante/DestinoEntrante and rings the corresponding queue/campaign
(the owning salesperson's personal line, or the group's — see section 5).
        ▼
Agent's softphone/hardphone (via Kamailio if it is a WSS webphone, or direct PJSIP)
```

If the call did NOT end in `CONNECT` state (nobody answered, or the caller
hung up while waiting), the `[dialer-missed]` hangup handler fires
`GET /missed?from=<caller>`, which makes the lead enter the queue as
"Missed Call" (see 2.4).

### 1.3 Where the rotated Caller ID comes in

`did_picker.py` exposes `/pick?tel=<number>` to choose the outbound Caller ID
(section 2). `restore_patches.sh` itself references a patch called
**"sticky+ramp caller ID in dialplan"** on a file named
`oml_extensions_precall.conf` (marker `PATCH CID`) — meaning the context
that calls `/pick` before dialing **is not included in this kit**: only its
destination path and the fact that it exists are referenced. Whoever
installs the kit has to write (or bring from elsewhere) that "precall"
context, which must:

1. Call `CURL(http://10.22.22.1:8055/pick?tel=${OMLOUTNUM})`.
2. Use the result as `CALLERID(num)` before the `Dial()` to the trunk.

> ⚠️ **Real ambiguity:** neither the `oml_extensions_precall.conf` context nor
> the SQL function `pick_did()` (see section 2) are in this kit. Only the
> HTTP server that wraps them (`did_picker.py`) is present, plus evidence
> that something calls them (comments in `restore_patches.sh`).

---

## 2. Caller ID rotation (`did_picker.py`)

This is a minimal HTTP server (`http.server`, no frameworks) that listens on
**`10.22.22.1:8055`** — an internal IP that must never be exposed to the
internet (see section 7). It runs as the systemd service `did-picker`.

### 2.1 What `pick(tel)` does — `/pick` endpoint

```python
tel = re.sub(r'\D', '', tel)[-10:]     # keeps the last 10 digits
if len(tel) < 10: return ''
SELECT pick_did('<tel>');              # via docker exec psql
```

Normalizes the phone number to 10 digits and delegates the ENTIRE decision
to the PostgreSQL function `pick_did(tel)`.

> ⚠️ **`pick_did()` is not included in `server/sql/schema.sql` or any other
> file in this kit.** The module's docstring says
> *"sticky+cap+ramp"* and the patch names (`sticky+ramp caller ID`) confirm
> the intent, but the **actual algorithm lives in an SQL function that has
> to be written separately**. From the usage contract it can be inferred
> that it must:
> - **Sticky per lead:** always return the same DID for the same `tel` as
>   long as that DID remains active (so the lead recognizes who is calling).
> - **Daily cap per number:** not choose a DID that already reached its
>   daily dialing quota.
> - **Warm-up ramp:** limit how much a newly added DID is used, raising the
>   cap gradually over days.
>
> This is an inference from the name and usage, **not a fact verified in
> code**. Whoever installs the kit must write this function (or obtain the
> full version) before rotation works.

### 2.2 `inbound_campana(caller)` — `/inbound_campana` endpoint

Implements "sticky by salesperson" inbound routing (detail in section 5):

1. Looks up `dialer_lead_owner` to see if the phone number already has an
   owner → if so, builds the virtual DID `900001<agent_id 2 digits>` and
   returns it.
2. If there is no owner, looks up the campaign (1 to 5) the contact was most
   recently assigned to (`ominicontacto_app_agenteencontacto`) and returns
   the group virtual DID `9000000<campaign>`.
3. If nothing matches, returns `''` (the dialplan uses the original DID).

### 2.3 `missed(caller)` — `/missed` endpoint

Reads the token from `/root/.env_dialer` (line `DIALER_API_TOKEN=`) and does
`POST https://<dialer-domain>/api/v1/dialer/missed_call/` with
`{"from": tel}` and `Authorization: Bearer <token>`. Logs success or error
in `/var/log/missed_calls.log`. The domain in the code is a placeholder
(`dialer.example.com`) — it must be pointed to the actual installation's
domain.

### 2.4 How the dialplan queries it (CURL)

The pattern used by `extensions_override.conf` (and assumed to be the same
for `/pick`, even though that context is not in the kit) is always the same:

```
same => n,Set(CURLOPT(conntimeout)=2)
same => n,Set(CURLOPT(timeout)=2)
same => n,Set(DLRINB=${CURL(http://10.22.22.1:8055/inbound_campana?from=${CALLERID(num)})})
same => n,ExecIf($["${DLRINB}" != ""]?Goto(oml-dial-in,${DLRINB},1))
```

Timeouts are short (2s) on purpose: if `did-picker` does not respond, the
call must be able to continue with the original number/route instead of
staying stuck waiting.

### 2.5 Why it matters

A number that dials the same mobile networks too frequently gets flagged as
**"Spam Likely"** by the carriers/phones themselves — from then on, the
answer rate drops even if the salesperson does everything right. Rotation
with a daily cap spreads volume across several DIDs, and "sticky" prevents
the same lead from seeing a different number every time they get called
(which also breeds distrust).

---

## 3. Sizing the number pool

The math is simple once the daily cap per number is known:

```
numbers needed = outbound dials per day ÷ daily cap per number
```

> ⚠️ The **daily cap per number** and the **warm-up ramp** values live
> inside `pick_did()`, which is not in this kit (see 2.1). There is no
> concrete number to document here without inventing it — these must be
> defined according to the SIP provider's policy (Telnyx and other carriers
> recommend gradually ramping up a new number's volume over 1–2 weeks
> before taking it to full cap) and hardcoded into `pick_did()`.

General warm-up recommendations (not specific to this kit, industry common
sense):
- Start a new number with a low cap and raise it day by day.
- Do not put several new numbers into rotation on the same day — stagger them.
- Watch the answer rate per number (see the provider's reputation/CNAM
  notes) to pull out of rotation any number that starts to drop.

---

## 4. Phone number normalization (`normalize_phones.sh`)

**Why it exists:** Asterisk dials digits, nothing else. If a number is left
with a `+`, a space, a dash, or the country "1" stuck on without being
normalized, **the call dies silently**: no exception, no error log, the
salesperson just sees that "nothing happened" and assumes the number does
not exist.

**What the script does** (runs every 10 minutes per its own comment, and
also during the launch pre-check):

1. Looks for contacts that are IN QUEUE (`estado IN (0,1,3)` in
   `ominicontacto_app_agenteencontacto`) whose phone does not match
   `^[0-9]{10}$`.
2. Strips everything that is not a digit. If the result is 11 digits and
   starts with `1` (USA country code), that `1` is stripped.
3. If the cleaned result IS 10 digits, the contact is updated and it logs
   `contacto <id>: '<before>' -> '<after>'`.
4. What could not be fixed (not a 10-digit USA number) is reported
   separately with `WARNING contact <id> queued with an undialable phone: '<tel>'`
   — it stays in the queue, but someone has to review it by hand.

The script only prints to stdout — it does not write to a log file by
itself; whoever schedules it via cron must redirect the output (see
07-OPERATIONS).

---

## 5. Inbound by salesperson ("sticky" applies to inbound too)

For each active agent, the kit creates a **personal inbound campaign** that
clones the base campaign `id=7` ("Inbound G1"): same qualification options,
same CRM parameters, same supervisors, but with a single member (that
agent) and its own virtual DID.

### 5.1 `create_agent_inbound.py` (runs inside the Django container)

For each active `AgenteProfile`:
- Campaign name: `Inbound A<id> <username>`.
- Virtual DID: `900001<agent id, 2 digits>` (e.g. agent id 7 → `90000107`).
- Clones `Campana` id 7 → new `Campana` (same `OpcionCalificacion` and
  `ParametrosCrm`, so the disposition/CRM engine recognizes them).
- Copies the supervisors from campaign 7 (detects the column dynamically).
- Creates `queue_table` (wait=20s) and `queue_member_table` for that agent.
- Creates `DestinoEntrante` (type=1, pointing to the campaign) and
  `RutaEntrante` with the virtual DID.
- At the end calls `RegenerarAsteriskFamilysOML().regenerar_asterisk()` to
  push the Redis families that Asterisk uses in real time.
- It is **idempotent**: each step first checks whether it already exists
  before creating it.
- Prints `RESULT:<json>` with `[{agente_id, username, campana_id, queue, did, ruta_id}, ...]`.

### 5.2 `create_agent_inbound.sh` (orchestrator, run on the host)

```
docker cp create_agent_inbound.py  -> Django container
manage.py shell -c "exec(...)"     -> runs the script above
extracts RESULT: -> /root/agent_inbound.json
python3 inbound_redis_sync.py      -> ensures Redis (see 5.3)
adds missing blocks to queues.conf (one per new campaign)
docker cp queues.conf -> Asterisk as oml_queues_override.conf
asterisk -rx "queue reload all"
```

It is run **manually after creating new agents** (it is not a cron job).

### 5.3 `inbound_redis_sync.py`

Ensures the following exist in Redis, based on `/root/agent_inbound.json`:
- `OML:CAMP:<id>` — cloned from `OML:CAMP:7` with its own `QNAME`,
  `SHOWCAMPNAME` and `QUEUETIME=20` (only if it did not already exist).
- `OML:INR:<did>` — `NAME`, `DST=1,<campana_id>`, `ID=<ruta_id>`, `LANG=es`
  (always refreshed).
- `OML:CAMPAIGN-AGENTS:<id>` — adds the agent as a member (only if the set
  did not already exist).

It is called both by `create_agent_inbound.sh` and by `restore_patches.sh`
(if `/root/agent_inbound.json` exists), so it survives a Redis restart.

### 5.4 How the selector routes

`inbound_campana()` in `did_picker.py` decides, in this order:
1. **Does the caller have an owner?** (`dialer_lead_owner`) → returns the
   owner's personal DID `900001<agent_id>` → **only that personal queue
   rings**.
2. **If there is no owner** → looks up the campaign (1–5) the contact was
   most recently assigned to → returns the group DID `9000000<campaign>`
   → the group's shared queue rings (any available agent picks up).

---

## 6. Number masking

The salesperson **never sees the lead's full number**. This is an explicit
product decision in the code (comments say "MASCARA"). The real number
only travels to the trunk/carrier and stays intact in the records
(`LlamadaLog`, recordings).

It is masked in three layers:

| Where | File | What it does |
|---|---|---|
| **Webphone** (leg toward the agent) | `[mask-agent]` in `extensions_override.conf` | Before dialing toward the agent, if the contact is identified (`OMLCODCLI` set and valid), replaces `CALLERID` and `CONNECTEDLINE` with `"Lead ***-***-<last 4>" <XXXXXX+last 4>`. If unidentified (a number typed by hand, or an inbound call from an unknown number), the real number passes through. |
| **Card / form in the web console** | `views_campana_preview.py.patched`, `forms_base.py.patched`, `views_calificacion_cliente.py.patched`, `views_agente.py.patched` (referenced in `restore_patches.sh`, marker `PATCH MASCARA`) | According to the persistence script's comments, they mask the number on the Preview card and on the disposition form. **These patched files are not included in this kit** (only their destination path and their marker are referenced) — the exact implementation detail cannot be documented. |
| **API for mobile clients** | `_enmascarar()` in `server/django/agent_api.py` | `'***-***-' + telefono[-4:]` — used when returning the lead's context (`telefono_contacto`) to the salesperson's app. This one is complete in the kit. |

---

## 7. SIP security

**The real risk:** a SIP port (5060/5160) open to the internet without
authentication lets anyone register a softphone impersonating an agent
(`REGISTER` to an extension in the 1100–1110 range) and originate calls that
get billed to the dialer owner's account. This has already happened in
production (see `firewall.sh`, comment from 2026-09-05).

### `firewall.sh`

Runs idempotent `iptables` rules (`-C` to check, `-I` to insert if missing):

1. **OMniLeads internal ports** — must never be public:
   `1440 4573 4730 5038 6379 7088 8000 8098 8099 8888 9000 9001 9191 22223`
   → `DROP` in `DOCKER-USER` for traffic entering via the WAN interface
   (`enp1s0` in the example — **adjust to the server's actual interface
   name**). This only blocks what comes in from the internet; internal
   traffic between containers does not go through that interface and is
   not affected.
2. **Host service ports** (dashboard, etc.):
   `8001 8002 3001 6379 5038 9000 9001` → `DROP` in `INPUT` from the WAN.
3. **Anti external registration:** blocks `REGISTER sip:` packets on port
   5060 from the internet (string match, `INPUT` and `DOCKER-USER`, tcp and
   udp) and blocks port **5160 entirely** from the internet — because
   Telnyx never sends `REGISTER` (only inbound INVITE), and legitimate
   agents come in through Kamailio on loopback (`127.0.0.1`), not directly
   from outside.

Per the kit's own `CLAUDE.md`, this script must be left as a systemd
service so it survives reboots.

### `purge_sip_intruders.sh`

Reactive cleanup, to run if intruders already got in, or as a periodic
check:

1. Lists `database show registrar/contact` in Asterisk, filters out the
   ones that do **not** have `"via_addr":"127.0.0.1"` (i.e. did not come
   through Kamailio) and deletes them one by one
   (`database del registrar/contact <key>`).
2. Reports how many external contacts (range 10xx/11xx) remain.
3. Shows the status of extension 1100 as a spot check.

This script is a cleanup tool — the one that prevents it from happening
again is `firewall.sh`. Use both together.

---

## 8. Trunk configuration

Classic mistakes, taken directly from the code:

| Mistake | Where it shows | How to avoid it |
|---|---|---|
| **Endpoint name set wrong** | `[test-trunk]` in `extensions_override.conf` sets `Dial(PJSIP/+15551000001@telnyx,30)` — the endpoint is literally named `telnyx` (the name configured in the OMniLeads trunk), **without** a separate `from_user` | The PJSIP endpoint must be named exactly as configured in OMniLeads; there is no need (and it is not correct) to set `from_user` separately |
| **Non-E.164 / inconsistent format** | The 3 patterns of `[oml-outr-1]`: `_XXXXXXXXXX`, `_1XXXXXXXXXX`, `_+1XXXXXXXXXX` | All three must be covered in the outbound route (10 digits, 11 with "1", or with "+1") — if any is missing, that number format falls to `exten => i` and dies with "no route" |
| **Outbound route context that has to be created by hand** | Comment on the first line of `extensions_outbound_route.conf`: *"broken generator: written by hand"* | OMniLeads' native outbound route generator does not build this file correctly — it has to be written/copied by hand as `oml_extensions_outr_override.conf` (via `restore_patches.sh`) |
| **Missing generic context `[oml-outr]`** | Same file | Without it, "Call outside campaign" and external transfers have nowhere to go |

**Before considering telephony done:** test with a real call —
`channel originate Local/s@test-trunk application Wait 30` tests the
outbound trunk with a test Caller ID; `channel originate Local/s@test-inbound
application Wait 40` tests inbound routing. The `[test-leads]` context
provides three ready-made test numbers to validate audio without calling a
real lead:

| Test number | Behavior |
|---|---|
| `5550000001` | Answers, echoes your own voice back (validates round-trip audio) |
| `5550000002` | Rings 12s and falls to voicemail |
| `5550000003` | Rings 30s and nobody answers |
