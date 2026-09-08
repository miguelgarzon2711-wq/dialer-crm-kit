#!/bin/bash
# LIFETIME OWNERSHIP: leads sitting in the pool (INICIAL, agente -1) that have an owner go back to that owner.
# Ownership is PERMANENT (it is not lost by deactivating or deleting the rep).
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads -Atc "
UPDATE ominicontacto_app_agenteencontacto aec SET agente_id = o.agente_id
FROM dialer_lead_owner o
WHERE aec.contacto_id = o.contacto_id AND aec.estado = 0 AND aec.agente_id = -1;" 2>/dev/null | grep -v "UPDATE 0" | sed "s/^/$(date '+%F %T') /" >> /var/log/sync_lead_owner.log
# Stale appointment reminder (>12h queued without a call): closed so it stops blocking the top of the list
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads -Atc "
UPDATE ominicontacto_app_agenteencontacto SET estado = 2
WHERE estado = 0 AND datos_contacto LIKE '%\"TIPO\": \"Recordatorio Cita\"%' AND modificado < now() - interval '12 hours';" 2>/dev/null | grep -v "UPDATE 0" | sed "s/^/$(date '+%F %T') expired reminder /" >> /var/log/sync_lead_owner.log
