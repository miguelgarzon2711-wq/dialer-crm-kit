#!/usr/bin/env python3
"""
Safety net for "lead on screen = no incoming calls" (el cliente / el negocio).

Every minute it compares Asterisk's actual state with the dialer's and corrects it:
  - has a lead on screen and is NOT paused  -> pauses them
  - has no lead and was left paused by us -> unpauses them
  - has no lead, is not on a call and has been paused for >3 min -> unpauses them anyway
    (safety net in case the Redis flag was lost, e.g. after a restart)

Self-heals in both directions: it works whether Django restarts or
Asterisk or Redis restarts. It doesn't depend on any previous command having arrived.
"""
import re
import subprocess
import sys
from datetime import datetime

DESPAUSE_SEGUNDOS = 180  # tolerance before unpausing without a flag (respects OMniLeads' ACW)
APP_DORMIDA_SEGUNDOS = 180  # the app stops sending heartbeats for 3 min (closed or asleep) -> out of the queues


def sh(cmd, timeout=25):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ''


def pausados_en_asterisk():
    """{agente_id: seconds_paused} reading the queues' real state."""
    salida = sh(['docker', 'exec', 'prod-env-acd-1', 'asterisk', '-rx', 'queue show'])
    salida = re.sub(r'\x1b\[[0-9;]*m', '', salida)
    encontrados = {}
    for linea in salida.splitlines():
        m = re.search(r'^\s+(\d+)_\S*.*?\(paused was (\d+) (secs?|mins?|hours?) ago\)', linea)
        if not m:
            continue
        agente_id, valor, unidad = int(m.group(1)), int(m.group(2)), m.group(3)
        segundos = valor * (1 if unidad.startswith('sec') else 60 if unidad.startswith('min') else 3600)
        encontrados[agente_id] = min(encontrados.get(agente_id, 10 ** 9), segundos)
    return encontrados


def consulta_sql(sql):
    return sh(['docker', 'exec', 'prod-env-postgresql-1', 'psql', '-U', 'omnileads',
               '-d', 'omnileads', '-tAc', sql])


def agentes_con_lead():
    out = consulta_sql(
        "SELECT DISTINCT agente_id FROM ominicontacto_app_agenteencontacto "
        "WHERE estado IN (1,3) AND agente_id > 0;")
    return {int(x) for x in out.split() if x.strip().isdigit()}


def agentes_en_llamada():
    """Don't touch whoever is on a call (their ACW is legitimate).
    A single pass over Redis: the cost does NOT grow with the number of salespeople."""
    lua = ("local r={} for _,k in ipairs(redis.call('KEYS','OML:AGENT:*')) do "
           "local v=redis.call('HGET',k,'CONTACT_NUMBER') "
           "if v and v~='' then r[#r+1]=k end end return r")
    out = sh(['docker', 'exec', 'prod-env-redis-1', 'redis-cli', '--no-raw', 'EVAL', lua, '0'])
    ocupados = set()
    for m in re.finditer(r'OML:AGENT:(\d+)', out):
        ocupados.add(int(m.group(1)))
    return ocupados


def marcados_por_nosotros():
    out = sh(['docker', 'exec', 'prod-env-redis-1', 'redis-cli', 'SMEMBERS', 'OML:DIALER:PAUSA_GESTION'])
    return {int(x) for x in out.split() if x.strip().isdigit()}


def miembros_en_asterisk():
    """Agents who are currently in some queue (logged in on Asterisk)."""
    salida = sh(['docker', 'exec', 'prod-env-acd-1', 'asterisk', '-rx', 'queue show'])
    salida = re.sub(r'\x1b\[[0-9;]*m', '', salida)
    return {int(m.group(1)) for m in re.finditer(r'^\s+(\d+)_\S', salida, re.M)}


