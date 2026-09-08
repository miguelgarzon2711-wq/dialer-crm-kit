# 01 — Architecture

> Target reader: an AI agent that is going to install or maintain this kit without
> having seen the code before. Everything that follows comes from reading `CLAUDE.md`, `README.md`,
> `server/django/*.py`, `server/scripts/*`, `server/asterisk/*`, and `server/sql/schema.sql`
> in this same repository. Where the code doesn't confirm something, that's stated
> explicitly — nothing is filled in with assumptions. Documents `docs/03` through `docs/09`
> go deeper into each topic; this one is the overall map.

---

## 1. Overview

### 1.1 What OMniLeads is

OMniLeads is an open-source contact center. It's not part of this kit — the kit
gets installed **on top of** an already-working OMniLeads installation. Its pieces,
all running as Docker containers:

- **Asterisk**: the telephony engine. Dials, receives calls, runs the dialplan,
  handles agent queues and the SIP trunk to the provider.
- **Django**: the web application and the REST API. Agent console, business
  logic (campaigns, contacts, dispositions, permissions), and the point where AMI
  (Asterisk Manager Interface) connects Django to Asterisk.
- **PostgreSQL**: the entire database, both OMniLeads' own and the kit's own
  tables.
- **Redis**: real-time state — which agent is in which state, on which call,
  the "families" that Asterisk queries for routing (trunk, outbound routes,
  inbound campaigns).
- **Kamailio**: SIP proxy over WebSocket/TLS. It's the entry point for the
  browser webphone and for the mobile app — neither one talks directly to
  Asterisk.
- **nginx**: reverse proxy. TLS, the web console, static files, and the
  `/ws` path to Kamailio.

### 1.2 What this kit adds

OMniLeads by itself is a generic dialer. This kit connects it to a CRM
(tested with GoHighLevel) and adds the business rules a sales team
needs:

| Módulo | Para qué |
|---|---|
| CRM → dialer webhooks | leads come in on their own, ordered by priority |
| Dialer → CRM disposition engine | tags, notes, and lead owner go back to the CRM on their own |
| Permanent lead owner ("sticky") | a lead who already talked to a sales rep always stays theirs |
| Caller ID rotation | outbound numbers don't get burned as spam |
| AI transcription | automatic notes in the CRM and voicemail auditing |
| REST API for the mobile app | a sales rep without a computer can work from their phone |
| Patch persistence | changes survive a container restart |

### 1.3 Diagram — CRM and SIP provider integration

```
                                   CRM (e.g. GoHighLevel)
                                    |            ^
                          webhooks  |            | REST API
                          (lead enters/          | (tags, notes,
                           leaves queue)          |  owner assignment)
                                    v            |
                        +--------------------------------+
                        |   Django (api_app + this kit's  |
                        |   own modules)                  |
                        +--------------------------------+
                            |                    |
                            | reads/writes       | reads/writes
                            v                    v
                    +---------------+     +---------------+
                    |  PostgreSQL   |     |     Redis      |
                    |  (contacts,   |     |  (live state:  |
                    |   queues,     |     |   agent,       |
                    |   owner,      |     |   call,        |
                    |   audit)      |     |   routes)      |
                    +---------------+     +---------------+
                            ^
                            | AMI (commands: dial, pause, etc.)
                            v
                    +----------------------+
                    |   Asterisk (acd)     |
                    |   dialplan, queues   |
                    +----------------------+
                            |
                            | SIP trunk (PJSIP)
                            v
                    SIP Provider  --------->  Human agent (phone)
```

### 1.4 Diagram — agent clients (browser and mobile app)

```
   Browser (webphone)             Mobile app (sales rep)
          |                             |
          |  HTTPS (console, API)      |  HTTPS (kit's own API,
          |  WSS  (SIP audio)          |   agent_api.py) + WSS (SIP audio)
          v                             v
   +---------------------------------------------+
   |                nginx (TLS)                   |
   +---------------------------------------------+
       |  /api/, web console       |  /ws
       v                           v
   +----------------+      +------------------------+
   |    Django      |      |  Kamailio (SIP proxy   |
   +----------------+      |  over WebSocket/TLS)   |
                            +------------------------+
                                       |
                                       | internal SIP/RTP
                                       v
                            +----------------------+
                            |   Asterisk (acd)      |
                            +----------------------+
```

The browser and the app **never speak SIP directly to Asterisk**: they always
go through Kamailio. The SIP credentials each client uses are ephemeral
(generated by `KamailioService`, see `server/django/agent_api.py`, endpoint
`AppSessionView`).

---

## 2. The containers

