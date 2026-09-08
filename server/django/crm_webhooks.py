# -*- coding: utf-8 -*-
"""
GHL -> OMniLeads: lead injection/removal for the Dialer (El Negocio).
File: /opt/omnileads/ominicontacto/api_app/views/crm_webhooks.py
Adapted from another client's ghl_integration.py (2026-08-22). NO Make, NO deposits.
"""
import json
import logging
from django.db import connection
from django.utils import timezone
from datetime import timedelta

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.authentication import SessionAuthentication
from api_app.authentication import ExpiringTokenAuthentication
from ominicontacto_app.models import (
    Contacto, BaseDatosContacto, CalificacionCliente, AgenteEnContacto,
)

from api_app.views import lead_ownership  # PATCH STICKY DE POR VIDA (2026-09-03)
logger = logging.getLogger(__name__)

DB_ID = 1  # id of the OMniLeads contact database holding these leads

# Mapping of campana (stable parameter in GHL automations) -> OML campana_id.
# If the client reorganizes the groups, ONLY this dict needs to change — GHL URLs stay untouched.
CAMPANAS = {
    "grupo1": 1,
    "grupo2": 2,
    "grupo3": 3,
    "mixto":  4,
    "v25":    5,
}

# Automatic distribution of leads WITHOUT an ad id (?campana=auto):
# weight = number of agents in the group; assigned to the group with LEAST pending queue per agent.
# Only Grupo 1 (18 agents) and Mixto (Agente12+Agente13). G2/G3/V25 are no longer used.
AUTO_WEIGHTS = {1: 18, 4: 2}  # 2026-09-03: leads without ad id are split between Grupo 1 and Mixto (proportional)


def _resolver_auto(contacto):
    """Group for a lead with no source campana. Consistency: if it was already in a
    campana, repeat it. If new: the campana with the lowest (pending/agents) ratio."""
    if contacto:
        prev = AgenteEnContacto.objects.filter(
            contacto_id=contacto.id, campana_id__in=list(AUTO_WEIGHTS)
        ).order_by('-id').first()
        if prev:
            return prev.campana_id
    mejor, mejor_ratio = None, None
    for cid, peso in AUTO_WEIGHTS.items():
        pendientes = AgenteEnContacto.objects.filter(
            campana_id=cid,
            estado__in=[AgenteEnContacto.ESTADO_INICIAL, AgenteEnContacto.ESTADO_ENTREGADO],
        ).count()
        ratio = pendientes / float(peso)
        if mejor_ratio is None or ratio < mejor_ratio:
            mejor, mejor_ratio = cid, ratio
    return mejor


TIPO_ORDEN = {
    # Dialer priorities (product decision): lower = delivered first
    "Recordatorio Cita": 0,   # appointment day (morning + 1h before) -> ONLY to the salesperson who owns the appointment
    "Llamada Perdida":   1,   # the lead called and nobody answered
    "WA Cita Pendiente": 2,   # already talked, was going to confirm the time and wrote via WA
    "Callback":          3,   # "call me at 5"
    "WA Respondio":      4,   # replied via WA and is not assigned
    "Nuevo Lead":        5,
    "Seguimiento":       6,
}


def _dialer_live():
    """Launch switch: webhooks only inject if DIALER_LIVE=1
    in /opt/omnileads/.env_dialer. Before launch they respond ok without acting
    (the leads from those days are picked up by the backfill)."""
    try:
        for line in open("/opt/omnileads/.env_dialer"):
            if line.strip() == "DIALER_LIVE=1":
                return True
    except Exception:
        pass
    return False


def _normalize_phone(phone):
    """Leaves the USA number in 10 digits."""
    phone = "".join(c for c in str(phone) if c.isdigit())
    if len(phone) == 11 and phone.startswith("1"):
        phone = phone[1:]
    return phone


# Dispositions that count as "already talked" — a later WA Respondio makes the lead
# STICKY to the same agent (business rule: only that salesperson can touch the lead again)
STICKY_SI_HABLARON = {"Agendo cita", "Va a agendar", "Llamada de vuelta programada", "Prefiere WhatsApp",
                      "Solo queria precio", "Colgo", "Error mio de ventas"}


