# Known issues in the kit

Things we know are imperfect in this code. They're here so you don't discover
them in production, and so you can decide whether they matter before you
install.

None of them stop the system from working. All of them have a known fix.

---

## 1. The initial lead load needs a manual step

`backfill_prepare.py` writes its result to `/root/backfill_leads.json`, but
`backfill_inject.py` reads it from `/tmp/backfill_leads.json`. Between the two
steps you have to copy the file by hand:

```bash
python3 backfill_prepare.py --dias 30
cp /root/backfill_leads.json /tmp/backfill_leads.json     # <-- this step is missing
python3 backfill_inject.py
```

**Fix:** unify the path in both files, or leave the `cp` documented in your
runbook. It's a one-time process, so it's not serious either.

---

## 2. The backfill's "assign" mode does nothing

`backfill_prepare.py` accepts an `asignar` mode (send each old lead to the sales
rep who already handled it), but in practice it always marks leads as `pool`.
The mode exists in the documentation and in `backfill_inject.py`, but whatever
generates the list never produces the other value.

**Consequence:** today the initial load always sends everything to the general
pool.

**Fix:** if you need the assign mode, the logic has to be completed in
`backfill_prepare.py`. If you don't need it, ignore it: the pool mode is the one
used in production and it works.

---

## 3. The "answered" disposition list is duplicated

The same set of dispositions is defined twice, in two different files:

- `CONTESTO` in `crm_dispositions.py` — decides whether the lead is assigned in
  the CRM.
- `DISPOS_CONVERSACION` in `lead_ownership.py` — decides whether the sales rep
  becomes the owner.

Today they're identical. If you edit one and forget the other, the system starts
behaving inconsistently: the lead ends up assigned in the CRM but with no owner
in the dialer, or the other way around. And it's very hard to diagnose.

**Recommended fix:** when adapting the kit, define the list **once** in a shared
module and import it in both places. If you'd rather not touch it, leave a
comment in both files pointing to the other.

---

## 4. The patches' line numbers won't match

The blocks in `server/patches/` carry the line numbers from the OMniLeads
version where they were made. In your installation they'll be shifted.

**Fix:** always search by function name or by the block's text, never by line
number.

---

## 5. The "only answered if it connected" check ships disabled

There's a validation that prevents saving a conversation disposition if the call
was never answered. It ships disabled on purpose: voicemail counts as "answered"
for the PBX, so the check can't tell a person apart from an answering machine,
and it would block legitimate dispositions.

**Fix:** turn it on only once transcription is working, since that can actually
tell voicemail apart from a person.

---

## 6. The scripts assume OMniLeads' container names

The scripts call the containers by fixed name (`prod-env-django-app-1`,
`prod-env-acd-1`, `prod-env-postgresql-1`, `prod-env-redis-1`). If your
installation uses different names, they have to be replaced.

**Fix:** check the real names with `docker ps --format '{{.Names}}'` and do a
global replace before installing. It's worth pulling them out into variables at
the top of each script.

---

## 7. The messages are in Spanish

The messages the sales rep sees, the logs, and the code comments are in
Spanish. If your client's team speaks another language, they need to be
translated.

**Fix:** the visible text is concentrated in a few places. It's best to leave
the code comments as they are: they explain decisions, and machine-translating
them tends to lose the nuance behind why each rule exists.
