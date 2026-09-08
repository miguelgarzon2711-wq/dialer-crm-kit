#!/usr/bin/env python3
"""
Launch BACKFILL (the operator: "the list we talked about"). Runs on the dialer HOST.
Reads Supabase (the client's dashboard): leads from the last N days; cross-references with appointments (Supabase) and with
GHL tags (dq / booked / comprador / solo whatsapp). Writes /root/backfill_leads.json with
the list and a summary. Does NOT inject anything (backfill_inject.py does that inside Django).

Buckets:
  - excluido_cita:        has an appointment (not cancelled) -> not called
  - excluido_dq:          dq*/booked/comprador/solo-whatsapp/wa-prefiere tag in GHL
  - excluido_sin_tel:     non-USA phone
  - pool:                 no reply from the salesperson -> everyone's queue (Seguimiento)
  - asignado:             the salesperson already replied -> THAT salesperson's queue (if they're in the dialer); if not, pool
Usage: python3 /root/backfill_prepare.py --dias 7
"""
import os, sys, json, argparse, subprocess, urllib.parse, time, re
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

ap = argparse.ArgumentParser(); ap.add_argument("--dias", type=int, default=30); ap.add_argument("--sin-ghl", action="store_true")
a = ap.parse_args()
env = {}
for line in open("/root/.env_dialer"):
    if "=" in line and not line.startswith("#"):
        k, v = line.strip().split("=", 1); env[k] = v
U, K = env.get("SUPABASE_URL", ""), env.get("SUPABASE_SERVICE_KEY", "")
GT, LOC = env.get("GHL_API_TOKEN", ""), env.get("GHL_LOCATION_ID", "")
assert U and K, "missing SUPABASE_URL / SUPABASE_SERVICE_KEY in /root/.env_dialer"

def sb(path, page=1000):
    """Paginated GET (PostgREST cuts off at 1000 rows per response)."""
    out, off = [], 0
    while True:
        sep = "&" if "?" in path else "?"
        url = U + "/rest/v1/" + path + sep + "offset=%d&limit=%d" % (off, page)
        j = None
        for i in range(4):
            r = subprocess.run(["curl", "-s", "-H", "apikey: " + K, "-H", "Authorization: Bearer " + K, url],
                               capture_output=True, text=True).stdout
            try:
                j = json.loads(r)
                if isinstance(j, list): break
            except Exception:
                pass
            j = None; time.sleep(2)
        if j is None: raise SystemExit("Supabase not responding: " + path)
        out.extend(j)
        if len(j) < page: return out
        off += page

def ghl_get(url, clave=None):
    """GET to GHL with rate-limit (429) handling and JSON error responses: if the response doesn't
    carry the expected key it's retried with increasing backoff; if it still fails it returns None
    (the lead is counted as ghl_error and is NOT injected blindly)."""
    for i in range(6):
        r = subprocess.run(["curl", "-s", "-m", "25", "-w", "\n%{http_code}", "-H", "Authorization: Bearer " + GT,
                            "-H", "Version: 2021-07-28", url], capture_output=True, text=True).stdout
        body, _, code = r.rpartition("\n")
        try:
            j = json.loads(body)
        except Exception:
            j = None
        if code == "200" and isinstance(j, dict) and (clave is None or clave in j):
            return j
        time.sleep(1.5 * (i + 1))
    return None

def ghl_tags(cid):
    """Returns (tags, assignedTo) of the contact in GHL (or (['__error__'],'') if GHL doesn't respond)."""
    if a.sin_ghl or not GT: return ([], "")
    j = ghl_get("https://services.leadconnectorhq.com/contacts/" + cid, "contact")
    if j is None: return (["__error__"], "")
    c = j.get("contact") or {}
    return ([t.lower() for t in (c.get("tags") or [])], (c.get("assignedTo") or "").strip())

def tiene_conversacion(cid):
    """True if the lead is already conversing via WhatsApp: they wrote at least once AFTER an
    outbound message (from the salesperson or the bot). The lead's first message (the ad one) doesn't count.
    Product decision rule: the salesperson finishes converting those via WhatsApp; they don't enter the dialer.
    Returns None if GHL didn't respond (counted as an error)."""
    if a.sin_ghl or not GT: return False
    j = ghl_get("https://services.leadconnectorhq.com/conversations/search?locationId=%s&contactId=%s" % (LOC, cid), "conversations")
    if j is None: return None
    convs = j.get("conversations") or []
    if not convs: return False
    for cv in convs:
        m = ghl_get("https://services.leadconnectorhq.com/conversations/%s/messages?limit=100" % cv.get("id"), "messages")
        if m is None: return None
        msgs = ((m.get("messages") or {}).get("messages")) if isinstance(m.get("messages"), dict) else (m.get("messages") or [])
        msgs = sorted(msgs, key=lambda x: x.get("dateAdded") or "")
        vio_saliente = False
        for x in msgs:
            d = (x.get("direction") or "").lower()
            if d == "outbound":
                vio_saliente = True
            elif d == "inbound" and vio_saliente:
                return True
    return False

