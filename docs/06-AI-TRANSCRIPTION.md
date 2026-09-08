# 06 — AI Transcription

This document explains what `server/scripts/transcribe_calls.py` (the
transcription cron) does, how to adapt it to a different client, and what
to check when something does not show up. It is written from the actual
code of the script, of `server/scripts/run_transcribe.sh`, and of
`server/sql/schema.sql`. Where the code does not say something, this
document does not invent it either — it is marked as ambiguous or as
"not included in the kit."

---

## 1. What it is for

Two things, using the same work (transcribing the call audio):

1. **Automatic CRM note.** After the salesperson dispositions a call, the
   system downloads the recording, transcribes it, and asks a language
   model to write a note in the agent's first person ("I spoke with...",
   "they told me..."). The salesperson writes nothing — the note appears on
   the CRM contact by itself.
2. **Voicemail audit.** The phone system counts an answered voicemail as an
   "answered call." A salesperson can (by mistake or on purpose) dispose
   that call as if they had spoken with a person. The script reads the
   transcript, detects whether it was actually an automated answering
   machine, and if the disposition says otherwise, leaves an alert.

Business value: better CRM records without depending on the salesperson
having the discipline to write notes, plus a way to detect inflated
dispositions that no other part of the system can see (the phone system
only knows whether the call "connected," not whether it connected with a
person).

---

## 2. How the process works, step by step

The script runs via cron (see `run_transcribe.sh`) and on each run:

### 2.1 Find what to process

`get_dispositions()` pulls from the OMniLeads database all the
qualifications (`ominicontacto_app_calificacioncliente`) modified in the
**last 48 hours**, for campaigns `1,2,3,4,5`, with the phone number, the
agent's name, and the contact's CRM `id_externo` (if already known).

Dispositions that are in `SKIP_DISPOSITIONS` (in the kit,
`"No contesto"` and `"Numero equivocado"`) are discarded — those do not
carry a real conversation and are handled by a separate automation outside
this script.

### 2.2 Find the recording

For each disposition, `find_recordings()` looks in
`reportes_app_llamadalog` for calls from the same contact and campaign,
with `duracion_llamada > 30` seconds, with event `COMPLETEAGENT` or
`COMPLETEOUTNUM` (a call that connected), within a window of **90 minutes
before to 5 minutes after** the disposition time.

If no recording shows up, the script waits 3 minutes (in case the
disposition is too recent) and then publishes a simple note to the CRM
without a transcription: `"📞 <disposition> — <agent>"`. This happens, for
example, when the call was so short it did not reach the 30-second
minimum.

### 2.3 Download the audio

Recordings live in **MinIO** (S3-compatible object storage), bucket
`omnileads`. `download_recording()` builds several candidate file names
— the database's `archivo_grabacion` field if it exists, or generated
patterns like `previewCall-<callid>`, `click2Call-<callid>`, or
`Inbound-<callid>` — and tries the disposition's date folder plus one day
before/after (in case the server's clock and the recording's clock do not
match exactly). The first name and date that exists in the bucket is the
one used.

### 2.4 Transcribe and detect language

`transcribe_audio()` sends the mp3 to OpenAI's transcription endpoint
(`whisper-1`), **without setting the `language` parameter** — Whisper
detects the language on its own. This matters because a voicemail greeting
can be in a different language than the actual conversation (for example,
a Latino market in the US with voicemail greetings in English). The
detected language is saved for the audit row. The script also sends a
business-context `prompt` to help Whisper with domain-specific vocabulary
(product names, industry jargon) — the kit ships an example built for a
car dealership in Miami that needs to be replaced.

### 2.5 Generate the note

