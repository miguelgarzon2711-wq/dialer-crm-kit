# Instructions for the agent installing this kit

> If you are an AI assistant (Claude Code or similar) and someone asked you to set up
> this dialer: **read this file completely before running a single command.** It was
> written for you. Every rule here came from breaking something in production and
> having to fix it.

---

## 1. What this is and what it is not

This is an **integration kit**, not a one-click product.

It assumes a working **OMniLeads** installation on your own server (or that you are
about to install one). OMniLeads is an open-source contact center built on Asterisk,
Django, PostgreSQL and Redis, running in Docker containers. This kit adds:

| What it adds | Why |
|---|---|
| CRM integration (tested with GoHighLevel) | leads come in on their own and results go back on their own |
| Dialing priorities | call the person most likely to answer first |
| Permanent lead ownership ("sticky") | two reps never fight over the same customer |
| Caller ID rotation | your numbers do not get flagged as "Spam Likely" |
| AI transcription | automatic CRM notes and voicemail auditing |
| REST API for mobile clients | a rep without a computer can still work |
| Patch persistence | nothing is lost when containers restart |

**Not included:** OMniLeads itself, the mobile app, or any credentials. Anything
written as `<LIKE_THIS>` you must replace with the client's real values.

---

## 2. Hard rules — breaking one of these costs hours

These are not suggestions. Each maps to a real incident.

### 2.1 Never run `docker compose up`
OMniLeads brings up containers with hash prefixes. `docker compose up` kills and
recreates them, and you lose everything inside. **Always use individual operations:**
`docker exec`, `docker cp`, `docker restart <name>`, `docker start/stop`.

### 2.2 Anything you edit inside a container, keep a copy outside
Containers get recreated. If you patch a file with `docker exec` and leave no copy on
the host, the next restart wipes it and the system fails again with nobody
understanding why. **Correct flow:** edit the local copy in `/opt/dialer-kit/patches/`,
copy it into the container with `docker cp`, then add the matching block to
`restore_patches.sh`.

### 2.3 Validate syntax BEFORE copying into the container
A Python file with a syntax error takes down all of Django, and the dialer stays dead
until someone notices.
```bash
python3 -c "import ast; ast.parse(open('file.py').read())" && echo OK
bash -n script.sh && echo OK
```
Do this every time. No exceptions.

### 2.4 In PostgreSQL, one SQL error aborts the whole transaction
Even if you catch the exception in Python. If a query inside a request fails (say, on
a column that does not exist), **everything after it in that same request fails too**,
including handing the lead to the rep. The symptom is baffling: "the lead disappears."

The defense is to wrap every optional query in its own savepoint:
```python
from django.db import transaction, connection
try:
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute("SELECT ...")
except Exception:
    pass   # this query failed, but the request survives
```

### 2.5 The switch only dials digits
A phone number with `+`, spaces or dashes **kills the call silently**: no error, no
log, nothing happens, and the rep assumes the number is dead. Normalize to 10 digits
(or whatever length the country uses) before dialing, on EVERY path: lead injection,
dialing from the console, dialing from the API, plus a cron job as a safety net.

### 2.6 Before changing anything in production, say so and wait for approval
Containers, Django, Asterisk and the database are live systems with people working on
top of them. Explain what you are touching and what could happen. Better to ask too
often than too little.

### 2.7 Never touch nginx routing
Cache headers are fine. Proxy and routing rules are not: one change there leaves the
softphone with no signaling (502 on `/ws`) and nobody can call.

### 2.8 Test with a REAL call
Configuration that "looks right" means nothing. A mistyped trunk endpoint, a missing
dialplan context or a wrong prefix only surface when you actually dial and listen.
Make the call, hear audio both ways, hang up, and confirm the record was saved.

---

## 3. Installation order

Do not skip steps or reorder them. Each depends on the previous one.

### Step 0 — Understand the business before touching code
Do not start until you can answer these. Ask the client:

1. Where do the leads come from? (form, WhatsApp, ads, old database)
2. How many per day? How many reps?
3. What outcome is a call trying to produce? (book an appointment, sell, qualify)
4. Do reps compete for leads, or does each own theirs?
5. **Should a lead that already spoke with a rep stay with that rep?**
   (almost always yes, and it drives the entire "sticky" logic)
6. What are the possible outcomes of a call? That list is your dispositions.
7. What hours do they call? What time zone?
8. How many times is a lead attempted before giving up?

Write the answers into a project file. You will come back to them constantly.

### Step 1 — Server and OMniLeads
- Dedicated server, not shared. Sizing reference: 4 vCPU / 8 GB for ~20 concurrent agents.
- Watch the system time zone.
- Install OMniLeads following its official documentation.
- Verify you can reach the web console and an agent can log in.
- **Do not touch anything in this kit yet.**

### Step 2 — Lock down the server (do this NOW, not at the end)
A freshly installed dialer with open ports is a target. In a real case, attackers
registered softphones posing as agents in under a day.
```bash
bash server/scripts/firewall.sh              # closes internal ports to the outside
bash server/scripts/purge_sip_intruders.sh   # drops registrations not coming from the proxy
```
Read both scripts before running them and adjust the network interface name. Install
the firewall as a systemd service so it survives reboots.

### Step 3 — Telephony
Sign up with the SIP provider, buy the numbers, configure the trunk.
Read `docs/05-TELEPHONY.md` in full: it lists the classic mistakes that cost a full
day (the trunk endpoint name, E.164 format, and the outbound route context you must
write by hand because the OMniLeads generator does not build it correctly).

**Finish this step with a real call that rings and has audio both ways.**

### Step 4 — Database
```bash
docker exec -i prod-env-postgresql-1 psql -U omnileads -d omnileads < server/sql/schema.sql
```

