#!/bin/bash
# Deletes agent registrations in Asterisk that do NOT come from the SIP proxy (127.0.0.1)
docker exec prod-env-acd-1 sh -lc 'asterisk -rx "database show registrar/contact"' \
 | grep -v '"via_addr":"127.0.0.1"' | grep -oE '^/registrar/contact/[^:]+' | sed 's|^/registrar/contact/||' \
 | while read -r key; do
     [ -z "$key" ] && continue
     docker exec prod-env-acd-1 sh -lc "asterisk -rx 'database del registrar/contact $key'" >/dev/null && echo "  deleted: $key"
   done
echo "--- external contacts remaining ---"
docker exec prod-env-acd-1 sh -lc 'asterisk -rx "pjsip show contacts"' | sed 's/\x1b\[[0-9;]*m//g' | grep -E 'Contact:\s+1[01][0-9][0-9]/' | grep -v '127.0.0.1' | wc -l
echo "--- extension 1100 now ---"
docker exec prod-env-acd-1 sh -lc 'asterisk -rx "pjsip show aor 1100"' | sed 's/\x1b\[[0-9;]*m//g' | grep -E 'Contact:\s+1100' || echo "  (no contact: correct, that agent is not connected)"