def _agent_si_conversaron(contacto_id, campaign_id):
    last_cal = CalificacionCliente.objects.filter(
        contacto_id=contacto_id,
        opcion_calificacion__campana_id=campaign_id,
        agente_id__gt=0,
    ).select_related("opcion_calificacion").order_by("-fecha").first()
    if last_cal and last_cal.opcion_calificacion.nombre in STICKY_SI_HABLARON:
        return last_cal.agente_id
    return -1


def _get_last_agent_for_contact(contacto_id, campaign_id):
    """For Callbacks: the last agent who dispositioned this contact in the campana."""
    last_cal = CalificacionCliente.objects.filter(
        contacto_id=contacto_id,
        opcion_calificacion__campana_id=campaign_id,
        agente_id__gt=0,
    ).order_by('-fecha').first()
    return last_cal.agente_id if last_cal else -1


TIPOS_PROTEGIDOS = ("Recordatorio Cita", "Llamada Perdida", "WA Cita Pendiente")


def _cita_ya_paso(valor):
    """True if `cita_inicio` (text sent by GHL) is a date/time that has ALREADY passed (Miami time).
    If it can't be parsed, False (don't block). The raw value is logged to help adjust formats."""
    if not valor:
        return False
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    v = str(valor).strip()
    tz = ZoneInfo("America/New_York")
    fmts = ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
            "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M",
            "%a, %b %d, %Y %I:%M %p", "%A, %B %d, %Y %I:%M %p", "%b %d, %Y, %I:%M %p", "%B %d, %Y, %I:%M %p",
            "%B %d, %Y at %I:%M %p", "%b %d, %Y at %I:%M %p", "%m/%d/%Y, %I:%M %p", "%Y-%m-%dT%H:%M", "%Y-%m-%d")
    dt = None
    for f in fmts:
        try:
            dt = _dt.strptime(v.replace("Z", "+0000"), f); break
        except ValueError:
            continue
    if dt is None:
        try:
            from dateutil import parser as _dp
            dt = _dp.parse(v)
        except Exception:
            logger.warning("DIALER cita_inicio not parseable: %r", v)
            return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt < _dt.now(tz)


def _agente_por_vendedor(ghl_user_id="", email=""):
    """Dialer agent from the GHL salesperson (user id or email)."""
    with connection.cursor() as cur:
        if ghl_user_id:
            cur.execute("SELECT agente_id FROM dialer_agent_crm_map WHERE ghl_user_id=%s", [ghl_user_id.strip()])
            r = cur.fetchone()
            if r: return int(r[0])
        if email:
            cur.execute("SELECT ap.id FROM ominicontacto_app_agenteprofile ap JOIN ominicontacto_app_user u ON u.id=ap.user_id "
                        "WHERE lower(u.email)=lower(%s) AND ap.borrado=false", [email.strip()])
            r = cur.fetchone()
            if r: return int(r[0])
    return -1


def _campanas_preview_del_agente(agente_id):
    """Preview campanas (1-5) where the agent is a queue member."""
    with connection.cursor() as cur:
        cur.execute("SELECT DISTINCT split_part(id_campana,'_',1)::int FROM queue_member_table WHERE member_id=%s",
                    [int(agente_id)])
        return {r[0] for r in cur.fetchall() if r[0] in (1, 2, 3, 4, 5)}


