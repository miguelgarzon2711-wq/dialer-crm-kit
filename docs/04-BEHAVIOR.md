# Dialer behavior

This document is the **contract**: what the system does in each situation and why.
If you're going to change a rule, read the "why" first — almost all of them came
from a real operational problem, not a technical preference.

Rule that runs through the whole design: **the server decides, the client only
paints.** Neither the web console nor the mobile app decide anything. They request
data and show buttons. That way behavior is identical on every device and a rule
only needs to change in one place.

---

## 1. Agent states

| State | What it means | Do calls reach them? |
|---|---|---|
| Disconnected | not logged into the system | no |
| Connected / ready | available | yes |
| Lead on screen | took a lead and hasn't closed it yet | **no** |
| On call | talking | no |
| ACW (after-call work) | hung up and hasn't disposed yet | no |
| On break | manual pause (bathroom, lunch) | no |

**Why "lead on screen" doesn't receive calls:** without this rule, the seller gets a
call right when they were about to dial, and loses the lead they had open. It's
implemented by pausing the agent in the queues when they take a lead and unpausing
them when they close it.

**The safety net:** a cron compares the real state of the queues against the
desired state every minute and corrects it in both directions. Without that
safety net, any restart leaves people paused forever without receiving leads, and
nobody notices until someone asks why they aren't getting work.

---

## 2. Getting a lead

The seller presses a single button. The server chooses for them.

1. Searches across **all** the agent's active campaigns, not just one.
2. Returns the **highest-priority** lead (see table below).
3. If the lead has an owner and the owner is someone else, it isn't delivered.
4. Marks the lead as delivered to that agent (reservation).
5. Pauses it in the queues while they have it on screen.

**The same lead keeps being delivered until they call it.** If they ask for
another one without calling, the same one comes up again: this prevents people
from skipping through leads until they find one they like.

### Lead taken and never called
If **10 minutes** pass without dialing it, it goes back to the queue:
- If the lead **has an owner**, it goes back to the owner at its original priority.
- If it **has no owner**, it goes back to the general pool with **high priority** so
  the next free seller calls it right away, and it **gets unassigned in the CRM**
  (it had been assigned when taken, and they can't keep a lead they never called).

---

## 3. Dialing priorities

The order is defined by `TIPO_ORDEN` in `crm_webhooks.py`. Lower number = delivered
first.

| Order | Type | Why it's there |
|---|---|---|
| 0 | Appointment reminder | it's today; if not called, the appointment is lost |
| 1 | Missed call | the customer called and nobody answered: they're waiting |
| 2 | Pending conversation | already talked and was about to confirm something |
| 3 | Scheduled callback | asked to be called at a specific time |
| 4 | Replied via message | just wrote in: available right now |
| 5 | New lead | just arrived |
| 6 | Follow-up | attempts from later days |

Two details that matter:

**Callbacks are injected one minute before their time**, not earlier. Otherwise, a
5 p.m. callback would be in the way all morning.

**High priorities don't get downgraded.** If a lead came in as "missed call" and
then a "follow-up" injection arrives for the same contact, it keeps the high
priority for 24 hours. Without this protection, CRM noise would bury what's
urgent.

---

## 4. Calling

- The seller **never sees the full number**: the screen shows `***-***-1234`. They
  dial by contact identifier. This is what keeps anyone from walking off with the
  database.
- The system calls the seller's phone first and then dials the lead. That's why the
  seller "receives" their own outbound call.
- The number is stripped down to digits only before dialing. A `+` or a space kills
  the call silently.
- The rotator picks the caller ID (see `05-TELEPHONY.md`): the same lead always
  sees the same number.

### Double dialing
Calling the same lead twice in a row raises the contact rate noticeably: a lot of
people don't answer the first time but do the second.

The rule is: **after saving the disposition you can dial the same lead again**, as
many times as needed. The lead leaves the screen only once the seller asks for the
next one.

### No new call without a disposition
If the last call isn't disposed, the call button doesn't work, neither for the same
lead nor for another one. Without this rule, orphan calls with no outcome show up
and the metrics stop being useful.

It's enforced in two layers: the server responds with a specific error, and the
client also blocks the button. The server's is the one that rules.

---

## 5. Disposing

On hanging up, the agent enters ACW and the qualification screen opens. Saving the
disposition:

1. Records the outcome linked to that call's unique identifier.
2. Takes the agent out of ACW.
3. Triggers the write-back to the CRM (tags, notes, owner).
4. Enables dialing again.

**Dispositions must be listed alphabetically**, not by internal identifier. Sellers
pick them by memorized position; moving them around causes qualification errors.

### Optional control: "answered" only if the call connected
There's an option to require that, in order to save a conversation disposition,
the call must have actually been answered. It prevents someone from marking
"booked an appointment" on a call that never connected.

It ships **off** for a reason: voicemail counts as "answered" for the switch, so
the control can't tell a person from an answering machine apart. With transcription
active it can be turned on with judgment.

---

## 6. Lead owner ("sticky for life")

**A lead that conversed with a seller belongs to that seller forever.**

- **One** real-conversation disposition is enough to set the owner.
- From that moment on, redials, callbacks and inbound calls from that number
  always go to the same person.
- If they don't answer afterward, that **doesn't** take away the owner status.
- If the lead never conversed with anyone, it keeps rotating freely in the pool.
- The owner isn't lost even if the seller is deactivated or deleted: their leads
  stay in queue under their name. Reassigning is a manual decision, not an
  automatic one.

**Why:** without this, two sellers call the same customer, the customer gets
annoyed, and internally they fight over the commission. It's the rule that
prevents the most conflicts.

In the CRM this shows up as the contact being assigned to the seller. The full
cycle:

| Moment | What happens in the CRM |
|---|---|
| The seller takes the lead | it gets assigned to them, so they can open it and see the history |
| Disposes "answered" | it stays assigned permanently |
| Disposes "didn't answer" and the lead has no owner | it gets unassigned and goes back to rotating |
| Disposes "didn't answer" but already has an owner | **nothing** is touched: it stays the owner's |
| Took it and never called it (10 min) | it gets unassigned if it had no owner |

---

## 7. Single session

One seller, one device. The last one to log in kicks out the previous one, in both
directions (computer ↔ phone, and phone ↔ phone).

**Why:** two sessions of the same agent compete for the same SIP phone. Neither one
works properly and it's extremely hard to diagnose: calls "disappear" with no
error.

---

## 8. Inbound calls

- If the calling number **has an owner**, the call goes only to that seller's
  queue. If they're busy, in ACW or disconnected, it rings for a few seconds and
  drops: it isn't passed to anyone else.
- If it **has no owner**, it goes to the group: it rings on the free sellers.
- If nobody answers, the lead enters the dialing queue with high priority as
  "missed call". The customer called: they're waiting.
- An unknown number automatically creates a new contact.

---

## 9. What happens when something goes down

The system is designed to recover on its own. No cron assumes a previous command
went through: **they compare the real state against the desired state and correct
it in both directions.**

| Goes down | What happens |
|---|---|
| Django (application) | calls in progress continue; agents log back in on their own |
| Asterisk (switch) | pauses are restored from the desired state within the next minute |
| Redis (memory) | a watchdog restores the critical keys |
| The entire server | on boot, the persistence script reapplies all the patches |
| The number selector | a check restarts it; the dialplan has a backup list |

That last part matters: if the caller ID selector doesn't respond, the dialplan
uses a backup list of numbers instead of leaving the call with no caller ID. Never
let an optional piece be able to take down the call.