Real names found in the kit's scripts. They carry the docker-compose project
prefix (`prod-env-` in this reference installation) — **that prefix can be
different on each server**. Before assuming a name, confirm it with:

```bash
docker ps --format '{{.Names}}'
```

| Contenedor (nombre visto en el kit) | Qué es | Rol en este stack |
|---|---|---|
| `prod-env-django-app-1` | Django (uwsgi) | Web console, OMniLeads' native API, and the endpoints the kit adds (CRM webhooks, dispositions, sticky, mobile app API). Runs with several uwsgi workers (patch `processes=4`, see `restore_patches.sh`). |
| `prod-env-acd-1` | Asterisk | The call engine: dialplan, queues (`queues.conf`), SIP trunk, AMI server. "acd" = Automatic Call Distribution. |
| `prod-env-postgresql-1` | PostgreSQL | The entire database: OMniLeads' native tables + the kit's own 3 tables (`server/sql/schema.sql`). |
| `prod-env-redis-1` | Redis | Live state: connected agents, queues, single session, "lead on screen" pause, CRM notes cache. |
| `prod-env-kamailio-webrtc-1` | Kamailio | SIP proxy over WebSocket/TLS for the browser webphone and the mobile app. |
| `prod-env-nginx-1` | nginx | Reverse proxy: TLS, `/ws` to Kamailio, static files, web console. The real name is looked up dynamically in `restore_patches.sh` (`docker ps | grep nginx`) because it isn't always fixed. |
| `prod-env-dialer-acd-dialplan-1` | OMniLeads' own service (`/app/app.py`) | Bridge between Asterisk and the rest of the system during after-call work (ACW); the kit applies a patch (`_delayed_decr`) for a 5-second race condition. |
| (no fixed name in the kit) | MinIO | Storage for call recordings (`.mp3`, bucket `omnileads`). Referenced by HTTP endpoint (port 9000/9001) and credentials in `server/scripts/transcribe_calls.py`, not by container name. |

Processes that **don't** run inside Docker, but on the host:

| Proceso | Qué es | Dónde se define |
|---|---|---|
| `did-picker` (systemd service) | Its own HTTP server (`server/scripts/did_picker.py`), listens on `10.22.22.1:8055` | Caller ID rotator + inbound campaign resolution + missed call logging |
| Firewall (`iptables`/`DOCKER-USER`) | `server/scripts/firewall.sh`, meant to run as a systemd service | Closes Docker's and the host's internal ports to the outside |
| `redis_watchdog.sh` | Looping daemon (`while true; sleep 15`) | Repairs critical Redis keys if they're lost |
| `watch_django_restart.sh` | Daemon that listens for `docker events` | Re-logs agents into the queues when Django restarts |
| Various crons | `server/scripts/*.sh`, `*.py` | See `docs/07-OPERATIONS.md` for the full table with frequencies |

---

## 3. A lead's journey, end to end