def _inject_in_preview(contacto_id, phone, nombre, campaign_id, ghl_id="", tipo=""):
    orden = TIPO_ORDEN.get(tipo, 6)
    datos_dict = json.dumps({"NOMBRE": nombre, "GHL_ID": ghl_id, "TIPO": tipo})
    # LIFETIME STICKY PATCH (product decision): if the lead has ALREADY TALKED with an
    # agent, EVERY injection (any type, any campana) goes only to that agent.
    assigned_agente_id = lead_ownership.get_owner(contacto_id)
    if tipo == "Recordatorio Cita" and assigned_agente_id <= 0:
        # business rule: the reminder ONLY goes to the owner of the lead/appointment; without an owner it's not injected
        logger.warning("inject Recordatorio Cita WITHOUT owner: contacto=%s -> not injected", contacto_id)
        return False
    if assigned_agente_id <= 0:
        if tipo in ("Callback", "WA Cita Pendiente"):
            assigned_agente_id = _get_last_agent_for_contact(contacto_id, campaign_id)
        elif tipo == "WA Respondio":
            assigned_agente_id = _agent_si_conversaron(contacto_id, campaign_id)
        else:
            assigned_agente_id = -1
        if assigned_agente_id > 0 and lead_ownership.agente_inactivo(assigned_agente_id):
            assigned_agente_id = -1
    # OWNER'S CAMPANA PATCH (product decision): if the lead already belongs to a salesperson
    # and arrives via a campana where that salesperson is NOT (e.g. Pepito from Grupo 1 and the
    # lead re-enters via Mixto), it's queued in the salesperson's campana: the lead is theirs and
    # they must be able to see it.
    if assigned_agente_id > 0:
        try:
            _camps = _campanas_preview_del_agente(assigned_agente_id)
            if _camps and campaign_id not in _camps:
                _prev = AgenteEnContacto.objects.filter(
                    contacto_id=contacto_id, campana_id__in=_camps).order_by("-modificado").first()
                _nuevo = _prev.campana_id if _prev else min(_camps)
                logger.info("inject: camp %s -> %s (owner's campana %s)", campaign_id, _nuevo, assigned_agente_id)
                campaign_id = _nuevo
        except Exception as _e:
            logger.error("inject owner's campana: %s", _e)
    try:
        en_llamada = AgenteEnContacto.objects.filter(
            contacto_id=contacto_id, campana_id=campaign_id,
            estado=AgenteEnContacto.ESTADO_ASIGNADO).exists()
        if en_llamada:
            return True
        existing = AgenteEnContacto.objects.filter(
            contacto_id=contacto_id, campana_id=campaign_id,
            estado__in=[AgenteEnContacto.ESTADO_INICIAL, AgenteEnContacto.ESTADO_ENTREGADO],
        ).first()
        if not existing:
            existing = AgenteEnContacto.objects.filter(
                contacto_id=contacto_id, campana_id=campaign_id,
                estado=AgenteEnContacto.ESTADO_FINALIZADO).order_by('-id').first()
        if existing:
            if existing.estado == AgenteEnContacto.ESTADO_FINALIZADO:
                AgenteEnContacto.objects.create(
                    agente_id=assigned_agente_id, contacto_id=contacto_id,
                    datos_contacto=datos_dict, telefono_contacto=phone,
                    campana_id=campaign_id, estado=AgenteEnContacto.ESTADO_INICIAL,
                    es_originario=True, orden=orden)
            else:
                # PRIORITY 0 SHIELD PATCH (product decision): a recent (<24h) Llamada Perdida or WA
                # Cita Pendiente in queue is NOT downgraded by a later injection of lower
                # priority (e.g. the "Nuevo Lead" webhook that arrives after creating the contact in GHL).
                try:
                    _dc = json.loads(existing.datos_contacto or "{}")
                    _tipo_prev = _dc.get("TIPO") if isinstance(_dc, dict) else None
                except Exception:
                    _tipo_prev = None
                _hace = (timezone.now() - existing.modificado) if existing.modificado else None
                if (orden > existing.orden and _tipo_prev in TIPOS_PROTEGIDOS
                        and _hace is not None and _hace < timedelta(hours=24)):
                    logger.info("inject: keeping priority 0 (%s) for contacto=%s; got '%s'",
                                _tipo_prev, contacto_id, tipo)
                    orden = existing.orden
                    datos_dict = json.dumps({"NOMBRE": nombre, "GHL_ID": ghl_id, "TIPO": _tipo_prev})
                existing.orden = orden
                existing.datos_contacto = datos_dict
                _uf = ["orden", "datos_contacto", "modificado"]
                if (existing.estado == AgenteEnContacto.ESTADO_INICIAL
                        and existing.agente_id != assigned_agente_id
                        and (assigned_agente_id > 0 or lead_ownership.agente_inactivo(existing.agente_id))):
                    existing.agente_id = assigned_agente_id
                    _uf.append("agente_id")
                existing.save(update_fields=_uf)
        else:
            AgenteEnContacto.objects.create(
                agente_id=assigned_agente_id, contacto_id=contacto_id,
                datos_contacto=datos_dict, telefono_contacto=phone,
                campana_id=campaign_id, estado=AgenteEnContacto.ESTADO_INICIAL,
                es_originario=True, orden=orden)
        # SINGLE CAMPANA PATCH (product decision): a lead never stays queued in two
        # Preview campanas at once; when injected into one, whatever it has queued in the others gets closed.
        try:
            AgenteEnContacto.objects.filter(
                contacto_id=contacto_id, campana_id__in=[1, 2, 3, 4, 5],
                estado=AgenteEnContacto.ESTADO_INICIAL).exclude(campana_id=campaign_id).update(
                estado=AgenteEnContacto.ESTADO_FINALIZADO)
        except Exception as _e:
            logger.error("inject mover: %s", _e)
        logger.info("inject ok: contacto=%s camp=%s orden=%s", contacto_id, campaign_id, orden)
        return True
    except Exception as e:
        logger.error("inject error: %s", e)
        return False


