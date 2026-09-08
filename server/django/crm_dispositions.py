# -*- coding: utf-8 -*-
"""
Disposition engine -> GHL, Dialer (2026-08-22). NO Make.
When a disposition is saved: tags + note + owner cycle (assign salesperson if answered /
unassign if not answered). File: api_app/views/crm_dispositions.py
Activated by importing it from urls.py (post_save signal of CalificacionCliente).
"""
import logging
import threading

import requests
from django.db import connection
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from ominicontacto_app.models import CalificacionCliente
from api_app.views import lead_ownership  # PATCH STICKY DE POR VIDA (2026-09-03)

logger = logging.getLogger(__name__)

GHL_BASE = "https://services.leadconnectorhq.com"
GHL_VERSION = "2021-07-28"
ENV_PATH = "/opt/omnileads/.env_dialer"

# tags per disposition (adapted Colombia schema)
TAG_ADD = {
    "Agendo cita":                  ["cita-agendada-dialer"],
    "Va a agendar":                 ["va-a-agendar"],
    "Llamada de vuelta programada": ["llamar-mas-tarde"],
    "Prefiere WhatsApp":            ["prefiere-wa"],
    "No interesado":                ["dq-no-interesado"],
    "Ya compro":                    ["dq-ya-compro"],
    "Numero equivocado":            ["numero-malo"],
}
# "llamar-mas-tarde" is removed on EVERY disposition except when it's being set
QUITA_LLAMAR_MAS_TARDE_EXCEPTO = {"Llamada de vuelta programada"}
QUITA_VA_A_AGENDAR_EXCEPTO = {"Va a agendar"}

# assignment cycle (business rule: permanent owner ONLY after being answered)
CONTESTO = {"Agendo cita", "Va a agendar", "Llamada de vuelta programada", "Prefiere WhatsApp",
            "Solo queria precio", "No interesado", "Ya compro", "Colgo",
            "Error mio de ventas"}
NO_CONTESTO = {"No contesto", "Numero equivocado"}


def _env():
    cfg = {}
    try:
        for line in open(ENV_PATH):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                cfg[k] = v
    except Exception as e:
        logger.error("dispo: could not read %s: %s", ENV_PATH, e)
    # Alias: whoever prefers neutral names can write CRM_API_TOKEN instead
    # of GHL_API_TOKEN. Either one is accepted; whichever is set wins.
    for clave in list(cfg):
        if clave.startswith("CRM_"):
            cfg.setdefault("GHL_" + clave[4:], cfg[clave])
    return cfg


def _ghl_vendedor_de_agente(agente_id):
    """Mapping of the dialer login (BDC or salesperson) -> GHL user of the owning SALESPERSON."""
    with connection.cursor() as cur:
        cur.execute("SELECT ghl_user_id FROM dialer_agent_crm_map WHERE agente_id=%s", [agente_id])
        row = cur.fetchone()
    return row[0] if row else None


