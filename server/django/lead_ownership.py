# -*- coding: utf-8 -*-
"""
STICKY DE POR VIDA — Dialer (regla decisión de producto).

El PRIMER agente con el que el lead CONVERSÓ (disposición de contacto real) queda
dueño del lead para siempre: toda inyección posterior (Seguimiento, Nuevo Lead,
WA Respondió, Callback, WA Cita Pendiente), en CUALQUIER campaña, se entrega solo
a él, aunque después vuelva a no contestar. El owner de GHL tampoco se quita.

NO se pierde ni si el agente se inactiva ni si se borra del dialer (decisión el operador). Tabla: dialer_lead_owner(contacto_id PK, agente_id, since, motivo).
"""
import logging
from django.db import connection

logger = logging.getLogger(__name__)

# Disposiciones que prueban que hubo conversación con el lead
DISPOS_CONVERSACION = {
    "Agendo cita", "Va a agendar", "Llamada de vuelta programada", "Prefiere WhatsApp",
    "Solo queria precio", "Colgo", "Error mio de ventas", "No interesado", "Ya compro",
}

# DUEÑO PERMANENTE (decisión de producto): el dueño NO se pierde ni si el vendedor se inactiva
# ni si se borra del dialer. Sus leads quedan en cola a su nombre.
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
    """Fija el dueño si aún no tiene (el primero gana, para siempre)."""
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
            logger.info("sticky: contacto=%s dueño=%s (%s)", contacto_id, agente_id, motivo)
        # inmediato: lo que este en cola (pool) de este lead pasa al dueño YA (sin esperar el cron)
        sync_contacto(contacto_id)
        return inserted
    except Exception as e:
        logger.error("sticky set_owner: %s", e)
        return False


def sync_contacto(contacto_id):
    """Los AEC en pool (INICIAL, agente -1) de ESTE lead pasan al dueño activo, en todas las campañas."""
    try:
        with connection.cursor() as cur:
            cur.execute(_SQL_SYNC_POOL + " AND aec.contacto_id = %s", [int(contacto_id)])
            return cur.rowcount
    except Exception as e:
        logger.error("sticky sync_contacto: %s", e)
        return -1


def get_owner(contacto_id):
    """agente_id dueño del lead (permanente), o -1."""
    try:
        with connection.cursor() as cur:
            cur.execute(_SQL_OWNER_ACTIVO, [int(contacto_id)])
            row = cur.fetchone()
        return int(row[0]) if row else -1
    except Exception as e:
        logger.error("sticky get_owner: %s", e)
        return -1


def sync_pool():
    """Seguro: cualquier AEC en pool (INICIAL, agente -1) de un lead con dueño
    activo vuelve al dueño. Cubre liberaciones de OML que no pasan por señales."""
    try:
        with connection.cursor() as cur:
            cur.execute(_SQL_SYNC_POOL)
            return cur.rowcount
    except Exception as e:
        logger.error("sticky sync_pool: %s", e)
        return -1


def agente_inactivo(agente_id):
    """True si el agente esta inactivo/borrado (o no existe). -1 no cuenta como inactivo."""
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
    """Leads en pool asignados a un agente inactivo/borrado vuelven al pool general."""
    try:
        with connection.cursor() as cur:
            cur.execute(_SQL_LIBERAR_INACTIVOS)
            return cur.rowcount
    except Exception as e:
        logger.error("sticky liberar_de_inactivos: %s", e)
        return -1


ensure_table()