desde = (datetime.now(timezone.utc) - timedelta(days=a.dias)).isoformat()
leads = sb("leads?select=ghl_contact_id,created_at,phone,full_name,ad_id,seller_id,first_response_at&created_at=gte." + urllib.parse.quote(desde) + "&order=created_at.asc")
citas = sb("appointments?select=lead_id,ghl_status&created_at=gte." + urllib.parse.quote((datetime.now(timezone.utc) - timedelta(days=a.dias + 30)).isoformat()) + "")
con_cita = {c["lead_id"] for c in citas if (c.get("ghl_status") or "").lower() != "cancelled"}
ads = {x["id"]: x for x in sb("ads?select=id,ad_set_id")}
adsets = {x["id"]: x for x in sb("ad_sets?select=id,campaign_id")}
camps = {x["id"]: x for x in sb("campaigns?select=id,name")}
sellers = {x["ghl_user_id"]: x["name"] for x in sb("sellers?select=ghl_user_id,name")}

def campana_de(ad_id):
    ad = ads.get(ad_id or ""); aset = adsets.get((ad or {}).get("ad_set_id") or "")
    name = (camps.get((aset or {}).get("campaign_id") or "") or {}).get("name", "")
    return ("mixto" if re.search(r"\bV5[23]\b", name, re.I) else "grupo1"), name

DQ_PREFIX = ("dq", "booked", "comprador", "solo whatsapp", "solo-whatsapp", "wa-prefiere", "prefiere whatsapp", "cita agendada", "numero-malo")
def es_dq(tags):
    return any(t.startswith(p) for t in tags for p in DQ_PREFIX)

def info_ghl(l):
    tags, assigned = ghl_tags(l["ghl_contact_id"])
    conv = tiene_conversacion(l["ghl_contact_id"])
    return (tags, assigned, conv)
with ThreadPoolExecutor(max_workers=3) as ex:
    tags_all = list(ex.map(info_ghl, leads))

out, resumen = [], {"total": len(leads), "excluido_cita": 0, "excluido_dq": 0, "excluido_sin_tel": 0, "excluido_conversacion": 0, "pool": 0, "asignado": 0, "ghl_error": 0}
por_camp = {"grupo1": 0, "mixto": 0}
for l, (tags, assigned_to, conv) in zip(leads, tags_all):
    tel = re.sub(r"\D", "", l.get("phone") or "")
    if conv is None or "__error__" in tags: resumen["ghl_error"] += 1; continue
    if len(tel) == 11 and tel.startswith("1"): tel = tel[1:]
    if l["ghl_contact_id"] in con_cita: resumen["excluido_cita"] += 1; continue
    if es_dq(tags): resumen["excluido_dq"] += 1; continue
    if len(tel) != 10: resumen["excluido_sin_tel"] += 1; continue
    if conv: resumen["excluido_conversacion"] += 1; continue           # already conversing via WhatsApp -> the salesperson handles it
    camp, cname = campana_de(l.get("ad_id"))
    bucket = "pool"                                                        # no salesperson -> everyone's queue
    resumen[bucket] += 1; por_camp[camp] += 1
    out.append({"ghl_id": l["ghl_contact_id"], "phone": tel, "nombre": (l.get("full_name") or "").strip() or tel,
                "campana": camp, "campana_ads": cname, "bucket": bucket, "seller_ghl": l.get("seller_id"), "assigned_to_ghl": assigned_to,
                "seller_nombre": sellers.get(l.get("seller_id") or "", ""), "created_at": l["created_at"], "tags": tags})
out.sort(key=lambda o: o["created_at"], reverse=True)   # most recent first
json.dump(out, open("/root/backfill_leads.json", "w"), indent=1, ensure_ascii=False)
resumen["por_campana"] = por_camp
resumen["asignado_por_vendedor"] = {}
for o in out:
    if o["bucket"] == "asignado":
        resumen["asignado_por_vendedor"][o["seller_nombre"] or "?"] = resumen["asignado_por_vendedor"].get(o["seller_nombre"] or "?", 0) + 1
print(json.dumps(resumen, indent=1, ensure_ascii=False))
print("file:", "/root/backfill_leads.json", "leads to inject:", len(out))
