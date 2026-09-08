# 07 — Operations

How the dialer stays alive day to day: what runs on its own, how patches
survive a restart, what rules can't be broken while operating, what the
watchdogs monitor, and the concrete commands used to diagnose a problem.
Everything below comes from the real code in `server/scripts/`. Where a value
(frequency, container name, network interface) depends on the specific
installation, it's marked as such — no number is invented that the code
doesn't state.

---

## 1. Scheduled task table

| Script | Type | Frequency (per the code itself) | What it does | Log |
|---|---|---|---|---|
| `normalize_phones.sh` | cron | Every 10 min (script comment) + during launch pre-check | Cleans up phones with a non-dialable format on contacts IN QUEUE; reports the ones it couldn't fix | Only prints to stdout — **writes no log file of its own**; whoever schedules it must redirect the output |
| `sync_agent_pause.py` | cron | Every minute (script comment) | Reconciles Asterisk's actual pause state vs. what the agent should have (lead on screen, sleeping app) in both directions | Only prints to stdout — redirect in the cron entry |
| `auto_release_leads.sh` | cron | Not specified in the script (only the threshold: DELIVERED contacts older than 10 min) — define the cadence per your needs, e.g. every 1–5 min | Releases abandoned DELIVERED contacts: without an owner they go back to the pool with priority 1 and get unassigned in the CRM; with an owner they go back to the owner | `/var/log/auto_release_leads.log` |
| `sync_lead_owner.sh` | cron | Not specified in the script — define per your needs | Reassigns to the pool leads from contacts that already have an owner; closes overdue Appointment Reminders (>12h without calling) | `/var/log/sync_lead_owner.log` |
| `check_did_picker.sh` | cron | Not specified in the script — define per your needs (critical service: frequent is advisable) | Tests `GET /pick?tel=123`; if it doesn't respond in 3s, restarts the `did-picker` systemd service | `/var/log/did_picker_watchdog.log` (only if it restarted) |
| `redis_watchdog.sh` | **daemon** (`while true; sleep 15`), not cron | Own 15s loop | Checks critical Redis keys (outbound and inbound) and regenerates them if missing (see section 4) | `/var/log/redis_watchdog.log` |
| `watch_django_restart.sh` | **daemon** (`docker events` streaming), not cron | Reacts to events, not an interval | Detects the Django container starting up and re-logs agents into the Asterisk queues 45s later | `/var/log/django_restart_watcher.log` |
| `restore_patches.sh` | `@reboot` (per the kit's `CLAUDE.md`: `@reboot sleep 90 && bash restore_patches.sh`) | On server startup (can also be run by hand) | Reapplies any patches that got lost (see section 2) | `/var/log/restore_patches.log` (most blocks; some don't log) |
| `sync_agents_pjsip.sh` | Run from `restore_patches.sh` (at the end, always) | Every time `restore_patches.sh` runs | Adds (append-only) the PJSIP endpoint for any active agent that's missing one in `oml_pjsip_agents.conf` | Prints to stdout; `restore_patches.sh` redirects it to its own log |
| `firewall.sh` | systemd service at boot (per `CLAUDE.md`), not cron | On startup / on demand | Closes internal ports and blocks external REGISTER/5160 (see `05-TELEPHONY.md` section 7) | Only prints to stdout |
| `purge_sip_intruders.sh` | Manual / on demand | Ad-hoc (initial hardening or on suspicion) | Deletes SIP registrations that don't come from Kamailio (127.0.0.1) | Only prints to stdout |
| `create_agent_inbound.sh` | Manual | "After creating new agents" (script comment) | Creates/ensures each active agent's personal inbound campaign | `/root/create_agent_inbound.out`, `/root/agent_inbound.json` + stdout |