def _procesar(cal_id):
    try:
        cal = CalificacionCliente.objects.select_related(
            "opcion_calificacion", "contacto", "agente", "agente__user").get(id=cal_id)
    except CalificacionCliente.DoesNotExist:
        return
    ghl_id = (cal.contacto.id_externo or "").strip()
    if not ghl_id:
        return
    dispo = cal.opcion_calificacion.nombre
    agente_nombre = cal.agente.user.get_full_name() or cal.agente.user.username

    cfg = _env()
    token, loc = cfg.get("GHL_API_TOKEN"), cfg.get("GHL_LOCATION_ID")
    if not token:
        logger.error("dispo: missing GHL_API_TOKEN")
        return
    H = {"Authorization": "Bearer %s" % token, "Version": GHL_VERSION,
         "Content-Type": "application/json"}

    def _try(desc, fn):
        try:
            r = fn()
            logger.info("dispo %s: %s %s", ghl_id, desc, r.status_code)
        except Exception as e:
            logger.error("dispo %s: %s ERROR %s", ghl_id, desc, e)

    # 1) tags
    add = list(TAG_ADD.get(dispo, []))
    if add:
        _try("tags+%s" % add, lambda: requests.post(
            "%s/contacts/%s/tags" % (GHL_BASE, ghl_id), headers=H,
            json={"tags": add}, timeout=15))
    quitar = []
    if dispo not in QUITA_LLAMAR_MAS_TARDE_EXCEPTO:
        quitar.append("llamar-mas-tarde")
        # callback tags set by the GHL calendar workflow
        quitar.extend(["callback", "requested callback"])
    if dispo not in QUITA_VA_A_AGENDAR_EXCEPTO:
        quitar.append("va-a-agendar")
    if quitar:
        _try("tags-%s" % quitar, lambda: requests.delete(
            "%s/contacts/%s/tags" % (GHL_BASE, ghl_id), headers=H,
            json={"tags": quitar}, timeout=15))

    # 2) simple note ONLY for dispositions without conversation (Whisper covers the rest)
    if dispo in ("No contesto", "Numero equivocado"):
        _try("nota", lambda: requests.post(
        "%s/contacts/%s/notes" % (GHL_BASE, ghl_id), headers=H,
        json={"body": u"\U0001F4DE %s — %s" % (dispo, agente_nombre)}, timeout=15))

    # 3) owner cycle. LIFETIME STICKY: if the lead already has an owner, the GHL owner
    # is the owner and a later "No contesto" does NOT unassign it (product decision rule).
    _owner = lead_ownership.get_owner(cal.contacto_id)
    if dispo in CONTESTO:
        vendedor = _ghl_vendedor_de_agente(_owner if _owner > 0 else cal.agente_id)
        if vendedor:
            _try("owner=%s" % vendedor, lambda: requests.put(
                "%s/contacts/%s" % (GHL_BASE, ghl_id), headers=H,
                json={"assignedTo": vendedor}, timeout=15))
        else:
            logger.warning("dispo: agente %s has no GHL mapping (dialer_agent_crm_map)", cal.agente_id)
    elif dispo in NO_CONTESTO and _owner <= 0:
        _try("owner=null", lambda: requests.put(
            "%s/contacts/%s" % (GHL_BASE, ghl_id), headers=H,
            json={"assignedTo": None}, timeout=15))


@receiver(post_save, sender=CalificacionCliente, dispatch_uid="dispo_crm")
def on_calificacion_saved(sender, instance, **kwargs):
    # LIFETIME STICKY: the first conversation disposition sets the owner (synchronous)
    try:
        _nombre = instance.opcion_calificacion.nombre
        if _nombre in lead_ownership.DISPOS_CONVERSACION and instance.agente_id > 0:
            lead_ownership.set_owner(instance.contacto_id, instance.agente_id, _nombre)
    except Exception as e:
        logger.error("sticky set_owner (signal): %s", e)
    threading.Thread(target=_procesar, args=(instance.id,), daemon=True).start()


# ── Assignment when GETTING a lead (product decision) ─────────────────
# When the agent does "Obtener contacto" (AEC moves to ENTREGADO), the lead
# is assigned in GHL to the agent's mapped SALESPERSON — so "Ir al CRM" always
# shows the lead. The disposition cycle then keeps it (answered) or
# unassigns it (did not answer).
from ominicontacto_app.models import AgenteEnContacto, Contacto


def _asignar_on_entrega(aec_id):
    try:
        aec = AgenteEnContacto.objects.get(id=aec_id)
        if aec.estado != AgenteEnContacto.ESTADO_ENTREGADO or aec.agente_id <= 0:
            return
        contacto = Contacto.objects.filter(id=aec.contacto_id).first()
        ghl_id = (contacto.id_externo or "").strip() if contacto else ""
        if not ghl_id:
            return
        vendedor = _ghl_vendedor_de_agente(aec.agente_id)
        if not vendedor:
            return
        cfg = _env()
        token = cfg.get("GHL_API_TOKEN")
        if not token:
            return
        H = {"Authorization": "Bearer %s" % token, "Version": GHL_VERSION,
             "Content-Type": "application/json"}
        r = requests.put("%s/contacts/%s" % (GHL_BASE, ghl_id), headers=H,
                         json={"assignedTo": vendedor}, timeout=10)
        logger.info("entrega: %s owner=%s %s", ghl_id, vendedor, r.status_code)
    except Exception as e:
        logger.error("entrega error: %s", e)


