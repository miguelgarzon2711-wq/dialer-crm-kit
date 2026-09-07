#!/usr/bin/env python3
"""
Seguro del "lead en pantalla = no entran llamadas" (el cliente / el negocio).

Cada minuto compara la realidad de Asterisk con la del dialer y corrige:
  - tiene lead en pantalla y NO está pausado  -> lo pausa
  - no tiene lead y quedó pausado por nosotros -> lo despausa
  - no tiene lead, no está en llamada y lleva >3 min pausado -> lo despausa igual
    (red de seguridad por si se perdió la marca de Redis, p.ej. tras un reinicio)

Se auto-repara en los dos sentidos: sirve tanto si se reinicia Django como si se
reinicia Asterisk o Redis. No depende de que ninguna orden previa haya llegado.
"""
import re
import subprocess
import sys
from datetime import datetime

DESPAUSE_SEGUNDOS = 180  # tolerancia antes de despausar sin marca (respeta el ACW de OMniLeads)
APP_DORMIDA_SEGUNDOS = 180  # la app deja de latir 3 min (cerrada o dormida) -> fuera de las colas


def sh(cmd, timeout=25):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ''


def pausados_en_asterisk():
    """{agente_id: segundos_pausado} leyendo el estado real de las colas."""
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
    """No tocar a quien está hablando (su ACW es legítimo).
    Una sola pasada por Redis: el costo NO crece con la cantidad de vendedores."""
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
    """Agentes que hoy están metidos en alguna cola (logueados en Asterisk)."""
    salida = sh(['docker', 'exec', 'prod-env-acd-1', 'asterisk', '-rx', 'queue show'])
    salida = re.sub(r'\x1b\[[0-9;]*m', '', salida)
    return {int(m.group(1)) for m in re.finditer(r'^\s+(\d+)_\S', salida, re.M)}


def apps_dormidas(ahora, miembros, en_llamada):
    """Celulares que dejaron de latir: (agente_id, segundos_sin_latir)."""
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
    """Saca de las colas al agente (mismo logout de la consola) y lo marca 'app_dormida':
    cuando la app vuelva a latir, se reengancha sola."""
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
    """cambios: lista de (agente_id, pausar_bool)."""
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

    # 1) tiene lead y no está pausado -> pausar (cubre reinicios de Asterisk)
    for agente in sorted(con_lead - set(pausados)):
        cambios.append((agente, True))
        motivos.append("agente %s: tiene lead en pantalla, se pausa" % agente)

    # 2) no tiene lead y quedó pausado -> despausar
    for agente, segundos in sorted(pausados.items()):
        if agente in con_lead or agente in en_llamada:
            continue
        if agente in marcados or segundos > DESPAUSE_SEGUNDOS:
            cambios.append((agente, False))
            motivos.append("agente %s: sin lead y pausado hace %ss, se libera" % (agente, segundos))

    # 3) la app del celular dejó de latir (cerrada o dormida) -> fuera de las colas
    import time as _t
    # (con lead en pantalla no se toca: eso lo resuelve auto_release_leads a los 10 min)
    dormidas = apps_dormidas(int(_t.time()), miembros_en_asterisk(), en_llamada | con_lead)
    for agente, seg in dormidas:
        motivos.append("agente %s: la app no late hace %ss, sale de las colas (se reengancha al volver)" % (agente, seg))

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