> Scripts that only "print to stdout" have no log path written in the code
> itself — whether the log persists depends on how they're scheduled
> (`>> /var/log/something.log` on the cron line, or the systemd journal if
> they're services).

---

## 2. Patch persistence (`restore_patches.sh`)

### The problem it solves

OMniLeads containers get restarted (server reboot, `docker restart`,
upgrades) and **any change made with a direct `docker exec` on the
container's filesystem is lost** at that point. The symptom is confusing:
something that was "already fixed" fails again on its own, with nobody
having touched anything on purpose.

### The idempotent pattern

Each block in the script follows the same shape:

```bash
# 1) MARK: is the patch already applied?
if ! docker exec <container> grep -q '<unique patch text>' <path inside container> 2>/dev/null; then
  # 2) MISSING -> copy the good version from the host and reload the minimum necessary
  docker cp /opt/dialer-kit/patches/<file>.patched <container>:<destination path>
  docker restart <container>            # or the specific reload: asterisk -rx 'dialplan reload',
                                          # 'module reload app_queue.so', nginx -s reload, pjsip reload
  echo "$(date): <description> restored" >> /var/log/restore_patches.log
fi
```

The mark can be a `grep` for a distinctive string inside a file, or directly
a `test -f` on a file that doesn't exist in stock OMniLeads. If the mark is
already there, the block does nothing — that's why it's safe to run the
whole script on every restart, or even by hand at any time, with no side
effects.

Two blocks break the pattern on purpose, by design:

- **`sync_agents_pjsip.sh`** — always gets called, with no mark, because it's
  *append-only*: it only adds missing PJSIP endpoints, never rewrites or
  deletes, so running it extra times can't break anything.
- **`inbound_redis_sync.py`** — gets called if `/root/agent_inbound.json`
  exists (there's no mark inside a container because what it ensures are
  Redis keys, not a file).

### How to add a new patch — step by step

1. Make the change on a copy of the file and **leave that copy on the host**,
   in `/opt/dialer-kit/patches/` (never edit only inside the container).
2. Add a unique piece of text into the file that doesn't exist in the
   original OMniLeads version (e.g. a `# PATCH MY-THING` comment) — that's
   your mark.
3. Validate syntax BEFORE touching the container:
   ```bash
   python3 -c "import ast; ast.parse(open('file.py').read())" && echo OK
   bash -n script.sh && echo OK
   ```
4. Add a new block at the end of `restore_patches.sh` with the shape above:
   mark check → `docker cp` → minimum reload → log.
5. Run the script twice in a row: the first time it should apply and log; the
   second time it should do nothing (that's how you confirm it's idempotent).
6. Test that it really survives: `docker restart <container>` (or restart
   the whole server) and confirm `restore_patches.sh` reapplies it on its
   own.

---

## 3. Hard operating rules

| Rule | Why |
|---|---|
| **Never `docker compose up`** | OMniLeads brings up containers with a hash prefix; `compose up` kills and recreates them, and everything inside is lost |
| **Always individual operations**: `docker exec`, `docker cp`, `docker restart <name>`, `docker start/stop` | They change only what's needed without touching the rest of the containers |
| **Validate syntax before copying into the container** (`ast.parse` for Python, `bash -n` for shell) | A Python file with a syntax error brings down all of Django |
| **Keep a local copy of anything edited inside a container** (in `/opt/dialer-kit/patches/`) | If there's no copy outside, the next restart erases the change with no warning |
| **Never touch nginx routing** (proxy/rules) — cache headers only | A routing change leaves the webphone with no signal (`/ws` 502) and nobody can call |

---

## 4. Monitoring (watchdogs)

| Watchdog | What it checks | What symptom it covers |
|---|---|---|
| `redis_watchdog.sh` (15s loop) | Existence of `OML:OUTR:1`, `OML:TRUNK:2`, `OML:CAMP:9` (outbound families) and `OML:INR:<DID>` (inbound family) in Redis. If missing, it runs from Django `RutaSalienteFamily().regenerar_families()`, `TrunkFamily().regenerar_families()`, `RegenerarAsteriskFamilysOML().regenerar_asterisk()` (outbound) or `SincronizadorDeConfiguracionTelefonicaEnAsterisk().sincronizar_en_asterisk()` (inbound) | A Redis `FLUSHALL`/restart that wipes the in-memory routing tables used by the real-time dialplan — without this, calls fail with "no route" even though the database configuration is perfect |
| `check_did_picker.sh` | `GET /pick?tel=123` responds in under 3s | The `did_picker.py` Python process hung or down — would silently break Caller ID rotation and inbound routing, because the dialplan depends on that CURL answering |
| `watch_django_restart.sh` | Restarts of the `prod-env-django-app-1` container (via `docker events`) | Django restarting kicks agents out of the Asterisk queues (membership lives in AMI sessions managed by Django) — the agent sees the console "connected" but doesn't get calls. **Note:** the list of agent IDs to re-log-in is *hardcoded* in the script (`[2, 4]` in this copy) — it needs to be updated when new agents are added, or replaced with a dynamic query against the active-agents table |
| `sync_agent_pause.py` (every minute) | Compares actual Asterisk pause vs. expected state: lead on screen, own flag in `OML:DIALER:PAUSA_GESTION`, call in progress (`OML:AGENT:*`), app heartbeat (`OML:DIALER:SESION:*`) | Agents left paused too long (blocking their queue) or unpaused too long (a call comes in while they have a lead on screen); phone apps that stopped heartbeating 3+ minutes ago and are still "connected" with nobody attending them |

---

## 5. Diagnosis

### "Why didn't the call come in?" (inbound)

```bash
# is the did-picker service alive and responding?
curl -s --max-time 3 'http://10.22.22.1:8055/pick?tel=123'
systemctl status did-picker

# what DID/queue is this inbound call being routed to?
curl 'http://10.22.22.1:8055/inbound_campana?from=<10-digit-number>'

# are there external SIP registrations (not from Kamailio) that could be interfering?
docker exec prod-env-acd-1 asterisk -rx "database show registrar/contact"
docker exec prod-env-acd-1 asterisk -rx "pjsip show contacts"

# does the lead already have an owner? (same query did_picker.py does)
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads -tAc \
  "SELECT r.telefono FROM dialer_lead_owner o \
   JOIN ominicontacto_app_contacto c ON c.id=o.contacto_id \
   JOIN configuracion_telefonia_app_rutaentrante r ON r.telefono='900001'||lpad(o.agente_id::text,2,'0') \
   WHERE c.telefono='<10-digit-number>' ORDER BY o.since DESC LIMIT 1;"

# did it get logged as a missed call?
tail -n 50 /var/log/missed_calls.log

# reload the dialplan if a context was just touched
docker exec prod-env-acd-1 asterisk -rx "dialplan reload"
```

### "Why isn't the agent getting leads?"

```bash
# is he paused in Asterisk? for how long?
docker exec prod-env-acd-1 asterisk -rx "queue show"
# look for the "... (paused was N secs ago)" line for the agent

# did the system itself set the pause? (if it's here, sync_agent_pause.py will lift it on its own)
docker exec prod-env-redis-1 redis-cli SMEMBERS OML:DIALER:PAUSA_GESTION

# does he have a delivered, unqualified lead stuck on screen?
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads -tAc \
  "SELECT * FROM ominicontacto_app_agenteencontacto WHERE agente_id=<id> AND estado IN (1,3);"

# did the app stop heartbeating? (phone heartbeat)
docker exec prod-env-redis-1 redis-cli HGETALL OML:DIALER:SESION:<agente_id>
# check 'ultimo_visto' and 'dispositivo' (if it says 'app_dormida', it reconnects on its own once heartbeats resume)

# does he have a PJSIP endpoint created in Asterisk? (can be missing after recreating the container)
docker exec prod-env-acd-1 asterisk -rx "pjsip show endpoint <extension>"
# if missing:
bash /root/sync_agents_pjsip.sh

# run the reconciler by hand and see what it would do
python3 server/scripts/sync_agent_pause.py
```

### "Why is there no audio?"

```bash
# typical "SIP Proxy not responding" error in the webphone -> Kamailio TLS certificates
docker exec prod-env-kamailio-webrtc-1 grep 'certs/cert.pem' /etc/kamailio/tls.cfg

# /ws returning 502 -> nginx's TLS config for the proxy towards Kamailio
docker exec prod-env-nginx-1 grep proxy_ssl_protocols /etc/nginx/conf.d/environment/oml_env.conf

# if either is missing, restore_patches.sh reapplies them:
bash /opt/dialer-kit/scripts/restore_patches.sh

# test audio without depending on a real lead (kit's test contexts)
# 5550000001 answers and echoes your own voice back -> validates two-way audio
# channel originate Local/s@test-trunk application Wait 30      (real outbound via the trunk)
# channel originate Local/s@test-inbound application Wait 40    (inbound, routed to a queue)
```

> For audio that "cuts out" on calls that do connect (RTP/NAT), this kit
> doesn't ship its own diagnostic script — it's solved with Asterisk's
> standard tools (e.g. `rtp set debug on` from the Asterisk console), which
> aren't part of this kit but of Asterisk in general.

---

## 6. Logs

| File | Written by | What to look for there |
|---|---|---|
| `/var/log/missed_calls.log` | `did_picker.py` (`missed()`) | One line per unanswered inbound call forwarded to `/api/v1/dialer/missed_call/`, with the result (`ok` or `ERROR <detail>`) |
| `/var/log/restore_patches.log` | `restore_patches.sh` | Which patch was reapplied and when — if it's empty after a restart, that's a good sign (nothing had been lost) |
| `/var/log/auto_release_leads.log` | `auto_release_leads.sh` | Every lead released due to abandonment (`aec_id`, `contacto_id`, name, whether it had an owner or not) and the result of the CRM unassignment |
| `/var/log/sync_lead_owner.log` | `sync_lead_owner.sh` | Leads reassigned to their owner from the pool, and appointment reminders closed as overdue |
| `/var/log/redis_watchdog.log` | `redis_watchdog.sh` | Alerts about missing Redis keys and whether the automatic regeneration worked |
| `/var/log/did_picker_watchdog.log` | `check_did_picker.sh` | Every time it had to restart the `did-picker` service because it wasn't responding |
| `/var/log/django_restart_watcher.log` | `watch_django_restart.sh` | Detected restarts of the Django container and whether re-logging agents into the queues succeeded |
| `/root/create_agent_inbound.out`, `/root/agent_inbound.json` | `create_agent_inbound.sh` | Raw output and structured JSON from the last run of personal inbound campaign creation |

Scripts that only print to stdout (`normalize_phones.sh`,
`sync_agent_pause.py`, `firewall.sh`, `purge_sip_intruders.sh`) don't
generate a log file on their own — if they're scheduled via cron, the output
has to be redirected by hand (`>> /var/log/<name>.log 2>&1`).
