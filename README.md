# Dialer + CRM Kit

Production-tested integration between an **OMniLeads dialer** and a **CRM** (tested
with GoHighLevel): leads come in on their own, get dialed by priority, and the result
of every call goes back to the CRM without anyone copying and pasting.

It came out of a live system running roughly 20 reps and 80 new leads a day.
Everything here ran in production, and the mistakes that cost the most are documented
so you do not repeat them.

---

## What it solves

Sales teams that call leads tend to hit the same five problems. This kit attacks the
root cause of each:

| Problem | What the kit does |
|---|---|
| Leads land in the CRM and nobody calls them in time | a webhook pushes them into the queue within seconds, ordered by likelihood of answering |
| Two reps call the same customer and fight over the commission | the first one to have a real conversation owns that lead for life |
| Outbound numbers get flagged "Spam Likely" and nobody answers | caller ID rotation with a daily cap per number and gradual warm-up |
| Nobody knows what was said on the calls | AI transcription, automatic CRM notes, and an alert when a rep dispositions a voicemail as if it were a person |
| The server reboots and half the system needs reconfiguring | patches reapply themselves at boot |

---

## What you need before starting

- Your own server with **OMniLeads** installed (Docker). Reference: 4 vCPU / 8 GB for
  about 20 concurrent agents.
- An account with a **SIP provider** (tested with Telnyx) and numbers in the area you
  will be calling.
- A **CRM** account with API access. GoHighLevel integration is included; for another
  CRM you rewrite a single layer.
- Optional: an **OpenAI** key if you want transcription and automatic notes.
- Someone with `root` access to the server.

---

## How to install it

**If you are using an AI agent (Claude Code or similar), point it at
[`CLAUDE.md`](CLAUDE.md) first.** That file is written for the agent: correct
installation order, rules that cannot be broken, and known failure modes.

The route, in short:

1. OMniLeads installed and verified.
2. **Lock down the server** (firewall). This comes early, not last.
3. Telephony: trunk, numbers, and a real call that rings.
4. Database: `server/sql/schema.sql`.
5. Credentials: copy `.env.example` and fill it in.
6. CRM integration.
7. Automation (cron).
8. Patch persistence and a reboot test.

Full detail in [`docs/02-INSTALLATION.md`](docs/02-INSTALLATION.md).

---

## Documentation

| Document | What it covers |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | **Start here.** Instructions for the installing agent, hard rules, known failure modes |
| [`docs/01-ARCHITECTURE.md`](docs/01-ARCHITECTURE.md) | How the pieces fit and the path a call takes |
| [`docs/02-INSTALLATION.md`](docs/02-INSTALLATION.md) | Step by step from an empty server |
| [`docs/03-CRM-INTEGRATION.md`](docs/03-CRM-INTEGRATION.md) | Webhooks, payloads, priorities and the ownership cycle |
| [`docs/04-BEHAVIOR.md`](docs/04-BEHAVIOR.md) | The business rules: what the dialer does and why |
| [`docs/05-TELEPHONY.md`](docs/05-TELEPHONY.md) | Trunk, number rotation, inbound routing and SIP security |
| [`docs/06-AI-TRANSCRIPTION.md`](docs/06-AI-TRANSCRIPTION.md) | Automatic notes and voicemail auditing |
| [`docs/07-OPERATIONS.md`](docs/07-OPERATIONS.md) | Cron, persistence, watchdogs and troubleshooting |
| [`docs/08-LESSONS.md`](docs/08-LESSONS.md) | What was expensive to learn |
| [`docs/09-KNOWN-ISSUES.md`](docs/09-KNOWN-ISSUES.md) | What we know is imperfect, and how to deal with it |

---

## Repository layout

```
server/
  django/     modules copied into the Django container
              crm_webhooks.py       receives leads from the CRM
              crm_dispositions.py   sends results back to the CRM
              lead_ownership.py     permanent lead ownership
              agent_api.py          REST API for mobile clients
  asterisk/   dialplan and queues
  scripts/    scheduled jobs and maintenance
  sql/        the kit's own tables
  patches/    changes to apply on top of OMniLeads files
tools/        credential scanner, run before publishing
docs/         documentation
```

---

## A note on language

Documentation and code comments are in English. **Identifiers are not**: table names,
columns, variables and API fields are in Spanish (`contacto_id`, `agente_id`,
`campana_id`, `telefono`) because OMniLeads defines them that way. Renaming them
breaks the system. The same applies to disposition and lead-type values, which are
compared against the database and the CRM as literal strings.

---

## Security

This repository contains **no credentials**. Every secret lives in `/root/.env_dialer`
(mode 600), outside version control.

Before pushing any change, run:

```bash
bash tools/check_secrets.sh
```

It scans for keys, tokens, IP addresses, phone numbers and real domains that slipped
through. If it finds anything, do not push until it is cleaned up.

---

## License and use

Private use under the author's authorization. OMniLeads and Asterisk carry their own
licenses: this kit neither includes nor redistributes them, it only builds on top.