def _remove_from_preview(contacto_id, campaign_ids):
    try:
        updated = AgenteEnContacto.objects.filter(
            contacto_id=contacto_id, campana_id__in=campaign_ids,
        ).exclude(estado=AgenteEnContacto.ESTADO_ASIGNADO).update(
            estado=AgenteEnContacto.ESTADO_FINALIZADO)
        logger.info("DIALER remove: contacto=%s camps=%s rows=%s", contacto_id, campaign_ids, updated)
        return True
    except Exception as e:
        logger.error("remove error: %s", e)
        return False


class CRMLeadActionView(APIView):
    """
    POST /api/v1/ghl/lead_action/?campana=<grupo1|compartida|agente13|agente12|grupo3>
    JSON Body:
      action: "inject" | "remove"
      ghl_id: GHL contact id (required)
      phone:  phone (required if the contact is new)
      nombre: lead name
      tipo:   "Nuevo Lead" | "WA Respondio" | "Callback" | "Seguimiento"
      campana: alternative to the query param
    remove without campana => removes from ALL campanas (e.g: booked/DQ).
    Auth: OML Bearer token.
    """
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)

    def post(self, request):
        data = request.data
        # GHL sends the Custom Data nested under "customData" — flatten it
        if isinstance(data, dict) and not data.get("action"):
            cd = data.get("customData") or data.get("custom_data")
            if isinstance(cd, dict):
                merged = dict(data)
                merged.update(cd)
                data = merged
            else:
                logger.warning("DIALER payload without action; keys=%s", list(data.keys())[:25] if isinstance(data, dict) else type(data))
        action = data.get("action")
        ghl_id = (data.get("ghl_id") or "").strip()
        tipo = (data.get("tipo") or "").strip()
        campana_key = (request.query_params.get("campana") or data.get("campana") or "").strip().lower()

        if not action or not ghl_id:
            return Response({"error": "action and ghl_id are required"}, status=400)

        if tipo == "Recordatorio Cita" or action == "remove":
            # visible in docker logs (WARNING) to verify the format GHL sends
            logger.warning("DIALER RECORDATORIO received: action=%s tipo=%s ghl_id=%s cita_inicio=%r vendedor_email=%r vendedor_ghl=%r",
                           action, tipo, ghl_id, data.get("cita_inicio"), data.get("vendedor_email"), data.get("vendedor_ghl"))
        if not _dialer_live():
            logger.info("DIALER gated (pre-launch): action=%s ghl_id=%s campana=%s", action, ghl_id, campana_key)
            return Response({"status": "ok", "gated": True})

        contacto = Contacto.objects.filter(bd_contacto_id=DB_ID, id_externo=ghl_id).first()

        if action == "inject":
            if campana_key == "auto":
                campaign_id = _resolver_auto(contacto)
                ok_auto = True
            elif campana_key not in CAMPANAS:
                return Response({"error": "invalid or missing campana", "validas": list(CAMPANAS) + ["auto"]}, status=400)
            if campana_key != "auto":
                campaign_id = CAMPANAS[campana_key]
            nombre = (data.get("nombre") or "").strip()
            if not contacto:
                phone = _normalize_phone(data.get("phone", ""))
                if len(phone) != 10:
                    return Response({"error": "valid phone (10 dig) required for a new contact"}, status=400)
                bd = BaseDatosContacto.objects.get(id=DB_ID)
                contacto = Contacto.objects.create(
                    telefono=phone, datos=json.dumps([nombre, tipo]),
                    id_externo=ghl_id, bd_contacto=bd, es_originario=False)
            else:
                datos = json.loads(contacto.datos) if contacto.datos else []
                nombre_final = nombre or (datos[0] if datos else "")
                contacto.datos = json.dumps([nombre_final, tipo])
                contacto.save(update_fields=["datos"])
            # Colombia-style: if their last disposition was "Va a agendar" and they reply via WA
            # -> WA Cita Pendiente (orden 0, sticky to the same agent)
            if tipo == "WA Respondio" and contacto:
                last_cal = CalificacionCliente.objects.filter(
                    contacto_id=contacto.id,
                    opcion_calificacion__campana_id=campaign_id,
                    agente_id__gt=0).select_related("opcion_calificacion").order_by("-fecha").first()
                if last_cal and last_cal.opcion_calificacion.nombre == "Va a agendar":
                    tipo = "WA Cita Pendiente"
                    logger.info("DIALER upgrade WA Respondio -> WA Cita Pendiente: contacto=%s agente=%s",
                                contacto.id, last_cal.agente_id)
            if tipo == "Recordatorio Cita" and _cita_ya_paso(data.get("cita_inicio")):
                logger.info("Recordatorio Cita ignored: the appointment already passed (%s) ghl_id=%s", data.get("cita_inicio"), ghl_id)
                return Response({"status": "skipped", "reason": "appointment already passed", "cita_inicio": data.get("cita_inicio")}, status=200)
            if tipo == "Recordatorio Cita" and lead_ownership.get_owner(contacto.id) <= 0:
                # the appointment already implies conversation: the appointment's salesperson becomes the lead owner
                _ag = _agente_por_vendedor(str(data.get("vendedor_ghl") or ""), str(data.get("vendedor_email") or ""))
                if _ag > 0:
                    lead_ownership.set_owner(contacto.id, _ag, "Recordatorio Cita (appointment's salesperson)")
                else:
                    logger.warning("Recordatorio Cita without owner or mappable salesperson: ghl_id=%s vendedor_ghl=%s email=%s",
                                   ghl_id, data.get("vendedor_ghl"), data.get("vendedor_email"))
                    return Response({"status": "skipped", "reason": "no owner: send vendedor_ghl or vendedor_email"}, status=200)
            datos_list = json.loads(contacto.datos) if contacto.datos else []
            ok = _inject_in_preview(contacto.id, contacto.telefono,
                                    datos_list[0] if datos_list else "",
                                    campaign_id, ghl_id=ghl_id, tipo=tipo)
            if ok:
                return Response({"status": "ok", "contact_id": contacto.id,
                                 "campana": campana_key, "orden": TIPO_ORDEN.get(tipo, 6)})
            return Response({"error": "inject failed"}, status=500)

        elif action == "remove":
            if not contacto:
                return Response({"error": "contact not found"}, status=404)
            ids = [CAMPANAS[campana_key]] if campana_key in CAMPANAS else list(CAMPANAS.values())
            ok = _remove_from_preview(contacto.id, ids)
            if ok:
                return Response({"status": "ok", "contact_id": contacto.id, "campanas": ids})
            return Response({"error": "remove failed"}, status=500)

        return Response({"error": "invalid action"}, status=400)


