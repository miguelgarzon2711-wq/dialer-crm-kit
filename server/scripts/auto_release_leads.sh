#!/bin/bash
# Auto-release el cliente: contactos ENTREGADOS sin llamar en >10 min -> vuelven al pool (INICIAL)
# 2026-09-06 (el operador): si el lead NO tiene dueño vuelve con prioridad 1 (como llamada perdida)
# para que el siguiente vendedor libre lo llame ya; si tiene dueño vuelve al dueño con su prioridad.
# 2026-09-06 noche (el operador): al devolverlo, si NO tiene dueño se DESASIGNA también en GHL
# (al obtenerlo se le había asignado al vendedor para que pudiera entrar al CRM; si nunca
# lo llamó, no puede quedarse con el lead en LeadConnector). Con dueño no se toca.
# Campañas Preview 1-5. Pedido decisión de producto.
CONTAINER=prod-env-postgresql-1
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

CANDIDATOS=$(docker exec "$CONTAINER" psql -U omnileads -d omnileads -t -A -F'|' -c \
  "SELECT a.id, a.contacto_id, (a.datos_contacto::json)->>'NOMBRE',
          CASE WHEN EXISTS (SELECT 1 FROM dialer_lead_owner o WHERE o.contacto_id=a.contacto_id) THEN 'dueno' ELSE 'libre' END
   FROM ominicontacto_app_agenteencontacto a
   WHERE a.campana_id BETWEEN 1 AND 5 AND a.estado=1 AND a.modificado < NOW() - INTERVAL '10 minutes';")

if [ -n "$CANDIDATOS" ]; then
  docker exec "$CONTAINER" psql -U omnileads -d omnileads -c \
    "UPDATE ominicontacto_app_agenteencontacto aec SET estado=0,
       agente_id=COALESCE((SELECT o.agente_id FROM dialer_lead_owner o WHERE o.contacto_id=aec.contacto_id), -1),
       orden=CASE WHEN EXISTS (SELECT 1 FROM dialer_lead_owner o WHERE o.contacto_id=aec.contacto_id)
                  THEN aec.orden ELSE LEAST(aec.orden, 1) END
     WHERE campana_id BETWEEN 1 AND 5 AND estado=1 AND modificado < NOW() - INTERVAL '10 minutes';" >/dev/null 2>&1
  LIBRES=""
  while IFS='|' read -r aec_id contacto_id nombre tiene; do
    [ -n "$aec_id" ] && echo "[$TIMESTAMP] RELEASE aec_id=$aec_id contacto_id=$contacto_id nombre=$nombre ($tiene)" >> /var/log/auto_release_leads.log
    [ "$tiene" = "libre" ] && LIBRES="$LIBRES,$contacto_id"
  done <<< "$CANDIDATOS"
  LIBRES="${LIBRES#,}"
  if [ -n "$LIBRES" ]; then
    # desasignar en GHL (owner=null) los que no tienen dueño de por vida
    docker exec prod-env-django-app-1 python3 /opt/omnileads/ominicontacto/manage.py shell -c "
import requests, logging
from api_app.views import crm_dispositions as d
from ominicontacto_app.models import Contacto
tok = d._env().get('GHL_API_TOKEN')
H = {'Authorization': 'Bearer %s' % tok, 'Version': d.GHL_VERSION, 'Content-Type': 'application/json'}
for c in Contacto.objects.filter(id__in=[$LIBRES]):
    g = (c.id_externo or '').strip()
    if not g or not tok:
        continue
    try:
        r = requests.put('%s/contacts/%s' % (d.GHL_BASE, g), headers=H, json={'assignedTo': None}, timeout=10)
        print('DESASIGNADO', c.id, g, r.status_code)
    except Exception as e:
        print('ERROR', c.id, e)
" 2>/dev/null | grep -E '^(DESASIGNADO|ERROR)' | while read -r linea; do
      echo "[$TIMESTAMP] GHL $linea" >> /var/log/auto_release_leads.log
    done
  fi
fi