@receiver(post_save, sender=AgenteEnContacto, dispatch_uid="deliver_owner")
def on_aec_saved(sender, instance, **kwargs):
    # LIFETIME STICKY: if OML returns a lead with an owner to the pool (agente -1), it's
    # returned to the owner right away (liberar_contacto, logout, etc.)
    if instance.estado == AgenteEnContacto.ESTADO_INICIAL and instance.agente_id == -1:
        try:
            _o = lead_ownership.get_owner(instance.contacto_id)
            if _o > 0:
                AgenteEnContacto.objects.filter(id=instance.id, agente_id=-1).update(agente_id=_o)
        except Exception as e:
            logger.error("sticky aec (signal): %s", e)
    if instance.estado == AgenteEnContacto.ESTADO_ENTREGADO:
        threading.Thread(target=_asignar_on_entrega, args=(instance.id,), daemon=True).start()

    # Lead on screen -> the salesperson does not receive inbound calls; once finished, they receive them again.
    _ag = instance.agente_id or -1
    if _ag > 0 and instance.estado in (AgenteEnContacto.ESTADO_ENTREGADO,
                                       AgenteEnContacto.ESTADO_ASIGNADO):
        threading.Thread(target=pausa_gestion, args=(_ag, True), daemon=True).start()
    elif _ag > 0 and instance.estado == AgenteEnContacto.ESTADO_FINALIZADO:
        def _quizas_despausar(agente_id):
            if not _tiene_lead_en_pantalla(agente_id):
                pausa_gestion(agente_id, False)
        threading.Thread(target=_quizas_despausar, args=(_ag,), daemon=True).start()



# ── GATE PATCH (product decision): anti-fraud "Prefiere WhatsApp without calling" ──────────
# A CONVERSATION disposition is only accepted if this delivery had an ANSWERED call
# (ANSWER event in LlamadaLog for that agent+contact, or for the calificacion's callid).
# Without an answer (didn't answer, voicemail, didn't even dial) only No contesto / Numero
# equivocado are accepted. Applies to Preview campanas 1-5. The form also filters the options
# (call_outcome view), but this is the real barrier (server-side).
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.utils import timezone as _tz
from reportes_app.models import LlamadaLog

GATE_ACTIVO = False   # product decision: free dispositions (not requiring an answered call)
GATE_CAMPANAS = {1, 2, 3, 4, 5}
DISPOS_SIN_CONTESTAR = {"No contesto", "Numero equivocado"}
GATE_VENTANA = timedelta(hours=3)
GATE_MSG = ("This call was NOT answered: you can only save 'No contesto' or 'Numero equivocado'. "
            "If the lead did answer, check that the call actually connected and try again.")


def llamada_contestada(agente_id, contacto_id, callid=None, desde=None):
    """True if there was an ANSWER from that agent to that contact in the window (or for that callid)."""
    try:
        if callid:
            if LlamadaLog.objects.filter(callid=callid, event="ANSWER").exists():
                return True
        desde = desde or (_tz.now() - GATE_VENTANA)
        return LlamadaLog.objects.filter(
            agente_id=agente_id, contacto_id=contacto_id, event="ANSWER", time__gte=desde).exists()
    except Exception as e:
        logger.error("DIALER gate llamada_contestada: %s", e)
        return True   # on a log error, don't block the agent


def resumen_llamadas(agente_id, contacto_id, desde=None):
    """For the form: {answered, attempts, last_event, last_duration}."""
    if not GATE_ACTIVO:
        return {"answered": True, "attempts": 0, "last_event": None, "last_duration": None, "gate": "off"}
    desde = desde or (_tz.now() - GATE_VENTANA)
    qs = LlamadaLog.objects.filter(agente_id=agente_id, contacto_id=contacto_id, time__gte=desde)
    attempts = qs.filter(event="DIAL").count()
    answered = qs.filter(event="ANSWER").exists()
    last = qs.order_by("-time").first()
    comp = qs.filter(event__in=["COMPLETEAGENT", "COMPLETEOUTNUM"]).order_by("-time").first()
    return {"answered": answered, "attempts": attempts,
            "last_event": last.event if last else None,
            "last_duration": comp.duracion_llamada if comp else None}


