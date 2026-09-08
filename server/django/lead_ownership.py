# -*- coding: utf-8 -*-
"""
LIFETIME STICKY — Dialer (product decision rule).

The FIRST agent the lead TALKED with (a real contact disposition) becomes the
owner of the lead forever: every subsequent injection (Seguimiento, Nuevo Lead,
WA Respondió, Callback, WA Cita Pendiente), in ANY campaign, is delivered only
to them, even if they later fail to answer again. The GHL owner isn't removed either.

It is NOT lost even if the agent is deactivated or deleted from the dialer (operator's decision). Table: dialer_lead_owner(contacto_id PK, agente_id, since, motivo).
"""
import logging
from django.db import connection

logger = logging.getLogger(__name__)

# Dispositions that prove there was a conversation with the lead
DISPOS_CONVERSACION = {
    "Agendo cita", "Va a agendar", "Llamada de vuelta programada", "Prefiere WhatsApp",
    "Solo queria precio", "Colgo", "Error mio de ventas", "No interesado", "Ya compro",
}

# PERMANENT OWNER (product decision): the owner is NOT lost even if the salesperson is deactivated
# or deleted from the dialer. Their leads stay queued under their name.
_SQL_OWNER_ACTIVO = (
    "SELECT o.agente_id FROM dialer_lead_owner o WHERE o.contacto_id = %s"
)

_SQL_SYNC_POOL = (
    "UPDATE ominicontacto_app_agenteencontacto aec SET agente_id = o.agente_id "
    "FROM dialer_lead_owner o "
    "WHERE aec.contacto_id = o.contacto_id AND aec.estado = 0 AND aec.agente_id = -1"
)


def ensure_table():
    try:
        with connection.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS dialer_lead_owner ("
                " contacto_id integer PRIMARY KEY,"
                " agente_id integer NOT NULL,"
                " since timestamptz NOT NULL DEFAULT now(),"
                " motivo text)")
    except Exception as e:
        logger.error("sticky ensure_table: %s", e)


def set_owner(contacto_id, agente_id, motivo=""):
    """Sets the owner if it doesn't have one yet (first one wins, forever)."""
    if not contacto_id or not agente_id or int(agente_id) <= 0:
        return False
    try:
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO dialer_lead_owner (contacto_id, agente_id, motivo) "
                "VALUES (%s, %s, %s) ON CONFLICT (contacto_id) DO NOTHING",
                [int(contacto_id), int(agente_id), motivo])
            inserted = cur.rowcount == 1
        if inserted:
            logger.info("sticky: contacto=%s owner=%s (%s)", contacto_id, agente_id, motivo)
        # immediate: whatever is in this lead's pool queue moves to the owner RIGHT NOW (without waiting for the cron)
        sync_contacto(contacto_id)
        return inserted
    except Exception as e:
        logger.error("sticky set_owner: %s", e)
        return False


def sync_contacto(contacto_id):
    """AEC rows in the pool (INICIAL, agente -1) for THIS lead move to the active owner, across all campaigns."""
    try:
        with connection.cursor() as cur:
            cur.execute(_SQL_SYNC_POOL + " AND aec.contacto_id = %s", [int(contacto_id)])
            return cur.rowcount
    except Exception as e:
        logger.error("sticky sync_contacto: %s", e)
        return -1


def get_owner(contacto_id):
    """agente_id of the lead's (permanent) owner, or -1."""
    try:
        with connection.cursor() as cur:
            cur.execute(_SQL_OWNER_ACTIVO, [int(contacto_id)])
            row = cur.fetchone()
        return int(row[0]) if row else -1
    except Exception as e:
        logger.error("sticky get_owner: %s", e)
        return -1


def sync_pool():
    """Safety net: any AEC in the pool (INICIAL, agente -1) of a lead with an active
    owner goes back to the owner. Covers OML releases that don't go through signals."""
    try:
        with connection.cursor() as cur:
            cur.execute(_SQL_SYNC_POOL)
            return cur.rowcount
    except Exception as e:
        logger.error("sticky sync_pool: %s", e)
        return -1


def agente_inactivo(agente_id):
    """True if the agent is inactive/deleted (or doesn't exist). -1 doesn't count as inactive."""
    try:
        if agente_id is None or int(agente_id) <= 0:
            return False
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM ominicontacto_app_agenteprofile "
                        "WHERE id=%s AND is_inactive=false AND borrado=false", [int(agente_id)])
            return cur.fetchone() is None
    except Exception as e:
        logger.error("sticky agente_inactivo: %s", e)
        return False


_SQL_LIBERAR_INACTIVOS = (
    "UPDATE ominicontacto_app_agenteencontacto aec SET agente_id = -1 "
    "WHERE aec.estado = 0 AND aec.agente_id > 0 AND NOT EXISTS ("
    " SELECT 1 FROM ominicontacto_app_agenteprofile a WHERE a.id = aec.agente_id "
    " AND a.is_inactive = false AND a.borrado = false)"
)


def liberar_de_inactivos():
    """Pool leads assigned to an inactive/deleted agent go back to the general pool."""
    try:
        with connection.cursor() as cur:
            cur.execute(_SQL_LIBERAR_INACTIVOS)
            return cur.rowcount
    except Exception as e:
        logger.error("sticky liberar_de_inactivos: %s", e)
        return -1


ensure_table()
