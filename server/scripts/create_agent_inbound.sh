#!/bin/bash
# PER-REP INBOUND STICKY - creates/ensures the personal inbound campaign for EVERY active agent
# (idempotent). Run it after creating new agents. It does: campaigns+routes in the DB (ORM),
# Redis (OML:CAMP / OML:INR / OML:CAMPAIGN-AGENTS), blocks in queues.conf and a reload.
set -e
CFG=/opt/dialer-kit/patches
DJ=prod-env-django-app-1
docker cp /root/create_agent_inbound.py $DJ:/tmp/create_agent_inbound.py
docker exec $DJ python3 /opt/omnileads/ominicontacto/manage.py shell -c "exec(open('/tmp/create_agent_inbound.py').read())" 2>&1 | grep -E "^REGEN|^RESULT|Error|Traceback" > /root/create_agent_inbound.out || true
grep "^RESULT:" /root/create_agent_inbound.out | sed 's/^RESULT://' > /root/agent_inbound.json
python3 /root/inbound_redis_sync.py
python3 - <<'PY'
import json, re
p="/opt/dialer-kit/patches/queues.conf"; s=open(p).read(); n=0
for it in json.load(open("/root/agent_inbound.json")):
    if "[%s]" % it["queue"] in s: continue
    s += "\n[%s]\nstrategy=ringall\ntimeout=20\nretry=5\nwrapuptime=0\nmaxlen=0\njoinempty=yes\nleavewhenempty=no\nreportholdtime=no\nringinuse=no\n" % it["queue"]; n += 1
open(p, "w").write(s); print("queues.conf +%d" % n)
PY
cat $CFG/queues.conf | docker exec -i prod-env-acd-1 tee /etc/asterisk/oml_queues_override.conf > /dev/null
docker exec prod-env-acd-1 asterisk -rx "queue reload all" > /dev/null
echo "personal inbound campaigns: $(python3 -c "import json;print(len(json.load(open('/root/agent_inbound.json'))))") | queues loaded: $(docker exec prod-env-acd-1 asterisk -rx 'queue show' | grep -c '_Inbound A' || true)"
