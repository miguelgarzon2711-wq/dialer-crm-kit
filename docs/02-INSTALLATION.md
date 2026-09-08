# 02 — Step-by-step installation

Runbook from an empty server to a working dialer. Order matters: each step
depends on the previous one.

Before starting, read [`../CLAUDE.md`](../CLAUDE.md) in full. This document
assumes you already know the hard rules in there.

Convention: anything shown as `<LIKE_THIS>` gets replaced with real data.

---

## Step 0 — Information you need from the client

Don't start without this. If the client can't answer something, note it as
pending and move on, but don't guess it.

**About the business:**

| Pregunta | Para qué la necesitás |
|---|---|
| Where do the leads come from? | defines how the CRM connects |
| How many leads per day and how many sales reps? | sizes the server and the number pool |
| What is a call trying to achieve? | defines the dispositions |
| Does a lead who already talked to a sales rep stay with them? | defines the sticky behavior (almost always yes) |
| What are the possible outcomes of a call? | that list is the exact set of dispositions |
| What hours and time zone does calling happen in? | configures the system and the reports |
| What data does the sales rep need from each call? | defines the transcription instructions |

**Access:**

- Server with `root` access and its SSH key.
- CRM account with permission to create an API token.
- SIP provider account with balance.
- A domain or subdomain pointing to the server.
- Optional: OpenAI key for transcription.

---

## Step 1 — Server and OMniLeads

Sizing reference: **4 vCPU / 8 GB RAM** for around 20 concurrent agents.
Dedicated server, not shared.

Before installing anything, set the system's time zone:

```bash
timedatectl set-timezone <YOUR_TIMEZONE>        # e.g. America/New_York
date                                       # verify
```

Install OMniLeads following its official documentation. When it's done,
verify that:

