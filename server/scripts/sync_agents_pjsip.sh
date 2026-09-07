#!/bin/bash
# Red de seguridad de la sección USUARIOS del dashboard.
# Asegura que cada AGENTE activo (fuente de verdad = base de datos) tenga su endpoint
# PJSIP en Asterisk. SOLO AGREGA los que falten (append-only): nunca reescribe ni borra,
# así que no puede romper la config existente (supervisores, agentes previos).
# Hace que los agentes creados desde la sección USUARIOS persistan ante reinicios.
CONF=/etc/asterisk/retrieve_conf/oml_pjsip_agents.conf
ADDED=0
LINES=$(docker exec -w /opt/omnileads/ominicontacto prod-env-django-app-1 python3 -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ominicontacto.settings.production')
django.setup()
from ominicontacto_app.models import AgenteProfile
for p in AgenteProfile.objects.filter(borrado=False, is_inactive=False, user__borrado=False):
    try:
        cid = p.get_asterisk_caller_id()
    except Exception:
        cid = str(p.sip_extension)
    print(str(p.sip_extension) + '|' + cid)
" 2>/dev/null)
while IFS='|' read -r SIP CID; do
  [ -z "$SIP" ] && continue
  if ! docker exec prod-env-acd-1 grep -q "^\[$SIP\](agents)" "$CONF" 2>/dev/null; then
    docker exec prod-env-acd-1 sh -c "printf '%b' '[$SIP](agents)\nendpoint/callerid=$CID <$SIP>\ninbound_auth/username=$SIP\ninbound_auth/password=\nendpoint/context=from-agent\n' >> $CONF"
    ADDED=$((ADDED + 1))
  fi
done <<< "$LINES"
[ "$ADDED" -gt 0 ] && docker exec prod-env-acd-1 asterisk -rx "pjsip reload" >/dev/null 2>&1
echo "sync_agents_pjsip: $ADDED agente(s) agregado(s) desde DB"
