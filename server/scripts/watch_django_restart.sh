#!/bin/bash
# Daemon: detects a restart of prod-env-django-app-1 and logs agents back in automatically.
# Supervised by check_dashboard.sh. Log: /var/log/django_restart_watcher.log

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> /var/log/django_restart_watcher.log; }

log "Watcher iniciado (PID 13402)"

docker events \
  --filter 'container=prod-env-django-app-1' \
  --filter 'event=start' \
  --format '{{.Time}}' | \
while read -r ts; do
    log "Django reinició (ts=$ts) — esperando 45s para que inicialice"
    sleep 45

    log "Re-logueando agentes en colas de Asterisk"
    docker exec prod-env-django-app-1 python3 -c "
# AGENTES: list of ids. To pull them from the database automatically:
#   from ominicontacto_app.models import AgenteProfile
#   AGENTES = list(AgenteProfile.objects.filter(borrado=False).values_list('id', flat=True))
AGENTES = []   # <-- fill in with your agent ids
import django, os
os.environ['DJANGO_SETTINGS_MODULE'] = 'ominicontacto.settings.production'
django.setup()
from ominicontacto_app.models import AgenteProfile
from ominicontacto_app.services.asterisk.agent_activity import AgentActivityAmiManager
mgr = AgentActivityAmiManager()
# AgenteProfile ids - add new agents here
# Ids of the agents to log back in after an application restart.
# Put the ones from your installation, or use the query above to take them all.
for agent_id in AGENTES:
    try:
        mgr.login_agent(AgenteProfile.objects.get(id=agent_id), manage_connection=True)
        print(f'Agente {agent_id} OK')
    except Exception as e:
        print(f'Agente {agent_id} ERROR: {e}')
" >> /var/log/django_restart_watcher.log 2>&1 && log "Agentes re-logueados OK" || log "ERROR al re-loguear agentes"
done

log "docker events terminó — el watcher debería ser reiniciado por check_dashboard.sh"