def validar_gate(instance):
    """Raises ValidationError if it's a conversation disposition without an answered call.
    Called by CalificacionCliente.save() BEFORE OML finalizes the AEC (otherwise a rejection
    would still pull the lead out of the queue anyway), and also the pre_save signal as a second barrier.
    DISABLED with GATE_ACTIVO=False (product decision)."""
    if not GATE_ACTIVO:
        return
    try:
        op = instance.opcion_calificacion
        nombre, camp = op.nombre, op.campana_id
    except Exception:
        return
    if camp not in GATE_CAMPANAS or nombre in DISPOS_SIN_CONTESTAR or not instance.agente_id or instance.agente_id <= 0:
        return
    if instance.pk:
        try:
            prev = CalificacionCliente.objects.only("opcion_calificacion_id").get(pk=instance.pk)
            if prev.opcion_calificacion_id == instance.opcion_calificacion_id:
                return   # disposition unchanged (e.g. observaciones): don't re-validate
        except CalificacionCliente.DoesNotExist:
            pass
    if not llamada_contestada(instance.agente_id, instance.contacto_id, instance.callid):
        logger.warning("DIALER gate: BLOCKED dispo '%s' agente=%s contacto=%s (no ANSWER)",
                       nombre, instance.agente_id, instance.contacto_id)
        raise ValidationError(GATE_MSG)


@receiver(pre_save, sender=CalificacionCliente, dispatch_uid="gate_dispo")
def on_calificacion_pre_save(sender, instance, **kwargs):
    validar_gate(instance)


# ── LEAD ON SCREEN = DOES NOT RECEIVE INBOUND (product decision) ────────
# While the salesperson has a delivered/assigned lead without a disposition, queue
# calls don't ring for them. Paused ONLY in Asterisk (QueuePause): OML's state is
# not touched and the lead is not released (OML's pause_agent would release it).
# Applies the same in the web console and in the mobile app.
# Safety net: /root/sync_agent_pause.sh unpauses every minute whoever no longer has a lead.
PAUSA_GESTION_ACTIVA = True
_REDIS_PAUSA_KEY = 'OML:DIALER:PAUSA_GESTION'


def _redis_ale():
    import redis as redis_lib
    from django.conf import settings
    return redis_lib.Redis(
        host=settings.REDIS_HOSTNAME,
        port=settings.CONSTANCE_REDIS_CONNECTION['port'],
        decode_responses=True)


def marcar_pausa_gestion(agente_id, pausado):
    try:
        r = _redis_ale()
        if pausado:
            r.sadd(_REDIS_PAUSA_KEY, str(agente_id))
        else:
            r.srem(_REDIS_PAUSA_KEY, str(agente_id))
    except Exception as e:
        logger.error("DIALER pausa gestion (redis) agente=%s: %s", agente_id, e)


def pausa_gestion(agente_id, pausar):
    """Pauses/unpauses the agent in their Asterisk queues for having a lead on screen."""
    if not PAUSA_GESTION_ACTIVA:
        return
    try:
        from ominicontacto_app.models import AgenteProfile
        from ominicontacto_app.services.asterisk.agent_activity import AgentActivityAmiManager
        agente = AgenteProfile.objects.filter(id=agente_id).first()
        if agente is None:
            return
        manager = AgentActivityAmiManager()
        manager.connect_manager()
        try:
            manager._queue_pause_unpause(agente, '0', 'pause' if pausar else 'unpause')
        finally:
            manager.disconnect_manager()
        marcar_pausa_gestion(agente_id, pausar)
        logger.info("DIALER pausa gestion: agente=%s %s", agente_id,
                    'paused (lead on screen)' if pausar else 'available')
    except Exception as e:
        logger.error("DIALER pausa gestion agente=%s pausar=%s: %s", agente_id, pausar, e)


def _tiene_lead_en_pantalla(agente_id):
    from ominicontacto_app.models import AgenteEnContacto
    return AgenteEnContacto.objects.filter(
        agente_id=agente_id,
        estado__in=[AgenteEnContacto.ESTADO_ENTREGADO, AgenteEnContacto.ESTADO_ASIGNADO],
    ).exists()


def revisar_pausas_gestion():
    """Anti-stuck-agent safety net: unpauses everyone who's marked and no longer
    has a lead on screen. Run by /root/sync_agent_pause.sh every minute."""
    liberados = []
    try:
        r = _redis_ale()
        for agente_id in list(r.smembers(_REDIS_PAUSA_KEY) or []):
            if not _tiene_lead_en_pantalla(int(agente_id)):
                pausa_gestion(int(agente_id), False)
                liberados.append(agente_id)
    except Exception as e:
        logger.error("DIALER revisar pausas gestion: %s", e)
    return liberados