- You can reach the web console through the domain, over HTTPS.
- You can create an agent and that agent can log in.
- The browser phone connects (it doesn't say "SIP Proxy not responding").

Write down the containers' real names, because the kit's scripts use them:

```bash
docker ps --format '{{.Names}}'
```

If they don't match `prod-env-django-app-1`, `prod-env-acd-1`,
`prod-env-postgresql-1`, and `prod-env-redis-1`, do a global replace in
`server/scripts/` before continuing.

**Don't move on until this works.** Everything else builds on this.

---

## Step 2 — Lock down the server

Do this now, not at the end. A dialer with open ports gets automated
registration attempts from day one.

Read both scripts before running them and adjust your network interface name:

```bash
ip -o link show | awk -F': ' '{print $2}'    # see the interface name
```

```bash
bash server/scripts/firewall.sh
bash server/scripts/purge_sip_intruders.sh
```

Set it up as a service so it survives restarts:

```bash
cat > /etc/systemd/system/dialer-firewall.service <<'EOF'
[Unit]
Description=Close the dialer's internal ports
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

**Verify it from another machine**, not from the server:

```bash
nmap -Pn -p 5038,5060,5160,6379,9000 <SERVER_IP>
```

The internal ports need to show up closed or filtered. From the server
itself they always look fine: that's not a valid test.

---

## Step 3 — Telephony

Read [`05-TELEPHONY.md`](05-TELEPHONY.md) in full before this step.

1. Sign up with the SIP provider and buy numbers for the area you're going
   to call.
2. Configure the trunk in OMniLeads. **The endpoint name has to be the
   trunk's name**, and it doesn't carry a from-user: that's the most common
   mistake.
3. Create the outbound route. OMniLeads' generator doesn't build this
   context correctly: use `server/asterisk/extensions_outbound_route.conf`
   as a base.
4. Set up the business caller ID with your provider. It's usually free and
   raises the answer rate quite a bit.

Load the numbers into the pool (Step 4 creates the table):

```sql
INSERT INTO numbers_pool (did) VALUES
  ('<NUMBER_1>'), ('<NUMBER_2>')
ON CONFLICT DO NOTHING;
```

**Finish with a real call:** it has to ring, be audible both ways, and leave
a saved log entry on hangup. If you haven't made that call, this step isn't
done.

---

## Step 4 — Database

```bash
docker exec -i prod-env-postgresql-1 psql -U omnileads -d omnileads \
  < server/sql/schema.sql
```

Verify:

```bash
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads \
  -c "\dt dialer_*" -c "\df pick_did"
```

You should see three `dialer_*` tables, the three number tables, and the
`pick_did` function.

Test the rotator before continuing:

```bash
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads \
  -c "SELECT pick_did('5551234567')"
```

It should return one of your numbers. If it comes back empty, the pool is
empty.

---

## Step 5 — Credentials

```bash
mkdir -p /opt/dialer-kit
cp .env.example /root/.env_dialer
chmod 600 /root/.env_dialer
```

Fill in each variable. Leave the ones you don't use empty: the system works
without transcription and without the optional ones.

The token the CRM uses to talk to the dialer gets generated like this:

```bash
docker exec prod-env-django-app-1 python3 \
  /opt/omnileads/ominicontacto/manage.py shell -c \
  "from rest_framework.authtoken.models import Token; \
   from django.contrib.auth import get_user_model; \
   u=get_user_model().objects.get(username='<API_USER>'); \
   print(Token.objects.get_or_create(user=u)[0].key)"
```

That value goes into `DIALER_API_TOKEN` and into the `Authorization` header
of the CRM's webhooks. **It's the only thing stopping a third party from
injecting fake leads.**

---

## Step 6 — Integration modules

Copy the four modules to the Django container:

```bash
mkdir -p /opt/dialer-kit/patches
cp server/django/*.py /opt/dialer-kit/patches/

for f in crm_webhooks crm_dispositions lead_ownership agent_api; do
  python3 -c "import ast; ast.parse(open('/opt/dialer-kit/patches/$f.py').read())" || exit 1
  docker cp /opt/dialer-kit/patches/$f.py \
    prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/views/$f.py
done
```

Add the routes: copy the urls file from the container, paste the block from
`server/django/urls_patch.py` at the end, validate it, and copy it back.

```bash
docker cp prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/urls.py \
          /opt/dialer-kit/patches/urls.py
# ... edit and paste the block ...
python3 -c "import ast; ast.parse(open('/opt/dialer-kit/patches/urls.py').read())"
docker cp /opt/dialer-kit/patches/urls.py \
          prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/urls.py
docker restart prod-env-django-app-1
```

Wait for it to come up and verify:

```bash
for i in $(seq 1 30); do
  sleep 3
  c=$(curl -s -o /dev/null -w "%{http_code}" https://<YOUR_DOMAIN>/accounts/login/)
  [ "$c" = "200" ] && { echo "arriba en $((i*3))s"; break; }
done

docker exec prod-env-django-app-1 python3 \
  /opt/omnileads/ominicontacto/manage.py shell -c \
  "from api_app.views import crm_webhooks, crm_dispositions, lead_ownership, agent_api; print('OK')"
```

If the import fails, check the error and fix it **before** continuing.

### Test the webhook with a fake lead

```bash
curl -X POST https://<YOUR_DOMAIN>/api/v1/crm/lead_action/ \
  -H "Authorization: Bearer <DIALER_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"action":"add","tipo":"Nuevo Lead","telefono":"5551234567","nombre":"Prueba"}'
```

The exact body structure is in
[`03-CRM-INTEGRATION.md`](03-CRM-INTEGRATION.md). With `DIALER_LIVE=0` the
request responds fine but doesn't inject: that's correct while you're
testing.

---

## Step 7 — Patches on top of OMniLeads

Read [`../server/patches/README.md`](../server/patches/README.md) and apply
them **one at a time**, verifying after each one. If you apply five together
and something breaks, you won't know which one it was.

The ones you can't skip:

- Don't auto-finalize campaigns (otherwise the campaign closes itself
  mid-morning).
- Digits only when dialing (otherwise calls die silently).
- Number masking, in all five places.

---

## Step 8 — Scheduled tasks

```bash
mkdir -p /opt/dialer-kit/scripts
cp server/scripts/* /opt/dialer-kit/scripts/
chmod +x /opt/dialer-kit/scripts/*.sh
```

The full table of what each one does is in
[`07-OPERATIONS.md`](07-OPERATIONS.md). Starting point:

```cron
@reboot sleep 90 && bash /opt/dialer-kit/scripts/restore_patches.sh
*/10 * * * * bash /opt/dialer-kit/scripts/normalize_phones.sh >> /var/log/normalize_phones.log 2>&1
*   * * * * bash /opt/dialer-kit/scripts/sync_agent_pause.sh  >> /var/log/sync_agent_pause.log 2>&1
*/5 * * * * bash /opt/dialer-kit/scripts/auto_release_leads.sh
*   * * * * bash /opt/dialer-kit/scripts/sync_lead_owner.sh
*   * * * * bash /opt/dialer-kit/scripts/check_did_picker.sh
```

Add transcription only if you configured the OpenAI key:

```cron
* * * * * bash /opt/dialer-kit/scripts/run_transcribe.sh >> /var/log/transcripciones.log 2>&1
```

The number selector runs as a service:

```bash
cat > /etc/systemd/system/did-picker.service <<'EOF'
[Unit]
Description=Dialer caller ID selector
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
curl -s "http://127.0.0.1:8055/pick?tel=5551234567"    # should return a number
```

Let a few minutes pass and check that the logs are being written. A cron
that fails silently is worse than not having one.

---

## Step 9 — Persistence

Adapt `restore_patches.sh` to your installation: paths, container names, and
one block per patch you've applied.

**Then actually test it:**

```bash
reboot
# wait, log back in, and verify:
tail -30 /var/log/restore_patches.log
docker exec prod-env-django-app-1 python3 \
  /opt/omnileads/ominicontacto/manage.py shell -c \
  "from api_app.views import crm_webhooks; print('OK')"
```

A kit that doesn't survive a reboot isn't finished.

---

## Step 10 — Final verification

Objective criteria. If any one fails, it's not ready.

| # | Prueba | Aprobado si |
|---|---|---|
| 1 | Real outbound call | it rings, it's audible both ways, the log gets saved |
| 2 | Real inbound call | it reaches the right sales rep and is audible |
| 3 | Webhook with a test lead | the lead shows up in the queue within seconds |
| 4 | Full cycle | the sales rep picks it up, calls, dispositions it, and the result shows up in the CRM |
| 5 | Sticky | a lead dispositioned as "contestó" always goes back to the same sales rep |
| 6 | Two sales reps at once | neither one gets the same lead |
| 7 | Reboot | everything comes back on its own, no intervention |
| 8 | Ports | scan from outside: internal ones closed |
| 9 | Credentials | `grep -rn` over the code: no results |
| 10 | Crons | the logs are being written |

Only once all ten pass, flip the switch: `DIALER_LIVE=1`.

---

## Common problems during installation

| Síntoma | Causa probable | Solución |
|---|---|---|
| The call does nothing, no error | the number has a `+`, spaces, or dashes | normalize to digits only; check that the cron is running |
| The browser phone says "SIP Proxy not responding" | misconfigured SIP proxy certificates | check the proxy's TLS configuration and reload |
| 502 error on the phone connection | nginx routing was modified | restore the original configuration |
| Django won't come up after a patch | syntax error in the copied file | check the container logs and restore the previous copy |
| The lead gets delivered and disappears | a SQL error aborted the transaction | wrap the optional queries in savepoints |
| The campaign closes itself | the no-auto-finalize patch is missing | apply it |
| Calls go out with no caller ID | the pool is empty or the selector isn't responding | load numbers and check the service |
| The sales rep isn't getting leads | they're stuck paused | check the pause synchronizer's log |
| Nothing goes back to the CRM | the disposition engine import is missing from the routes | add the last line of `urls_patch.py` |
| A patch disappears on its own | it's not in `restore_patches.sh` | add it |