class EnLlamadaView(APIView):
    """GET /api/v1/agente/en_llamada/ -> {en_llamada: bool}. Ported from another client
    (flow control for the disposition page). Active call = CONTACT_NUMBER
    set in Redis and STATUS without ACW."""
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)

    def get(self, request):
        import redis as redis_lib
        from django.conf import settings
        if not hasattr(request.user, 'agenteprofile'):
            return Response({"en_llamada": False})
        agente_id = request.user.agenteprofile.id
        try:
            r = redis_lib.Redis(
                host=settings.REDIS_HOSTNAME,
                port=settings.CONSTANCE_REDIS_CONNECTION['port'],
                decode_responses=True)
            key = 'OML:AGENT:{}'.format(agente_id)
            contact_number = r.hget(key, 'CONTACT_NUMBER') or ''
            status = r.hget(key, 'STATUS') or ''
        except Exception:
            contact_number = ''
            status = ''
        en_llamada = bool(contact_number) and 'ACW' not in status
        return Response({"en_llamada": en_llamada})


class ContactoHistorialView(APIView):
    """GET /api/v1/contact_history/?contacto_id=ID -> last 10 dispositions (preview panel)."""
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)

    def get(self, request):
        from zoneinfo import ZoneInfo
        contacto_id = request.query_params.get("contacto_id")
        if not contacto_id:
            return Response({"history": []})
        try:
            contacto_id = int(contacto_id)
        except ValueError:
            return Response({"error": "invalid contacto_id"}, status=400)
        tz = ZoneInfo("America/New_York")
        cals = CalificacionCliente.objects.filter(contacto_id=contacto_id).select_related(
            "opcion_calificacion", "agente__user").order_by("-modified")[:10]
        history = []
        for cal in cals:
            history.append({
                "fecha": cal.modified.astimezone(tz).strftime("%d/%m %H:%M") if cal.modified else "",
                "disposition": cal.opcion_calificacion.nombre if cal.opcion_calificacion else "-",
                "notes": cal.observaciones or "",
                "agent": cal.agente.user.get_full_name() if cal.agente else "-",
            })
        return Response({"history": history})


