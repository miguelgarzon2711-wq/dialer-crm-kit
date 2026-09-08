# Patches on top of OMniLeads

This kit **does not redistribute OMniLeads**. What's here are the specific changes that
need to be applied to the files your installation already ships, with the exact code
block and an explanation of why each one exists.

The blocks live in:

- `patches_django.txt` — changes to OMniLeads' Python files, templates and JavaScript.
- `patches_dialplan.txt` — changes to the Asterisk dialplan.

Each block comes with a reference line number and its context lines. The line
numbers correspond to **one** version of OMniLeads: in yours they'll be shifted.
**Don't apply by line number: look up the function or block by name.**

---

## How to apply a patch

1. Copy the original file from the container to the host:
   ```bash
   docker cp prod-env-django-app-1:/opt/omnileads/ominicontacto/<path>/<file>.py \
             /opt/dialer-kit/patches/<file>.py
   ```
2. Edit the host copy, applying the corresponding block.
3. Validate the syntax. **This step is not optional:** a mistake here brings down all of Django.
   ```bash
   python3 -c "import ast; ast.parse(open('<file>.py').read())" && echo OK
   ```
4. Copy it back and restart:
   ```bash
   docker cp /opt/dialer-kit/patches/<file>.py \
             prod-env-django-app-1:/opt/omnileads/ominicontacto/<path>/<file>.py
   docker restart prod-env-django-app-1
   ```
5. **Add the block to `restore_patches.sh`** so it survives restarts.
   If you skip this step, the patch disappears on its own and nobody will understand why.
6. Verify the application came back up:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" https://<YOUR_DOMAIN>/accounts/login/
   ```
   It has to return `200`. If not, check the container logs.

For JavaScript changes there are two extra steps after copying, because static
files are served compressed:
```bash
docker exec prod-env-django-app-1 python3 /opt/omnileads/ominicontacto/manage.py collectstatic --noinput
docker exec prod-env-django-app-1 python3 /opt/omnileads/ominicontacto/manage.py compress --force
```

For dialplan changes there's no need to restart anything:
```bash
docker exec prod-env-acd-1 asterisk -rx "dialplan reload"
```

---

## What each patch does and why

### In `models.py`

| Patch | What it solves |
|---|---|
| **Don't auto-finalize campaigns** | OMniLeads closes a Preview-type campaign on its own once it runs out of contacts. With leads trickling in throughout the day, the campaign closes by mid-morning and stops receiving. |
| **Unified delivery button** | Out of the box, the agent picks a campaign and then requests a lead. With this change, a single button delivers the highest-priority lead **across all** their campaigns. Fewer decisions for the rep, better dialing order. |
| **Pure priority** | Strictly respects the priority field when picking the next lead, instead of mixing it with other criteria. |
| **Multiple records per contact** | With re-injections, the same contact can have several agent-contact relationship records. Without this, saving the disposition of a re-injected lead throws an error. |
| **Preserve the external identifier** | Keeps the CRM's contact id inside the lead card, so it can be opened from the console. |
| **Validate before finalizing** | Runs the disposition validation **before** closing the agent-contact relationship, not after. If it validates after, it's already too late: the lead was closed with an invalid disposition. |

### Number masking (several files)

The rep never sees the lead's full phone number: they see `***-***-1234`. It has to be
masked in **five** places, and if you miss one the number leaks through there:

1. The lead card (`views_campana_preview.py`).
2. The disposition form (`views_calificacion_cliente.py`).
3. The browser phone, which receives the number via signaling (dialplan).
4. When dialing from the console: it arrives masked and the **real one must be restored**
   from the database before calling (`views_agente.py`).
5. When saving the disposition: same case, restore the real one (`views_calificacion_cliente.py`).

### Other

| Patch | File | What it solves |
|---|---|---|
| **Digits only when dialing** | `views_agente.py` | A `+` or a space kills the call in silence. Cleans the number no matter what the source data looks like. |
| **Dispositions in alphabetical order** | `forms_base.py` | Out of the box they come sorted by internal id. Reps pick them by position; if they move around, wrong dispositions get filed. |
| **Own domain allowed** | `settings` | Without this, any form submission from your domain returns a 403 error. |
| **API token lifetime** | `settings` | The API token lasts 9 hours out of the box. For webhooks that run forever, it's extended to a year. |
| **Unified button (interface)** | `campanasPreviewAgente.js` | The visual part of the single button. Requires the two static-file commands above. |
| **Release lead on pause** | `agent_activity.py` | If the agent goes on pause with a lead open, the lead is released instead of staying locked. |
| **Four application processes** | `oml_uwsgi.ini` | Out of the box it ships with just one: with several agents, one slow request blocks everyone. |

### In the dialplan (`patches_dialplan.txt`)

| Patch | What it solves |
|---|---|
| **Caller ID selector** | Before dialing, it asks the rotation service which number to use for that lead. It has a short timeout and a fallback list: if the service doesn't respond, the call goes out anyway. |
| **Guaranteed international format** | Some providers require the `+` with the country code. It's ensured without depending on the prefix configuration. |
| **Masked towards the agent** | On the leg towards the salesperson, the number travels masked. The leg towards the provider and the records keep the real number. |
| **Manual outbound route** | OMniLeads' route generator doesn't build this context correctly. It has to be written by hand (it's in `../asterisk/extensions_outbound_route.conf`). |
| **Internal test numbers** | Redirects a range of fake numbers to local contexts that simulate echo, voicemail and nobody-answers. Useful for testing the system without spending minutes or bothering anyone. Highly recommended for demos. |

---

## Recommendation

Apply the patches one at a time, verifying after each one. If you apply five
together and something breaks, you won't know which one it was.