### Step 5 — Credentials
```bash
cp .env.example /root/.env_dialer
chmod 600 /root/.env_dialer      # important
```
Fill in every variable. Never write them into the code or commit them.

### Step 6 — CRM integration
Copy the modules from `server/django/` into the Django container, add the routes and
restart. Read `docs/03-CRM-INTEGRATION.md` for the exact webhook payloads, and test
with a fake lead before wiring up the real flow.

### Step 7 — Automation
Install the cron jobs from `server/scripts/`. The full table is in
`docs/07-OPERATIONS.md`. Start with the security and persistence ones.

### Step 8 — Persistence
Adapt `server/scripts/restore_patches.sh` to your installation and add it to boot:
```
@reboot sleep 90 && bash /opt/dialer-kit/scripts/restore_patches.sh
```
**Actually test it:** reboot the server and confirm the dialer comes back by itself.
A kit that does not survive a reboot is not finished.

### Step 9 — Acceptance test before handover
- Real outbound call, audio both ways.
- Inbound call reaching the right rep.
- A lead enters through the webhook and shows up in the queue.
- A rep takes it, calls it, dispositions it, and the result appears in the CRM.
- Reboot the server and everything still works.
- Two reps taking leads at the same time without collisions.

---

## 4. Adapting it to a different client

### 4.1 What ALWAYS needs changing
| What | Where |
|---|---|
| CRM, SIP provider and AI credentials | `/root/.env_dialer` |
| Lead types and priorities | `TIPO_ORDEN` in `crm_webhooks.py` |
| Dispositions and which count as "answered" | `CONTESTO` / `NO_CONTESTO` in `crm_dispositions.py` |
| Tags written back to the CRM | `crm_dispositions.py` |
| Numbers and daily cap per number | `did_picker.py` |
| Queues and campaigns | `server/asterisk/queues.conf.example` |
| Transcription language and business context | `transcribe_calls.py` |
| Time zone | system and Django settings |

### 4.2 What probably works as is
Lead ownership, phone normalization, the firewall, patch persistence, abandoned-lead
release, and the API structure.

### 4.3 If the CRM is not GoHighLevel
The design already separates the layers. Rewrite only the functions that talk HTTP to
the CRM (`_try(...)` and the `requests` calls in `crm_dispositions.py` and
`crm_webhooks.py`). Priority, ownership and disposition logic is CRM-agnostic and
stays as is. Details at the end of `docs/03-CRM-INTEGRATION.md`.

---

## 5. Design decisions worth keeping

You can change them, but understand why they are this way first.

**The server decides, the client only renders.** Neither the web console nor the mobile
app decides anything: they ask and display. That keeps behavior identical across
devices and means a rule changes in exactly one place.

**The lead's phone number never leaves the server.** The client receives
`***-***-1234`. Reps dial by contact id, not by number. This is what stops someone
from walking out with the database on their phone.

**A lead that had a conversation is owned for life.** One real conversation is enough
for that lead to belong to that rep: redials, callbacks and inbound calls always come
back to the same person. Without this, reps fight over commissions. Later
no-answers do not remove ownership.

**No new call until the last one is dispositioned.** Otherwise you get orphan calls
with no outcome and your metrics become useless.

**One session per rep.** The last device in kicks the previous one out. Two sessions
for the same agent compete for the same SIP phone and neither works properly.

**No inbound calls while a lead is on screen.** Otherwise a call lands on the rep just
as they were about to dial, and they lose the lead they had open.

**Every cron job must self-heal in both directions.** Do not assume a previous command
landed. Compare actual state against desired state and correct both ways: that is what
lets the system recover on its own after any component restarts.

---

## 6. Mistakes we already made — do not repeat them

| Symptom | Actual cause |
|---|---|
| Call dies silently, no error | the number contained `+` or spaces |
| Delivered lead "disappears" | an SQL error aborted the whole transaction |
| One ring, then audio cuts out | on mobile, the call was answered before the OS activated the audio session |
| Softphone reports "SIP Proxy not responding" | TLS certificates on the SIP proxy misconfigured |
| A patch vanishes on its own | it was edited inside the container with no copy on the host |
| Timestamps look wrong | stored in UTC and displayed without converting to the client's zone |
| Outbound calls nobody placed | SIP port left open: someone registered as an agent |
| Campaigns close by themselves | OMniLeads auto-finalizes Preview campaigns when contacts run out |
| Two reps calling the same lead | lead ownership missing |
| An agent gets no leads and nobody knows why | they were left paused by a pause that never lifted |

---

## 7. Known issues in the kit itself

Before installing, read `docs/09-KNOWN-ISSUES.md`. These are things we know are
imperfect and that you should decide about up front rather than discover in
production. None of them prevent the system from working.

The three most likely to bite you:
- Container names are hardcoded in the scripts: check yours with
  `docker ps --format '{{.Names}}'` and replace them before installing.
- The list of dispositions that count as "answered" is duplicated across two files.
  If you edit one, edit the other.
- Line numbers in the patches will not match your OMniLeads version: search by
  function name, never by line number.

---

## 8. Before calling it done

- [ ] Real outbound call with audio both ways.
- [ ] Real inbound call to the correct rep.
- [ ] A lead completes the full cycle: CRM to dialer to call to disposition to CRM.
- [ ] Server reboot: everything comes back on its own.
- [ ] `grep -rn` for credentials in the code: no results.
- [ ] Internal ports closed (verify from outside the server, not from inside).
- [ ] Cron jobs running and writing to their logs.
- [ ] The client knows what to look at when something breaks.

If any of these is missing, it is not done. Say so instead of handing it over half-finished.
