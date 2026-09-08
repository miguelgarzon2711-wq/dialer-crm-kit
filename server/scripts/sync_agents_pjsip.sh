#!/bin/bash
# Safety net for agents created outside the Asterisk config.
# Ensures every active AGENT (source of truth = the database) has its PJSIP
# endpoint in Asterisk. It ONLY ADDS missing ones (append-only): it never rewrites or deletes,
# so it cannot break the existing config (supervisors, previously created agents).
# This is what makes newly created agents survive restarts.
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
echo "sync_agents_pjsip: $ADDED agent(s) added from the DB"
