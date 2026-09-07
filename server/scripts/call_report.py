#!/usr/bin/env python3
"""
Reporte de llamadas Dialer (decisión de producto). Uso: python3 /root/call_report.py [--dias 7] [--agente agente1]
- Salientes: marcadas, contestadas (humano o buzon), % contestacion, segundos promedio hasta contestar, duracion promedio.
- Buzones (auditoria Whisper): cuantas, duracion promedio, idioma.
- Humanos: cuantas, duracion promedio.
- ALERTAS: disposicion de conversacion sobre un buzon (Whisper) y conversaciones "contestadas" de menos de 15 s.
"""
import os
import argparse, psycopg2
from datetime import datetime, timedelta, timezone

DB = dict(host=os.environ.get('DB_HOST', '127.0.0.1'),
          port=int(os.environ.get('DB_PORT', 5432)),
          dbname=os.environ.get('DB_NAME', 'omnileads'),
          user=os.environ.get('DB_USER', 'omnileads'),
          password=os.environ.get('DB_PASSWORD', 'CAMBIAR_CLAVE_POSTGRES'))
CONV = ("Agendo cita", "Va a agendar", "Llamada de vuelta programada", "Prefiere WhatsApp",
        "Solo queria precio", "Colgo", "Error mio de ventas", "No interesado", "Ya compro")

ap = argparse.ArgumentParser()
ap.add_argument("--dias", type=int, default=7)
ap.add_argument("--agente", default=None)
a = ap.parse_args()
desde = datetime.now(timezone.utc) - timedelta(days=a.dias)
conn = psycopg2.connect(**DB); cur = conn.cursor()

def q(sql, params=()):
    cur.execute(sql, params); return cur.fetchall()

filtro_ag = ""
params = [desde]
if a.agente:
    filtro_ag = " AND l.agente_id = (SELECT ap.id FROM ominicontacto_app_agenteprofile ap JOIN ominicontacto_app_user u ON u.id=ap.user_id WHERE u.username=%s)"
    params.append(a.agente)

print("=" * 78)
print("REPORTE LLAMADAS DIALER DIALER — ultimos %d dias%s" % (a.dias, (" — agente " + a.agente) if a.agente else ""))
print("=" * 78)

# ── Salientes (preview 4, manual 1, click2call 6) ──
(dials, answered, avg_ring, avg_dur) = q("""
    SELECT COUNT(*) FILTER (WHERE event='DIAL'),
           COUNT(*) FILTER (WHERE event='ANSWER'),
           ROUND(AVG(bridge_wait_time) FILTER (WHERE event='ANSWER' AND bridge_wait_time>=0),1),
           ROUND(AVG(duracion_llamada) FILTER (WHERE event IN ('COMPLETEAGENT','COMPLETEOUTNUM') AND duracion_llamada>0),1)
    FROM reportes_app_llamadalog l
    WHERE l.time >= %s AND l.tipo_llamada IN (1,4,6)""" + filtro_ag, params)[0]
print("\nSALIENTES")
print("  marcadas: %s | contestadas (humano o buzon): %s | tasa: %s%%" % (dials, answered, round(100.0 * answered / dials, 1) if dials else 0))
print("  segundos promedio hasta que contestan: %s | duracion promedio de las contestadas: %s s" % (avg_ring, avg_dur))

# ── Timbre de las NO contestadas (cuanto esperan antes de rendirse) ──
r = q("""SELECT ROUND(AVG(bridge_wait_time),1), COUNT(*) FROM reportes_app_llamadalog l
         WHERE l.time >= %s AND l.tipo_llamada IN (1,4,6) AND event IN ('NOANSWER','CANCEL','BUSY','RINGNOANSWER') AND bridge_wait_time>=0""" + filtro_ag, params)[0]
print("  no contestadas: %s | segundos promedio de timbre antes de colgar: %s" % (r[1], r[0]))