class SkipLeadView(APIView):
    """POST /api/v1/agente/skip_lead/ {contacto_id, campana_id, razon}: skips a
    WA Respondio / WA Cita Pendiente lead without dialing or disposition (AEC -> FINALIZADO)."""
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)

    def post(self, request):
        # SKIP DISABLED (product decision): nobody can skip leads; the attempt is logged
        logger.warning("DIALER SkipLead BLOCKED: agente=%s data=%s", getattr(request.user, "username", "?"), dict(request.data) if hasattr(request.data, "items") else request.data)
        return Response({"error": "Skipping leads is disabled"}, status=403)
        contacto_id = request.data.get("contacto_id")
        campana_id = request.data.get("campana_id")
        razon = (request.data.get("razon") or "").strip()
        if not contacto_id or not campana_id or not razon:
            return Response({"error": "contacto_id, campana_id and razon are required"}, status=400)
        aec = AgenteEnContacto.objects.filter(contacto_id=contacto_id, campana_id=campana_id).exclude(
            estado=AgenteEnContacto.ESTADO_FINALIZADO).first()
        if not aec:
            return Response({"error": "AEC not found or already finished"}, status=404)
        if aec.estado == AgenteEnContacto.ESTADO_ASIGNADO:
            return Response({"error": "There is an active call - cannot skip"}, status=409)
        datos = aec.datos_contacto or {}
        if isinstance(datos, str):
            try:
                datos = json.loads(datos)
            except Exception:
                datos = {}
        if datos.get("TIPO", "") not in ("WA Respondio", "WA Cita Pendiente"):
            return Response({"error": "Only WA Respondio / WA Cita Pendiente leads can be skipped"}, status=400)
        aec.estado = AgenteEnContacto.ESTADO_FINALIZADO
        aec.save(update_fields=["estado"])
        agente = request.user.username if request.user and request.user.is_authenticated else "?"
        try:
            from datetime import datetime as _dt
            with open("/var/log/skip_leads.log", "a") as f:
                f.write("%s | SKIP | agente=%s | contacto_id=%s | nombre=%s | ghl_id=%s | razon=%s\n" % (
                    _dt.now().strftime("%Y-%m-%d %H:%M:%S"), agente, contacto_id,
                    datos.get("NOMBRE", "?"), datos.get("GHL_ID", ""), razon))
        except Exception:
            pass
        logger.info("DIALER SkipLead: agente=%s contacto=%s razon=%s", agente, contacto_id, razon)
        return Response({"ok": True})



def _ghl_contacto_por_telefono(phone10, nombre_si_nuevo="Llamada perdida"):
    """Finds/creates the contact in GHL by phone (POST /contacts/upsert) and applies the
    llamada-perdida tag. Returns (ghl_id, is_new). (product decision: every missed call
    must exist in the CRM so it can be assigned and sent reminders.)"""
    try:
        import requests
        from api_app.views import crm_dispositions as _d
        cfg = _d._env()
        token, loc = cfg.get("GHL_API_TOKEN"), cfg.get("GHL_LOCATION_ID")
        if not token or not loc:
            logger.error("DIALER missed GHL: missing token/location")
            return "", False
        H = {"Authorization": "Bearer %s" % token, "Version": _d.GHL_VERSION,
             "Content-Type": "application/json"}
        r = requests.post("%s/contacts/upsert" % _d.GHL_BASE, headers=H,
                          json={"locationId": loc, "phone": "+1%s" % phone10}, timeout=8)
        j = r.json() if r.status_code in (200, 201) else {}
        cid = ((j.get("contact") or {}).get("id") or "").strip()
        nuevo = bool(j.get("new"))
        if not cid:
            logger.error("DIALER missed GHL upsert %s: %s %s", phone10, r.status_code, r.text[:200])
            return "", False
        if nuevo:
            requests.put("%s/contacts/%s" % (_d.GHL_BASE, cid), headers=H,
                         json={"firstName": nombre_si_nuevo, "source": "Llamada perdida (dialer)"}, timeout=8)
        requests.post("%s/contacts/%s/tags" % (_d.GHL_BASE, cid), headers=H,
                      json={"tags": ["llamada-perdida"]}, timeout=8)
        logger.info("DIALER missed GHL: tel=%s ghl_id=%s nuevo=%s", phone10, cid, nuevo)
        return cid, nuevo
    except Exception as e:
        logger.error("DIALER missed GHL error %s: %s", phone10, e)
        return "", False


