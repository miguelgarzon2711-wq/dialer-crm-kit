#!/bin/bash
# STICKY DE POR VIDA (decisión de producto): leads en pool (INICIAL, agente -1) con dueño vuelven al dueño.
# El dueño es PERMANENTE (no se pierde por inactivar/borrar al vendedor).
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads -Atc "
UPDATE ominicontacto_app_agenteencontacto aec SET agente_id = o.agente_id
FROM dialer_lead_owner o
WHERE aec.contacto_id = o.contacto_id AND aec.estado = 0 AND aec.agente_id = -1;" 2>/dev/null | grep -v "UPDATE 0" | sed "s/^/$(date '+%F %T') /" >> /var/log/sync_lead_owner.log
# Recordatorio de cita vencido (>12h en cola sin llamar): se cierra para no estorbar arriba de la lista
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads -Atc "
UPDATE ominicontacto_app_agenteencontacto SET estado = 2
WHERE estado = 0 AND datos_contacto LIKE '%\"TIPO\": \"Recordatorio Cita\"%' AND modificado < now() - interval '12 hours';" 2>/dev/null | grep -v "UPDATE 0" | sed "s/^/$(date '+%F %T') recordatorio vencido /" >> /var/log/sync_lead_owner.log