| Paso | Qué pasa | Componente | Archivo del kit |
|---|---|---|---|
| 1 | The lead comes in or changes state in the CRM (form, WhatsApp, call). A CRM automation decides to inject it into the dialer. | CRM (external to the kit) | — |
| 2 | The CRM fires `POST /api/v1/ghl/lead_action/?campana=<clave>` with `action=inject`. The dialer validates, checks the `DIALER_LIVE` switch, resolves the target campaign, computes priority (`TIPO_ORDEN`), and respects the permanent owner if one already exists. | Django | `server/django/crm_webhooks.py` (`CRMLeadActionView`, `_inject_in_preview`) |
| 3 | The lead becomes an `AgenteEnContacto` row in INICIAL state, in the queue for the matching campaign. If it has an owner, the row is already assigned to that agent. | PostgreSQL (native OMniLeads) + `dialer_lead_owner` (kit) | `server/django/lead_ownership.py` (`get_owner`) |
| 4 | The sales rep requests a lead ("Get contact", or the app's single button). OMniLeads' native engine hands over the highest-priority one; the kit assigns it to the rep mapped in the CRM and pauses them in the inbound queues while they have it on screen. | Django + Redis | `server/django/agent_api.py` (`AppLeadView`), `server/django/crm_dispositions.py` (`on_aec_saved`, `pausa_gestion`) |
| 5 | The sales rep calls (console or app). The server first dials the agent's phone and then the lead, normalized to digits only, over the outbound route to the SIP trunk. The rotator picks the caller ID. The lead sees the agent's masked number on their screen. | Django (AMI) → Asterisk → SIP trunk | `server/django/agent_api.py` (`AppLlamarView`), `server/asterisk/extensions_outbound_route.conf`, `server/scripts/did_picker.py` |
| 6 | On hangup, the agent enters ACW (after-call work) and can't dial again until they disposition the call. | Django + Redis | `server/django/agent_api.py` (`AppLlamadaTerminadaView`), native web console |
| 7 | The sales rep saves the disposition. A signal triggers the round trip back to the CRM: tags, a simple note (only for "no contestó"), and the owner cycle (assign if answered, release if not and it has no owner yet). If the disposition proves a conversation happened, the permanent owner is set. | Django (signal `post_save`) | `server/django/crm_dispositions.py` (`on_calificacion_saved`, `_procesar`), `server/django/lead_ownership.py` (`set_owner`) |
| 8 | The result now lives in the CRM (tags, note, `assignedTo`) and in the kit's tables (`dialer_lead_owner`, `dialer_agent_crm_map`). Asterisk logs the call event. | CRM + PostgreSQL | `reportes_app_llamadalog` (native OMniLeads) |
| 9 | A cron checks recent calls, looks up the recording in MinIO, transcribes it with Whisper, detects whether it was a voicemail, drafts a note with GPT, and publishes it to the CRM (and to the kit's own table, so the app can show it instantly). | Cron (host) + OpenAI | `server/scripts/transcribe_calls.py`, `server/scripts/run_transcribe.sh` |
| 10 | If a lead is left "delivered" without being called within 10 minutes, it goes back to the queue: to its owner if it has one, or to the pool with high priority and unassigned from the CRM if not. | Cron (host) | `server/scripts/auto_release_leads.sh` |
| 11 | If the lead calls in before being re-delivered (inbound), Asterisk checks whether it has an owner: if so, it rings only their personal queue; if not, the group. If nobody answers, it gets re-injected as "Llamada Perdida" with the highest priority. | Asterisk + Django | `server/asterisk/extensions_override.conf` (`[from-pstn]`, `[dialer-missed]`), `server/django/crm_webhooks.py` (`CRMMissedCallView`) |

See `docs/03-CRM-INTEGRATION.md` for the field-by-field detail of the webhook,
and `docs/05-TELEPHONY.md` for the detail of outbound/inbound calling.

---

## 4. The kit's modules

| Archivo | Qué hace | De qué depende |
|---|---|---|
| `server/django/crm_webhooks.py` | Entry endpoint `POST /api/v1/ghl/lead_action/`: injects/removes leads, computes priority (`TIPO_ORDEN`), handles appointment reminders and missed calls (`CRMMissedCallView`). | `lead_ownership.py` (owner), `dialer_agent_crm_map` table, native models `Contacto`/`AgenteEnContacto`/`CalificacionCliente` |
| `server/django/crm_dispositions.py` | Dialer → CRM output engine: tags, simple note, owner-assignment cycle in the CRM, "lead on screen" pause (`pausa_gestion`), optional "only counts as answered if it connected" gate (`GATE_ACTIVO`, off by default). | `lead_ownership.py`, `/opt/omnileads/.env_dialer`, `dialer_agent_crm_map` table, `reportes_app.LlamadaLog` |
| `server/django/lead_ownership.py` | Permanent lead owner: sets the first owner, syncs the pool toward the owner, releases leads from inactive agents. | PostgreSQL only, `dialer_lead_owner` table |
| `server/django/agent_api.py` | Full REST API for the mobile app: ephemeral SIP session, get lead, call, release, disposition options, disposition submission, CRM notes (cached), agent state, single browser/phone session, app heartbeat. | `crm_dispositions.py` (pause/gate), native `KamailioService`, Redis, `dialer_call_audit` |
| `server/scripts/did_picker.py` | Internal HTTP server (`10.22.22.1:8055`): picks outbound caller ID (`/pick`), resolves the inbound queue by owner (`/inbound_campana`), logs missed calls (`/missed`). | SQL function `pick_did()` **not included in this kit** (see `docs/05-TELEPHONY.md`), `dialer_lead_owner` table |
| `server/scripts/auto_release_leads.sh` | Releases "delivered" leads not called within more than 10 minutes; unassigns from the CRM if they have no owner. | PostgreSQL, `dialer_lead_owner`, GHL API |
| `server/scripts/sync_lead_owner.sh` | Safety net: reassigns to the owner any stray leads in the pool; closes overdue appointment reminders (+12h). | PostgreSQL, `dialer_lead_owner` |
| `server/scripts/sync_agent_pause.py` | Reconciles the actual pause state in Asterisk against the desired one (lead on screen, app asleep, call in progress) in both directions. | Asterisk (CLI), Redis, PostgreSQL |
| `server/scripts/normalize_phones.sh` | Safety net: cleans up non-dialable phone formats on contacts sitting in the queue. | PostgreSQL |
| `server/scripts/check_did_picker.sh` | `did-picker` watchdog: if it doesn't respond within 3s, restarts the systemd service. | `did_picker.py`, systemd |
| `server/scripts/redis_watchdog.sh` | Daemon that repairs critical Redis keys if a `FLUSHALL`/restart wipes them. | Redis, Django (management shell) |
| `server/scripts/watch_django_restart.sh` | Daemon that re-logs agents into Asterisk's queues when Django restarts. | `docker events`, Django |
| `server/scripts/restore_patches.sh` | Reapplies all of this installation's patches on boot (or manually). It's the heart of persistence. | `/opt/dialer-kit/patches/*` (not included in this repo; each installation builds its own) |
| `server/scripts/sync_agents_pjsip.sh` | Append-only: adds the PJSIP endpoint for any active agent missing from Asterisk. | PostgreSQL, Asterisk |
| `server/scripts/create_agent_inbound.py` / `.sh` | Creates, for each agent, their personal inbound campaign (a clone of a base campaign) with its own virtual DID. | Native OMniLeads models, Redis |
| `server/scripts/inbound_redis_sync.py` | Ensures the personal inbound campaigns' families exist in Redis. | Redis, `/root/agent_inbound.json` |
| `server/scripts/backfill_prepare.py` / `backfill_inject.py` | Initial load of historical leads at launch (reads Supabase/CRM, writes a list; injects it from inside Django). See `docs/09-KNOWN-ISSUES.md` — the intermediate step of copying the file is manual. | Supabase (optional), GHL API, `crm_webhooks.py` |
| `server/scripts/call_report.py` | On-demand call report: contact rate, voicemail vs. human, alerts. | PostgreSQL |
| `server/scripts/transcribe_calls.py` / `run_transcribe.sh` | Transcribes new calls with Whisper, detects voicemail, drafts a note with GPT, and publishes it to the CRM. | MinIO, OpenAI, GHL API, `dialer_call_audit` table |
| `server/scripts/firewall.sh` / `purge_sip_intruders.sh` | Security lockdown: internal ports, blocking external REGISTER, cleanup of intruding SIP registrations. | `iptables`, Asterisk (CLI) |
| `server/sql/schema.sql` | Creates the kit's own 3 tables (idempotent). | PostgreSQL only |
| `server/asterisk/extensions_outbound_route.conf` | Outbound route to the trunk (hand-written because OMniLeads' native generator doesn't build it correctly). | PJSIP trunk configured in OMniLeads |
| `server/asterisk/extensions_override.conf` | Inbound: extracts the real DID from the `To:` header (workaround for the tested provider), detects missed calls, applies number masking toward the agent, and brings in test contexts (`test-trunk`, `test-inbound`, `test-leads`). | `did_picker.py` |
| `server/asterisk/queues.conf.example` | Example of the Preview and Inbound campaign queues from a reference installation — **it's an example, not a file to copy as-is**. | — |

---

## 5. Where the state lives

### 5.1 PostgreSQL

**The kit's own tables** (`server/sql/schema.sql`):

| Tabla | Para qué |
|---|---|
| `dialer_lead_owner` | Permanent lead owner: `contacto_id` → `agente_id`, since when, for which disposition. |
| `dialer_agent_crm_map` | Dialer agent ↔ CRM user mapping. Sales rep column: see note below. |
| `dialer_call_audit` | One row per transcribed call: duration, whether it's a voicemail, language, alert, note drafted by the AI. |

> **Note:** the code (`crm_dispositions.py`, `crm_webhooks.py`, `backfill_inject.py`)
> reads and writes the `ghl_user_id` column of `dialer_agent_crm_map`, but
> `schema.sql` creates that column under the name `crm_user_id`. This is a
> real inconsistency in the kit — see `docs/02-INSTALLATION.md`, common
> problems section, before loading the sales-rep mapping.

**Native OMniLeads tables** the kit reads or writes directly (outside the
ORM, with raw SQL in several scripts):

| Tabla | Para qué la usa el kit |
|---|---|
| `ominicontacto_app_contacto` | The lead: phone, `id_externo` (CRM id), data (name/type in JSON). |
| `ominicontacto_app_agenteencontacto` (AEC) | The row that says "this contact is in this queue, in this state, for this agent." It's the heart of assignment. |
| `ominicontacto_app_calificacioncliente` / `_historicalcalificacioncliente` | The saved disposition and its history (django-simple-history). |
| `ominicontacto_app_opcioncalificacion` | Catalog of dispositions per campaign. |
| `ominicontacto_app_agenteprofile` / `_user` | The agent: SIP extension, active/inactive state, group. |
| `reportes_app_llamadalog` | Events for each call (DIAL, ANSWER, COMPLETEAGENT, etc.) — input for `call_report.py` and `transcribe_calls.py`. |
| `queue_table` / `queue_member_table` | Asterisk queues and their members. |
| `configuracion_telefonia_app_rutaentrante` / `_destinoentrante` | DIDs and which campaign they route to. |

### 5.2 Redis

| Clave (patrón) | Quién la usa | Para qué |
|---|---|---|
| `OML:AGENT:<agente_id>` | Native OMniLeads | Agent's live state: `STATUS`, `CONTACT_NUMBER`, `PAUSE_ID`. The kit reads it to know if they're on a call or in ACW. |
| `OML:CAMP:<id>`, `OML:CAMPAIGN-AGENTS:<id>`, `OML:INR:<did>`, `OML:OUTR:<id>`, `OML:TRUNK:<id>` | Native OMniLeads (regenerated by its own services) | "Families" that Asterisk's dialplan queries in real time to know how to route. `redis_watchdog.sh` watches them and regenerates them if they're missing. |
| `OML:DIALER:PAUSA_GESTION` (set) | Kit | Agents paused in the queues because they have a lead on screen. |
| `OML:DIALER:SESION:<agente_id>` (hash) | Kit | Single browser/app session: which device is active, last heartbeat from the mobile app. |
| `OML:DIALER:NOTAS:<contacto_id>` | Kit | 60s cache of the CRM notes for the mobile app. |

### 5.3 Files

| Archivo | Para qué |
|---|---|
| `/root/.env_dialer` (host) and `/opt/omnileads/.env_dialer` (copy inside the Django container) | Kit credentials. See `docs/02-INSTALLATION.md` on variable names. |
| `/opt/dialer-kit/patches/` | Host-side copy of every file patched inside a container — the source of truth for `restore_patches.sh`. **Not included in this repository**: each installation builds it as it patches. |
| `/var/log/*.log` | One log per script (`restore_patches.log`, `auto_release_entregados.log`, `sync_lead_owner.log`, `did_picker_watchdog.log`, `redis_watchdog.log`, `django_restart_watcher.log`, `transcripciones.log`, `missed_calls.log`, `voicemail_alerts.log`, `skip_leads.log`). Several scripts only print to stdout and rely on the cron redirecting output. |
| `/root/agent_inbound.json`, `/root/backfill_leads.json` | Intermediate state from `create_agent_inbound.sh` and `backfill_prepare.py` respectively. |
| `/var/log/transcripciones_procesadas.json`, `/var/log/transcripts_cache.json` | Deduplication and cache for transcriptions — each audio file gets transcribed exactly once, ever. |
| `/tmp/transcribe_calls.lock` | `flock` so two runs of the transcription cron don't step on each other. |

---

## 6. Architecture decisions and their reasoning

**The server decides, the client only renders.** Neither the web console nor
the mobile app computes priority, owner, or permissions: they ask the server
for data and show buttons. All the business logic lives in `crm_webhooks.py`,
`crm_dispositions.py`, `lead_ownership.py`, and `agent_api.py`. Practical
consequence: a rule gets changed in one single place and shows up the same
way on browser and phone (see the comment at the top of `agent_api.py`: "the
app decides NOTHING, it only renders").

**The lead's number never leaves the server.** The real phone number lives
in the database and in the SIP signaling; what reaches the client (web or
app) is `***-***-1234` (`_enmascarar()` in `agent_api.py`), and in the
webphone's own audio the Caller ID the agent sees is also masked
(`[mask-agent]` in `extensions_override.conf`). The sales rep dials by
contact id, never by number. This is what stops someone from walking off
with the phone number database on their phone.

**Patches get reapplied on boot.** OMniLeads' containers get recreated
(reboot, `docker restart`, upgrade) and lose any change made directly with
`docker exec`. `restore_patches.sh` always follows the same pattern — check
(`grep`/`test -f`) → if missing, copy from the host and reload the minimum →
log it — and it's scheduled with `@reboot`. It's idempotent: running it extra
times never breaks anything.

**Automations self-heal in both directions.** No cron assumes a previous
command got through: it compares actual state against desired state and
corrects in both directions. Explicit example in the code itself
(`sync_agent_pause.py`): if an agent has a lead on screen and isn't paused,
it pauses them; if they have no lead and are still paused, it releases them.
Without this symmetry, any restart of Django, Asterisk, or Redis leaves the
system in a half-broken state that nobody notices until someone asks why
they aren't getting any work.
