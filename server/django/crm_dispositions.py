# -*- coding: utf-8 -*-
"""
Motor disposiciones -> GHL, Dialer (2026-08-22). SIN Make.
Al guardar una disposicion: tags + nota + ciclo de owner (asignar vendedor si contesto /
desasignar si no contesto). Archivo: api_app/views/crm_dispositions.py
Se activa importandolo desde urls.py (signal post_save de CalificacionCliente).
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

# tags por disposicion (esquema Colombia adaptado)
TAG_ADD = {
    "Agendo cita":                  ["cita-agendada-dialer"],
    "Va a agendar":                 ["va-a-agendar"],
    "Llamada de vuelta programada": ["llamar-mas-tarde"],
    "Prefiere WhatsApp":            ["prefiere-wa"],
    "No interesado":                ["dq-no-interesado"],
    "Ya compro":                    ["dq-ya-compro"],
    "Numero equivocado":            ["numero-malo"],
}
# "llamar-mas-tarde" se quita en TODA disposicion excepto cuando se esta poniendo
QUITA_LLAMAR_MAS_TARDE_EXCEPTO = {"Llamada de vuelta programada"}
QUITA_VA_A_AGENDAR_EXCEPTO = {"Va a agendar"}

# ciclo de asignacion (regla de negocio: owner permanente SOLO tras contestacion)
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
        logger.error("dispo: no pude leer %s: %s", ENV_PATH, e)
    # Alias: quien prefiera nombres neutrales puede escribir CRM_API_TOKEN en vez
    # de GHL_API_TOKEN. Se acepta cualquiera de los dos; gana el que este puesto.
    for clave in list(cfg):
        if clave.startswith("CRM_"):
            cfg.setdefault("GHL_" + clave[4:], cfg[clave])
    return cfg


def _ghl_vendedor_de_agente(agente_id):
    """Mapeo login dialer (BDC o vendedor) -> user GHL del VENDEDOR dueno."""
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
        logger.error("dispo: sin GHL_API_TOKEN")
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
        # tags de callback que pone el workflow del calendario en GHL
        quitar.extend(["callback", "requested callback"])
    if dispo not in QUITA_VA_A_AGENDAR_EXCEPTO:
        quitar.append("va-a-agendar")
    if quitar:
        _try("tags-%s" % quitar, lambda: requests.delete(
            "%s/contacts/%s/tags" % (GHL_BASE, ghl_id), headers=H,
            json={"tags": quitar}, timeout=15))

    # 2) nota simple SOLO para dispos sin conversacion (el resto lo cubre Whisper)
    if dispo in ("No contesto", "Numero equivocado"):
        _try("nota", lambda: requests.post(
        "%s/contacts/%s/notes" % (GHL_BASE, ghl_id), headers=H,
        json={"body": u"\U0001F4DE %s — %s" % (dispo, agente_nombre)}, timeout=15))

    # 3) ciclo de owner. STICKY DE POR VIDA: si el lead ya tiene dueno, el owner GHL
    # es el dueno y un "No contesto" posterior NO lo desasigna (regla decisión de producto).
    _owner = lead_ownership.get_owner(cal.contacto_id)
    if dispo in CONTESTO:
        vendedor = _ghl_vendedor_de_agente(_owner if _owner > 0 else cal.agente_id)
        if vendedor:
            _try("owner=%s" % vendedor, lambda: requests.put(
                "%s/contacts/%s" % (GHL_BASE, ghl_id), headers=H,
                json={"assignedTo": vendedor}, timeout=15))
        else:
            logger.warning("dispo: agente %s sin mapeo GHL (dialer_agent_crm_map)", cal.agente_id)
    elif dispo in NO_CONTESTO and _owner <= 0:
        _try("owner=null", lambda: requests.put(
            "%s/contacts/%s" % (GHL_BASE, ghl_id), headers=H,
            json={"assignedTo": None}, timeout=15))


@receiver(post_save, sender=CalificacionCliente, dispatch_uid="dispo_crm")
def on_calificacion_saved(sender, instance, **kwargs):
    # STICKY DE POR VIDA: la primera disposicion de conversacion fija el dueno (sincrono)
    try:
        _nombre = instance.opcion_calificacion.nombre
        if _nombre in lead_ownership.DISPOS_CONVERSACION and instance.agente_id > 0:
            lead_ownership.set_owner(instance.contacto_id, instance.agente_id, _nombre)
    except Exception as e:
        logger.error("sticky set_owner (signal): %s", e)
    threading.Thread(target=_procesar, args=(instance.id,), daemon=True).start()


# ── Asignación al OBTENER lead (decisión decisión de producto) ─────────────────
# Cuando el agente da "Obtener contacto" (AEC pasa a ENTREGADO), se asigna el
# lead en GHL al VENDEDOR mapeado del agente — así "Ir al CRM" siempre muestra
# el lead. El ciclo de disposición luego lo mantiene (contestó) o lo
# desasigna (no contestó).
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
    # STICKY DE POR VIDA: si OML devuelve al pool (agente -1) un lead con dueno, se lo
    # regresa al dueno de inmediato (liberar_contacto, logout, etc.)
    if instance.estado == AgenteEnContacto.ESTADO_INICIAL and instance.agente_id == -1:
        try:
            _o = lead_ownership.get_owner(instance.contacto_id)
            if _o > 0:
                AgenteEnContacto.objects.filter(id=instance.id, agente_id=-1).update(agente_id=_o)
        except Exception as e:
            logger.error("sticky aec (signal): %s", e)
    if instance.estado == AgenteEnContacto.ESTADO_ENTREGADO:
        threading.Thread(target=_asignar_on_entrega, args=(instance.id,), daemon=True).start()

    # Lead en pantalla -> el vendedor no recibe entrantes; al finalizarlo vuelve a recibir.
    _ag = instance.agente_id or -1
    if _ag > 0 and instance.estado in (AgenteEnContacto.ESTADO_ENTREGADO,
                                       AgenteEnContacto.ESTADO_ASIGNADO):
        threading.Thread(target=pausa_gestion, args=(_ag, True), daemon=True).start()
    elif _ag > 0 and instance.estado == AgenteEnContacto.ESTADO_FINALIZADO:
        def _quizas_despausar(agente_id):
            if not _tiene_lead_en_pantalla(agente_id):
                pausa_gestion(agente_id, False)
        threading.Thread(target=_quizas_despausar, args=(_ag,), daemon=True).start()



# ── PATCH GATE (decisión de producto): anti-fraude "Prefiere WhatsApp sin llamar" ──────────
# Una disposicion de CONVERSACION solo se acepta si en esta entrega hubo una llamada CONTESTADA
# (evento ANSWER en LlamadaLog para ese agente+contacto, o para el callid de la calificacion).
# Sin contestacion (no contesto, buzon, ni siquiera marco) solo se aceptan No contesto / Numero
# equivocado. Se aplica a las campanas Preview 1-5. El formulario tambien filtra las opciones
# (vista call_outcome), pero esta es la barrera real (server-side).
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.utils import timezone as _tz
from reportes_app.models import LlamadaLog

GATE_ACTIVO = False   # decisión de producto: disposiciones libres (sin exigir llamada contestada)
GATE_CAMPANAS = {1, 2, 3, 4, 5}
DISPOS_SIN_CONTESTAR = {"No contesto", "Numero equivocado"}
GATE_VENTANA = timedelta(hours=3)
GATE_MSG = ("Esta llamada NO fue contestada: solo puedes guardar 'No contesto' o 'Numero equivocado'. "
            "Si el lead si contesto, revisa que la llamada haya conectado y vuelve a intentar.")


def llamada_contestada(agente_id, contacto_id, callid=None, desde=None):
    """True si hubo ANSWER de ese agente a ese contacto en la ventana (o para ese callid)."""
    try:
        if callid:
            if LlamadaLog.objects.filter(callid=callid, event="ANSWER").exists():
                return True
        desde = desde or (_tz.now() - GATE_VENTANA)
        return LlamadaLog.objects.filter(
            agente_id=agente_id, contacto_id=contacto_id, event="ANSWER", time__gte=desde).exists()
    except Exception as e:
        logger.error("DIALER gate llamada_contestada: %s", e)
        return True   # ante error del log, no bloquear al agente


def resumen_llamadas(agente_id, contacto_id, desde=None):
    """Para el formulario: {answered, attempts, last_event, last_duration}."""
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
    """Lanza ValidationError si es una disposicion de conversacion sin llamada contestada.
    La llama CalificacionCliente.save() ANTES de que OML finalice el AEC (si no, un rechazo
    igual sacaba el lead de la cola), y tambien la senal pre_save como segunda barrera.
    DESACTIVADO con GATE_ACTIVO=False (decisión de producto)."""
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
                return   # no cambia la disposicion (ej. observaciones): no re-validar
        except CalificacionCliente.DoesNotExist:
            pass
    if not llamada_contestada(instance.agente_id, instance.contacto_id, instance.callid):
        logger.warning("DIALER gate: BLOQUEADA dispo '%s' agente=%s contacto=%s (sin ANSWER)",
                       nombre, instance.agente_id, instance.contacto_id)
        raise ValidationError(GATE_MSG)


@receiver(pre_save, sender=CalificacionCliente, dispatch_uid="gate_dispo")
def on_calificacion_pre_save(sender, instance, **kwargs):
    validar_gate(instance)


# ── LEAD EN PANTALLA = NO RECIBE ENTRANTES (decisión decisión de producto) ────────
# Mientras el vendedor tiene un lead entregado/asignado sin disposicionar, no le
# timbran las llamadas de las colas. Se pausa SOLO en Asterisk (QueuePause): no se
# toca el estado de OML ni se libera el lead (pause_agent de OML sí lo liberaría).
# Aplica igual en la consola web y en la app del celular.
# Seguro: /root/sync_agent_pause.sh despausa cada minuto a quien ya no tenga lead.
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
    """Pausa/despausa al agente en sus colas de Asterisk por tener un lead en pantalla."""
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
                    'pausado (lead en pantalla)' if pausar else 'disponible')
    except Exception as e:
        logger.error("DIALER pausa gestion agente=%s pausar=%s: %s", agente_id, pausar, e)


def _tiene_lead_en_pantalla(agente_id):
    from ominicontacto_app.models import AgenteEnContacto
    return AgenteEnContacto.objects.filter(
        agente_id=agente_id,
        estado__in=[AgenteEnContacto.ESTADO_ENTREGADO, AgenteEnContacto.ESTADO_ASIGNADO],
    ).exists()


def revisar_pausas_gestion():
    """Seguro anti-agente-colgado: despausa a todo el que esté marcado y ya no
    tenga lead en pantalla. Lo corre /root/sync_agent_pause.sh cada minuto."""
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
