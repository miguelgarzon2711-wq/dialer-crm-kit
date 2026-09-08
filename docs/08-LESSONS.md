# Lessons that cost us

Each of these came out of a real production problem: hours of diagnosis, dropped
calls, or money spent. Reading them now saves you from repeating them.

They're ordered by how expensive they turned out to be.

---

## 1. An open SIP port is someone else's money

**What happened.** A few days after installing the dialer, unknown softphones
showed up registered on the PBX pretending to be agent extensions. They were one
step away from making international calls at the client's expense.

**Why.** The default install leaves the SIP port listening to the internet, and
automated scanners probe extensions and passwords around the clock.

**What to do.** Close the ports **day one**, not once everything else is ready:
- Block registration messages that don't come from the internal proxy.
- Close every internal port to the outside (database, memory store, PBX admin
  interface). In Docker this goes in the chain that filters traffic towards the
  containers; a normal firewall rule isn't enough.
- A cron that periodically deletes any registration that doesn't come from the
  proxy.

**How to verify it.** Scan the ports **from the outside**, from another machine.
From the server itself, everything always looks fine.

---

## 2. A SQL error erases the lead from the sales rep

**What happened.** Sales reps reported the lead "vanishing": they'd request it, it
would show up for an instant, and then disappear.

**Why.** A query against a column that didn't exist. In PostgreSQL, a SQL error
**aborts the entire transaction** even if you catch the exception in Python.
Everything after it in that same request would fail, including the lead delivery.

**What to do.** Every optional query goes in its own savepoint:
```python
try:
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute("SELECT ...")
except Exception:
    pass
```
And never assume a column exists just because "it should."

---

## 3. A "+" kills the call without a trace

**What happened.** Leads that couldn't be called. No error, no log, nothing: it
would dial and absolutely nothing would happen.

**Why.** The number had an international format with `+`. The PBX's dialplan
expects digits only; with the `+` no rule matches and the call dies silently.

**What to do.** Normalize on **every** entry path: when injecting from the CRM,
when dialing from the console, when dialing from the API, in the initial bulk
load. And also a cron every 10 minutes as a safety net, because there's always a
path you didn't think of.

**General lesson:** when something fails silently, suspect the data format before
the logic.

---

## 4. What you edit inside a container disappears

**What happened.** Patches that used to work would stop being there after a
restart, and the system would fail again as if it had never been fixed.

**Why.** Containers get recreated. Anything inside that doesn't come from the
image is lost.

**What to do.** Three-step discipline, no exceptions:
1. Edit the copy on the **host**.
2. Copy it into the container.
3. Add the block to the persistence script that runs at startup.

And test a real restart. A system that doesn't survive a reboot isn't finished.

---

## 5. Never `docker compose up`

It brings up and recreates containers that hold state, and you lose everything
inside them. In a contact center stack, that's the entire system.

Always use individual operations: `docker exec`, `docker cp`, `docker restart <name>`.

---

## 6. Code brought over from another client steals the calls

**What happened.** When cloning a dialer that already worked to set up a new one,
the new client's calls went out through the old client's provider.

**Why.** The origin's custom configurations traveled with the copy: outbound
routes, provider credentials, identifiers.

**What to do when cloning:**
- Empty out all custom configurations before starting.
- Rotate **all** secrets: PBX keys, SIP proxy, database, admin interface.
- Search for the old client's name across the whole tree and in the database.
- Test with a real call and verify where it went out from.

---

## 7. Dates in the wrong timezone ruin the reports

Always store in universal time, and convert to the client's timezone **only when
displaying**. A badly converted hour makes a 5pm callback get injected at 10am, or
a daily report include calls from a different day.

Watch out for the common case: the server in one country, the client in another,
and the operator in a third.

---

## 8. You get charged for numbers even if you don't use them

Phone numbers are billed **per month, per number**, even if you don't make a
single call. A pool of 80 numbers costs a fixed ~80 dollars a month.

Before buying, do the math: dials per day ÷ daily cap per number. And check when
your provider bills: it can be by calendar month or by purchase anniversary. If
you have auto top-up with insufficient balance, the renewal fails and you're left
without numbers overnight.

---

## 9. Transcription is billed per audio, not per call

**What happened.** Transcription cost spiked without call volume going up.

**Why.** The process was reprocessing the same recordings over and over.

**What to do.** Track which audio has already been transcribed (by a unique call
identifier), check before sending, and use a lock so two simultaneous runs don't
duplicate work. In the real case, the saving was about 35 dollars a month at low
volume; at high volume it's much more.

---

## 10. Configuration "looking fine" means nothing

A mis-named trunk, a missing dialplan context, or a wrong prefix look perfect in
the panel and don't fail until you actually dial.

Always test with a **real call**: that it rings, that it's heard in both
directions, that the record is saved when you hang up.

---

## 11. Without sticky, your team fights

Two sales reps calling the same customer on the same day is the fastest way to
look bad in front of the client and cause internal conflict over commission.

The rule that fixes it: **whoever talks to them first keeps the lead forever.**
That they don't answer afterward changes nothing.

---

## 12. Every automation must self-heal in both directions

A cron that only applies a change in one direction leaves the system broken as
soon as something restarts. Real example: the pause that keeps a sales rep from
getting calls while they have a lead open. If the cron only pauses and never
unpauses, a restart leaves people paused forever with no work coming in, and
nobody notices.

Automations must **compare the actual state against the desired one and correct
in both directions**, without assuming any previous command went through.

---

## 13. An optional piece can never bring down the call

The caller ID picker is an auxiliary service. If it goes down and the dialplan
waits on it with no fallback, the entire calling system goes down with it.

Set a short timeout and a default fallback on every call to an external service.
A call with a less-than-optimal number beats no call at all.

---

## 14. On mobile, audio starts when the OS says so

**What happened (applies if you're building a mobile client).** It would ring and
then the audio would cut out.

**Why.** The app was answering the call before the OS activated the audio
session. The audio engine would start on top of a session that then changed, and
it went mute.

**What to do.** Wait for the OS's "audio session activated" notice before
answering. With a timeout, so it doesn't wait forever.

---

## 15. When something fails, find the root cause before changing tack

The temptation is to try something else. It's almost always cheaper to read the
actual log, capture the traffic, or compare against a case that does work.

An example from this very project: for hours, an audio problem in the mobile app
was chased down. Capturing network traffic showed the audio was flowing fine in
both directions: the problem was the number being called, which answered and hung
up after a few seconds.