`generate_crm_note()` calls OpenAI's chat completion (`gpt-4o-mini`) with a
different system prompt depending on whether the call is outbound
(`SYSTEM_PROMPT_OUTBOUND`) or inbound (`SYSTEM_PROMPT_INBOUND`, campaign
`campana_id == CAMPAIGN_INBOUND`). It passes the disposition, the total
duration, and the full transcript (if there were several recordings for
the same disposition, they are concatenated with `\n---\n`). The prompt
asks for flowing text, in the agent's first person, with business-specific
rules (which data must never be omitted, tone, maximum length). The inbound
prompt also lets the model literally reply `"skip"` when the audio is
unintelligible or silent — **the outbound prompt, as it ships in the kit,
does not have that instruction explicitly**, although the code does check
whether the response is `"skip"` for both cases.

### 2.6 Publish to the CRM (and save a local copy)

`post_ghl_note()` publishes the note to the CRM contact via
`POST /contacts/{id}/notes` of the GoHighLevel API, with the text:
`"<emoji> <disposition> — <agent>\n\n<note>"` (📞 outbound, 📲 inbound).
If the contact did not yet have `id_externo` saved in the dialer,
`lookup_ghl_by_phone()` looks it up by phone number before publishing.

In addition, `guardar_nota_local()` writes the same note to the
`nota_crm` column of `dialer_call_audit`, so that a dialer's own console or
app can show it instantly without having to query the CRM.

---

## 3. Cost control

Whisper is charged per audio processed, and GPT is charged per token in the
note, so the script is built to **transcribe each audio only once in its
entire lifetime**, no matter how many times the cron runs:

