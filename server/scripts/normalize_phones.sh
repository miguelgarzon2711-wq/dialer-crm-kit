#!/bin/bash
# Safety net: no queued contact may have a phone number containing
# "+", spaces, dashes or the country "1": the switch only dials 10 digits, and otherwise
# the call dies with no warning. Runs every 10 min and in the pre-launch check.
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads -At -F'|' -c "
WITH malos AS (
  SELECT c.id, c.telefono,
         CASE WHEN length(regexp_replace(c.telefono,'\D','','g'))=11 AND regexp_replace(c.telefono,'\D','','g') LIKE '1%'
              THEN substr(regexp_replace(c.telefono,'\D','','g'),2)
              ELSE regexp_replace(c.telefono,'\D','','g') END AS limpio
    FROM ominicontacto_app_contacto c
   WHERE c.telefono !~ '^[0-9]{10}$'
     AND EXISTS (SELECT 1 FROM ominicontacto_app_agenteencontacto a WHERE a.contacto_id=c.id AND a.estado IN (0,1,3))
)
UPDATE ominicontacto_app_contacto c SET telefono = m.limpio
  FROM malos m WHERE c.id = m.id AND m.limpio ~ '^[0-9]{10}$'
RETURNING c.id, m.telefono, c.telefono;" 2>/dev/null | grep -E '^[0-9]+\|' | while IFS='|' read -r id antes despues; do
  echo "$(date '+%F %T') contact $id: '$antes' -> '$despues'"
done
# anything that could not be fixed (not a 10-digit US number) is reported
docker exec prod-env-postgresql-1 psql -U omnileads -d omnileads -At -F'|' -c "
SELECT c.id, c.telefono FROM ominicontacto_app_contacto c
 WHERE c.telefono !~ '^[0-9]{10}$'
   AND EXISTS (SELECT 1 FROM ominicontacto_app_agenteencontacto a WHERE a.contacto_id=c.id AND a.estado IN (0,1,3));" 2>/dev/null | grep -E '^[0-9]+\|' | while IFS='|' read -r id tel; do
  echo "$(date '+%F %T') WARNING contact $id queued with an undialable phone: '$tel'"
done
