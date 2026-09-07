# -*- coding: utf-8 -*-
"""
INBOUND STICKY POR VENDEDOR (decisión de producto). Crea, por cada agente activo, una campaña
entrante "Inbound A<id> <user>" con ese único agente, clonando la campaña 7 (Inbound G1):
Campana + opciones + parametrosCrm + supervisores + queue_table (wait 20s) + queue_member +
DestinoEntrante + RutaEntrante con DID virtual 900001<id 2 dígitos>. Idempotente.
Imprime JSON con [{agente_id, username, campana_id, queue, did, ruta_id}].
"""
import json
from django.db import connection
from ominicontacto_app.models import (Campana, AgenteProfile, OpcionCalificacion, ParametrosCrm)
from configuracion_telefonia_app.models import RutaEntrante, DestinoEntrante

BASE = Campana.objects.get(id=7)
WAIT = 20
out = []
for ag in AgenteProfile.objects.filter(borrado=False).select_related("user").order_by("id"):
    user = ag.user.username
    nombre = "Inbound A%d %s" % (ag.id, user)
    did = "900001%02d" % ag.id
    c = Campana.objects.filter(nombre=nombre).first()
    if c is None:
        c = Campana.objects.get(id=7)
        c.pk = None; c.id = None
        c.nombre = nombre
        c.save()
        # opciones de calificación (mismas que la 7, así el motor GHL y el sticky las reconocen)
        for o in OpcionCalificacion.objects.filter(campana_id=7):
            o.pk = None; o.id = None; o.campana = c; o.save()
        for p in ParametrosCrm.objects.filter(campana_id=7):
            p.pk = None; p.id = None; p.campana = c; p.save()
    qname = "%d_%s" % (c.id, nombre)
    with connection.cursor() as cur:
        # supervisores = los mismos de la 7 (columna detectada dinamicamente), idempotente
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='ominicontacto_app_campana_supervisors'")
        scol = [r[0] for r in cur.fetchall() if r[0] not in ("id", "campana_id")][0]
        cur.execute("INSERT INTO ominicontacto_app_campana_supervisors (campana_id, %s) "
                    "SELECT %%s, s.%s FROM ominicontacto_app_campana_supervisors s WHERE s.campana_id=7 "
                    "AND NOT EXISTS (SELECT 1 FROM ominicontacto_app_campana_supervisors x WHERE x.campana_id=%%s AND x.%s=s.%s)"
                    % (scol, scol, scol, scol), [c.id, c.id])
        cur.execute("SELECT 1 FROM queue_table WHERE campana_id=%s", [c.id])
        if not cur.fetchone():
            cols = [r[0] for r in connection.introspection.get_table_description(cur, "queue_table")]
            cols = [x for x in cols if x not in ("name", "campana_id", "wait")]
            cur.execute("INSERT INTO queue_table (name, campana_id, wait, %s) SELECT %%s, %%s, %%s, %s FROM queue_table WHERE campana_id=7"
                        % (", ".join(cols), ", ".join(cols)), [nombre, c.id, WAIT])
        cur.execute("SELECT 1 FROM queue_member_table WHERE id_campana=%s AND member_id=%s", [qname, ag.id])
        if not cur.fetchone():
            cur.execute("SELECT membername, interface, penalty FROM queue_member_table WHERE member_id=%s LIMIT 1", [ag.id])
            row = cur.fetchone()
            if row is None:
                row = ("%s" % (ag.user.get_full_name() or user), "Local/%s@from-queue/n" % ag.sip_extension, 0)
            cur.execute("INSERT INTO queue_member_table (membername, interface, penalty, paused, id_campana, member_id, queue_name) "
                        "VALUES (%s, %s, %s, 0, %s, %s, %s)", [row[0], row[1], row[2], qname, ag.id, nombre])
    dst = DestinoEntrante.objects.filter(tipo=1, object_id=c.id).first()
    if dst is None:
        base_dst = DestinoEntrante.objects.get(id=3)
        dst = DestinoEntrante.objects.create(nombre=nombre, tipo=1, object_id=c.id, content_type_id=base_dst.content_type_id)
    ruta = RutaEntrante.objects.filter(telefono=did).first()
    if ruta is None:
        ruta = RutaEntrante.objects.create(nombre=nombre, telefono=did, prefijo_caller_id="", destino=dst, idioma_id=2)
    out.append({"agente_id": ag.id, "username": user, "campana_id": c.id, "queue": qname, "did": did, "ruta_id": ruta.id})

# Redis: familias de campañas/agentes (OML:CAMP, OML:CAMPAIGN-AGENTS, ...)
try:
    from ominicontacto_app.services.asterisk.redis_database import RegenerarAsteriskFamilysOML
    RegenerarAsteriskFamilysOML().regenerar_asterisk()
    regen = "ok"
except Exception as e:
    regen = "ERROR %s" % e
print("REGEN:", regen)
print("RESULT:" + json.dumps(out))