- **Tracking by `callid`** (Asterisk's `uniqueid`, not the disposition id):
  `STATE_FILE` (`/var/log/transcripciones_procesadas.json`) stores the set
  of `callids` already processed successfully. Before downloading or
  transcribing an audio, the script discards any `callids` already in that
  set.
- **Transcription cache**: `CACHE_FILE`
  (`/var/log/transcripts_cache.json`) stores the transcribed text by
  `callid`, as soon as it is obtained, **before** trying to generate or
  publish the note. If publishing to the CRM fails and the `callid` does
  not get marked as processed, the next run retries the note and the
  publish, but **does not pay for Whisper again** — it takes the text from
  the cache.
- **Lock against concurrent runs**: `run_transcribe.sh` wraps the execution
  in `flock -n /tmp/transcribe_calls.lock`. If the cron fires a new run
  while the previous one is still alive, the new one exits silently
  without doing anything — this prevents two processes from downloading
  and transcribing the same audio at the same time.
- Other filters that also lower cost: `MIN_DURATION_SECS = 30`
  (very short calls are not transcribed), the 48-hour window in the
  disposition query, and `SKIP_DISPOSITIONS` (dispositions without a real
  conversation never enter the transcription flow).

Why it matters: without this mechanism, every cron run could re-transcribe
the same audio if the note fails to publish or if state is lost,
multiplying the API cost with no added benefit.

---

## 4. Voicemail detection

`BUZON_PATTERNS` is a list of regular expressions, in Spanish and English,
for typical answering-machine greeting phrases (e.g.:
`"deje su mensaje"`, `"después del tono"`, `"buzón de voz"`,
`"leave a message"`, `"after the beep"`, `"mailbox"`, `"press one"`).
`es_buzon()` looks for those patterns **only in the first 600 characters**
of the transcript, because the voicemail greeting is always at the
beginning of the call; a single matching pattern is enough to flag it as
voicemail.

When the salesperson disposed the call with a disposition that is **not**
in `SKIP_DISPOSITIONS` (i.e. they marked it as some kind of conversation)
but the audio turned out to be voicemail, `registrar_audit()` sets
`alerta = true` in `dialer_call_audit` and adds a line to
`/var/log/voicemail_alerts.log` with date, agent, disposition, contact,
campaign, `callid`, duration, detected language, and the first 120
characters of the transcript. It also prints a warning to the cron log
(`/var/log/transcripciones.log`, depending on how `run_transcribe.sh` is
configured).

This mechanism is **free of additional cost**: it reuses the transcription
already paid for the CRM note, with no extra API call.

---

## 5. The audit table

`dialer_call_audit` (defined in `server/sql/schema.sql`, and recreated
idempotently by `ensure_audit_table()` inside the script if it does not
exist yet):

| Column | What it stores |
|---|---|
| `callid` (PK) | Asterisk's `uniqueid` — uniquely identifies the call. |
| `contacto_id` | Contact id in the dialer. |
| `agente` | Full name of the agent who dispositioned the call. |
| `disposicion` | Name of the disposition chosen by the agent. |
| `campana_id` | Campaign the call belongs to. |
| `duracion` | Call duration in seconds. |
| `es_buzon` | `true` if the AI detected an answering-machine greeting. |
| `idioma` | Language detected by Whisper (`es`, `en`, ...); can be left empty if the row comes from cache without a new language value. |
| `alerta` | `true` if the agent disposed as a real conversation something that was voicemail. |
| `transcript_inicio` | First 300 characters of the transcript, to review without having to find the audio. |
| `fecha` | When the row was recorded (`now()` on insert). |
| `nota_crm` | The note drafted by the AI, to show in a dialer-native console without depending on the CRM. |

It serves three purposes: not re-transcribing the same audio (section 3),
detecting voicemail disguised as a conversation (section 4), and feeding
custom reports (the code's comment mentions a `call_report.py` that reads
this table, **not included in this kit**).

**Watch out when adapting:** `ensure_audit_table()` creates the table
without the `nota_crm` column if it has to create it from scratch (that
column only exists in `schema.sql`). Since the kit's install order requires
applying `schema.sql` before running the script (see `CLAUDE.md`, Step 4),
in practice the table is always born complete. If for some reason the
script were to run first, `guardar_nota_local()` would fail trying to
write to a column that does not exist — the error is caught and only
printed to the log, it does not break the script, but the local note does
not get saved.

---

## 6. How to adapt it

| What to change | Where |
|---|---|
| Language and tone of the note | `SYSTEM_PROMPT_OUTBOUND` / `SYSTEM_PROMPT_INBOUND` |
| Business context (industry, what is sold, which data is critical) | Same two prompts — replace the car dealership example |
| Domain-specific vocabulary to help Whisper | The `prompt` inside `transcribe_audio()` |
| Note format (paragraphs, max length, critical rules) | Inside each `SYSTEM_PROMPT_*` |
| Which dispositions NOT to transcribe | `SKIP_DISPOSITIONS` |
| Which campaigns to audit | The `IN (1,2,3,4,5)` in `get_dispositions()` and `CAMPAIGN_INBOUND` (currently `-1`, i.e. no inbound campaign set up — set it to the client's actual inbound campaign id if applicable) |
| Minimum duration to transcribe | `MIN_DURATION_SECS` |
| How often it runs | The cron line that invokes `run_transcribe.sh` |
| Voicemail phrases if the language or market changes | `BUZON_PATTERNS` |
| Timezone used for text dates and MinIO folders | The `BOGOTA` variable (the name stuck from the original Bogotá-based operator; what matters is the value of `ZoneInfo(...)`, not the variable's name) |
| MinIO, OpenAI, and CRM credentials | `/root/.env_dialer` — never hardcoded in the script |

About "how often it runs": the cron line must follow standard hour-range
syntax. A range crossing midnight written as `12-02` **is not valid** in
cron (the first number must be smaller than the second); it has to be
split in two, for example `0-2,12-23`.

---

## 7. Costs and considerations

Two OpenAI API calls are billed:

- **Transcription** (`whisper-1`): billed per minute of audio processed.
- **Note drafting** (`gpt-4o-mini`): billed per input tokens (the full
  transcript) and output tokens (the note, capped at 200 tokens). Only
  **one note call per disposition**, even if there are several grouped
  recordings.

The script has no hardcoded prices — the current OpenAI rate for both
models must be checked at install time. To estimate monthly spend: average
duration of transcribable calls (>30s) × number of calls transcribed per
day (after discounting `SKIP_DISPOSITIONS` and the ones with no recording
found) × 30 days, for the Whisper cost; plus one `gpt-4o-mini` note for
each of those calls for the drafting cost (usually cents per note, given
the 200-token output cap).

**If `OPENAI_API_KEY` is not set in `/root/.env_dialer`**, the script
prints `"OPENAI_API_KEY no configurada — abortando"` and exits immediately
(`sys.exit(0)`, no cron error). Nothing gets transcribed, no note or audit
row is generated — but since this script is a cron independent from the
rest of the dialer, the rest of the system (dialing, queues, dispositions,
CRM writes that do not depend on AI) keeps working with no issue at all.

---

## 8. Privacy

The audio of every transcribed call is sent to OpenAI's API (an external
provider, outside the dialer's own infrastructure) for transcription and
note drafting. This means customer and salesperson voices, and any data
mentioned during the call (names, figures, personal situations), leave the
dialer's own server toward a third party.

It is worth telling the client this explicitly before turning the module
on, so they can decide whether to disclose it to their own customers or
adjust their existing call-recording notice. This script neither adds to
nor removes anything from consent for recording the call itself (that
should already be handled at the telephony layer) — it only adds extra
processing on top of an audio that was already being recorded.

---

## 9. Troubleshooting

If no new notes appear in the CRM, check in this order:

1. **Did the cron run?** Check `/var/log/transcripciones.log` (or whatever
   log the actual cron defines) and confirm there is a line with the
   expected date/time and the count of dispositions found.
2. **Is there a stale lock?** If a previous process died halfway through,
   `/tmp/transcribe_calls.lock` may still be held, and `flock -n` makes all
   subsequent runs exit silently without doing anything. Check whether the
   process holding the lock is still alive.
3. **Is `OPENAI_API_KEY` set in `/root/.env_dialer`?** Without it the
   script does nothing (section 7).
4. **Is `GHL_READ_TOKEN` set?** If the contact did not have `id_externo`
   saved and there is no read token, `lookup_ghl_by_phone()` never finds
   the CRM id and that disposition gets marked as processed without
   publishing anything (to avoid retrying it in a loop). Also check
   `GHL_TAGS_API_KEY` / `GHL_API_TOKEN`, which are the ones
   `post_ghl_note()` uses to publish (separate variables from the read
   token).
5. **Does the disposition fall within the 48-hour window and the filtered
   campaigns?** `get_dispositions()` only looks at `campana_id IN (1,2,3,4,5)`
   and `cc.modified` from the last 48h.
6. **Is the disposition in `SKIP_DISPOSITIONS`?** Those are never
   transcribed by design.
7. **Was a recording found?** If `find_recordings()` finds nothing
   (duration under 30s, an event other than `COMPLETEAGENT`/
   `COMPLETEOUTNUM`, or outside the 90-min-before/5-min-after window), the
   result is a simple note with no AI content — not a total absence of a
   note. If even that simple note does not appear, check point 4 (missing
   `ghl_id`).
8. **Could the audio be downloaded from MinIO?** If `download_recording()`
   does not find any of the candidate names on any of the three dates it
   tries, after 15 minutes of age only the simple note is published as
   well. Check that the file name in `archivo_grabacion` or the pattern
   (`previewCall-`, `click2Call-`, `Inbound-`) matches how OMniLeads
   actually names recordings on that server.
9. **Did at least 3 minutes pass since the disposition?** The script waits
   that long on purpose, to give the recording time to finish uploading to
   MinIO. It is normal not to see a note right after dispositioning.
10. **Did the model reply `"skip"`?** That is a valid result (unintelligible
    audio or no human voice) — check that run's log, it explicitly says
    `"GPT: skip"`.
11. **Was the state or cache file lost?** If `STATE_FILE` or `CACHE_FILE`
    get corrupted or deleted, the script treats everything within the 48h
    window as new again — it can generate duplicate notes in the CRM (it
    does not duplicate the Whisper cost if the cache survived, but if both
    files were lost, everything gets paid for again). Check permissions
    and disk space in `/var/log/`.
12. **Does the `dialer_call_audit` table exist with the `nota_crm` column?**
    See the note at the end of section 5.