class CRMMissedCallView(APIView):
    """POST /api/v1/dialer/missed_call/ {from: <caller>} — INBOUND call NOT answered
    (product decision): the lead enters the queue as "Llamada Perdida" (orden 0, delivered
    first). With an owner -> only to the owner and in their campana; without an owner -> to everyone
    (Grupo 1 or the campana it was already in). Unknown number -> the contact gets created. Bypasses the gate."""
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        phone = _normalize_phone(data.get("from") or request.query_params.get("from") or "")
        if len(phone) != 10:
            return Response({"error": "invalid from"}, status=400)
        tipo = "Llamada Perdida"
        ids = list(Contacto.objects.filter(bd_contacto_id=DB_ID, telefono=phone)
                   .order_by("-id").values_list("id", flat=True))
        contacto = None
        for cid in ids:                      # prefer the contact that ALREADY has an owner
            if lead_ownership.get_owner(cid) > 0:
                contacto = Contacto.objects.get(id=cid)
                break
        if contacto is None and ids:
            contacto = Contacto.objects.get(id=ids[0])
        # CRM: the contact must exist in GHL (search by phone; create if not) + tag
        ghl_id_actual = (contacto.id_externo or "").strip() if contacto else ""
        if ghl_id_actual and not ghl_id_actual.startswith("DEMO"):
            _ghl_contacto_por_telefono(phone)          # exists: just apply the llamada-perdida tag
            ghl_nuevo = ghl_id_actual
        else:
            ghl_nuevo, _ = _ghl_contacto_por_telefono(phone)
        if contacto is None:
            nombre = "Llamada perdida %s" % phone
            bd = BaseDatosContacto.objects.get(id=DB_ID)
            contacto = Contacto.objects.create(
                telefono=phone, datos=json.dumps([nombre, tipo]),
                id_externo=ghl_nuevo or "", bd_contacto=bd, es_originario=False)
        else:
            datos = json.loads(contacto.datos) if contacto.datos else []
            nombre = datos[0] if datos else phone
            contacto.datos = json.dumps([nombre, tipo])
            if ghl_nuevo and not (contacto.id_externo or "").strip():
                contacto.id_externo = ghl_nuevo
                contacto.save(update_fields=["datos", "id_externo"])
            else:
                contacto.save(update_fields=["datos"])
        last = AgenteEnContacto.objects.filter(
            contacto_id=contacto.id, campana_id__in=[1, 2, 3, 4, 5]).order_by("-modificado").first()
        campaign_id = last.campana_id if last else CAMPANAS.get("grupo1", 1)
        ok = _inject_in_preview(contacto.id, contacto.telefono, nombre, campaign_id,
                                ghl_id=(contacto.id_externo or ""), tipo=tipo)
        logger.info("missed call: tel=%s contacto=%s camp=%s dueno=%s ok=%s",
                    phone, contacto.id, campaign_id, lead_ownership.get_owner(contacto.id), ok)
        return Response({"status": "ok" if ok else "error", "contacto_id": contacto.id,
                         "campana": campaign_id, "dueno": lead_ownership.get_owner(contacto.id)})



class CRMCallOutcomeView(APIView):
    """GET /api/v1/agente/call_outcome/?contacto_id=N -> {answered, attempts, last_event, last_duration}
    for the logged-in agent with that contact in the last window. The disposition form uses it
    to only leave 'No contesto'/'Numero equivocado' when the call wasn't answered."""
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)

    def get(self, request):
        if not hasattr(request.user, "agenteprofile"):
            return Response({"answered": True, "attempts": 0})
        try:
            contacto_id = int(request.query_params.get("contacto_id") or 0)
        except ValueError:
            contacto_id = 0
        if contacto_id <= 0:
            return Response({"answered": True, "attempts": 0})
        from api_app.views import crm_dispositions as _d
        return Response(_d.resumen_llamadas(request.user.agenteprofile.id, contacto_id))