def apps_dormidas(ahora, miembros, en_llamada):
    """Phones that stopped sending heartbeats: (agente_id, seconds_without_heartbeat)."""
    out = sh(['docker', 'exec', 'prod-env-redis-1', 'redis-cli', '--no-raw', 'KEYS', 'OML:DIALER:SESION:*'])
    dormidas = []
    for m in re.finditer(r'OML:DIALER:SESION:(\d+)', out):
        agente = int(m.group(1))
        h = sh(['docker', 'exec', 'prod-env-redis-1', 'redis-cli', 'HGETALL', 'OML:DIALER:SESION:%d' % agente])
        campos = h.split()
        d = dict(zip(campos[0::2], campos[1::2]))
        if d.get('dispositivo') != 'app' or not d.get('ultimo_visto', '').isdigit():
            continue
        sin_latir = ahora - int(d['ultimo_visto'])
        if sin_latir >= APP_DORMIDA_SEGUNDOS and agente in miembros and agente not in en_llamada:
            dormidas.append((agente, sin_latir))
    return dormidas


def dormir_apps(dormidas):
    """Takes the agent out of the queues (same logout as the console) and marks them 'app_dormida':
    when the app starts sending heartbeats again, it reconnects on its own."""
    if not dormidas:
        return
    ids = ','.join(str(a) for a, _ in dormidas)
    codigo = (
        "from ominicontacto_app.models import AgenteProfile;"
        "from ominicontacto_app.services.asterisk.agent_activity import AgentActivityAmiManager;"
        "from api_app.views.agent_api import _redis_para_agente, _REDIS_SESION;"
        "r=_redis_para_agente();m=AgentActivityAmiManager();"
        "[(m.logout_agent(a, manage_connection=True), r.hset(_REDIS_SESION.replace('%s', str(a.id)), 'dispositivo', 'app_dormida'))"
        " for a in AgenteProfile.objects.filter(id__in=[" + ids + "])]")
    sh(['docker', 'exec', 'prod-env-django-app-1', 'python3',
        '/opt/omnileads/ominicontacto/manage.py', 'shell', '-c', codigo], timeout=90)


def aplicar(cambios):
    """cambios: list of (agente_id, pausar_bool)."""
    if not cambios:
        return
    lineas = ';'.join("d.pausa_gestion(%d, %s)" % (a, 'True' if p else 'False') for a, p in cambios)
    sh(['docker', 'exec', 'prod-env-django-app-1', 'python3',
        '/opt/omnileads/ominicontacto/manage.py', 'shell', '-c',
        "from api_app.views import crm_dispositions as d;" + lineas], timeout=60)


def main():
    pausados = pausados_en_asterisk()
    con_lead = agentes_con_lead()
    marcados = marcados_por_nosotros()
    en_llamada = agentes_en_llamada()

    cambios, motivos = [], []

    # 1) has a lead and is not paused -> pause (covers Asterisk restarts)
    for agente in sorted(con_lead - set(pausados)):
        cambios.append((agente, True))
        motivos.append("agente %s: has a lead on screen, pausing" % agente)

    # 2) has no lead and was left paused -> unpause
    for agente, segundos in sorted(pausados.items()):
        if agente in con_lead or agente in en_llamada:
            continue
        if agente in marcados or segundos > DESPAUSE_SEGUNDOS:
            cambios.append((agente, False))
            motivos.append("agente %s: no lead and paused for %ss, releasing" % (agente, segundos))

    # 3) the mobile app stopped sending heartbeats (closed or suspended) -> out of the queues
    import time as _t
    # (an agent with a lead on screen is left alone: auto_release_leads handles that at 10 min)
    dormidas = apps_dormidas(int(_t.time()), miembros_en_asterisk(), en_llamada | con_lead)
    for agente, seg in dormidas:
        motivos.append("agente %s: the app hasn't sent a heartbeat for %ss, leaving the queues (reconnects on return)" % (agente, seg))

    if cambios:
        aplicar(cambios)
    if dormidas:
        dormir_apps(dormidas)
    if motivos:
        ahora = datetime.now().strftime('%F %T')
        for m in motivos:
            print("%s %s" % (ahora, m))
    return 0


if __name__ == '__main__':
    sys.exit(main())