# ── Auditoria Whisper (buzon vs humano) ──
try:
    filtro_aud = " AND agente ILIKE %s" if a.agente else ""
    p2 = [desde] + ([a.agente + "%"] if a.agente else [])
    (n_buz, d_buz, n_hum, d_hum) = q("""
        SELECT COUNT(*) FILTER (WHERE es_buzon), ROUND(AVG(duracion) FILTER (WHERE es_buzon),1),
               COUNT(*) FILTER (WHERE NOT es_buzon), ROUND(AVG(duracion) FILTER (WHERE NOT es_buzon),1)
        FROM dialer_call_audit WHERE fecha >= %s""" + filtro_aud, p2)[0]
    print("\nAUDITORIA WHISPER (llamadas contestadas con disposicion de conversacion y >30 s)")
    print("  buzones detectados: %s (duracion promedio %s s) | humanos: %s (duracion promedio %s s)" % (n_buz, d_buz, n_hum, d_hum))
    idi = q("SELECT COALESCE(NULLIF(idioma,''),'?'), COUNT(*) FROM dialer_call_audit WHERE fecha >= %s AND es_buzon GROUP BY 1 ORDER BY 2 DESC", [desde])
    if idi: print("  idioma de los buzones: " + ", ".join("%s=%s" % x for x in idi))
    print("\nALERTAS — disposicion de conversacion sobre un BUZON (por vendedor):")
    rows = q("""SELECT agente, disposicion, COUNT(*) FROM dialer_call_audit WHERE fecha >= %s AND alerta""" + filtro_aud + " GROUP BY 1,2 ORDER BY 3 DESC", p2)
    if not rows: print("  ninguna")
    for ag, d, n in rows: print("  %-22s %-30s %s" % (ag, d, n))
except psycopg2.Error as e:
    conn.rollback(); print("\n(auditoria Whisper aun sin datos: %s)" % str(e).splitlines()[0])

# ── Conversaciones sospechosamente cortas (contestada < 15 s con disposicion de conversacion) ──
print("\nALERTAS — 'conversacion' con llamada contestada de menos de 15 s (por vendedor):")
rows = q("""
    SELECT u.username, oc.nombre, COUNT(*)
    FROM ominicontacto_app_calificacioncliente cc
    JOIN ominicontacto_app_opcioncalificacion oc ON oc.id=cc.opcion_calificacion_id
    JOIN ominicontacto_app_agenteprofile ap ON ap.id=cc.agente_id JOIN ominicontacto_app_user u ON u.id=ap.user_id
    JOIN LATERAL (SELECT MAX(duracion_llamada) dur FROM reportes_app_llamadalog l
                  WHERE l.agente_id=cc.agente_id AND l.contacto_id=cc.contacto_id AND l.event IN ('COMPLETEAGENT','COMPLETEOUTNUM')
                    AND l.time BETWEEN cc.modified - interval '3 hours' AND cc.modified + interval '5 minutes') d ON true
    WHERE cc.modified >= %s AND oc.campana_id IN (1,2,3,4,5) AND oc.nombre IN %s AND d.dur IS NOT NULL AND d.dur < 15
    """ + (" AND u.username=%s" if a.agente else "") + " GROUP BY 1,2 ORDER BY 3 DESC",
    [desde, CONV] + ([a.agente] if a.agente else []))
if not rows: print("  ninguna")
for ag, d, n in rows: print("  %-22s %-30s %s" % (ag, d, n))

# ── Por vendedor: marcadas / contestadas / tasa ──
print("\nPOR VENDEDOR (salientes)")
rows = q("""SELECT u.username, COUNT(*) FILTER (WHERE event='DIAL'), COUNT(*) FILTER (WHERE event='ANSWER'),
                   ROUND(AVG(bridge_wait_time) FILTER (WHERE event='ANSWER' AND bridge_wait_time>=0),1)
            FROM reportes_app_llamadalog l JOIN ominicontacto_app_agenteprofile ap ON ap.id=l.agente_id
            JOIN ominicontacto_app_user u ON u.id=ap.user_id
            WHERE l.time >= %s AND l.tipo_llamada IN (1,4,6) GROUP BY 1 ORDER BY 2 DESC""", [desde])
print("  %-14s %8s %11s %6s %14s" % ("vendedor", "marcadas", "contestadas", "tasa", "seg. contestar"))
for u, d, an, ring in rows:
    print("  %-14s %8s %11s %5s%% %14s" % (u, d, an, round(100.0 * an / d, 1) if d else 0, ring))
conn.close()
