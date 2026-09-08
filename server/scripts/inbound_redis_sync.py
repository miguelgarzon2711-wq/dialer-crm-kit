#!/usr/bin/env python3
"""Ensures in Redis the families for the personal inbound campanas (OML:CAMP:<id>, OML:INR:<did>,
OML:CAMPAIGN-AGENTS:<id>) from /root/agent_inbound.json. Idempotent; called by
create_agent_inbound.sh and restore_patches.sh."""
import json, subprocess
R = lambda *a: subprocess.run(["docker", "exec", "prod-env-redis-1", "redis-cli"] + list(a),
                              capture_output=True, text=True).stdout.strip()
raw = R("hgetall", "OML:CAMP:7").split("\n")
base = dict(zip(raw[0::2], raw[1::2]))
n = 0
for it in json.load(open("/root/agent_inbound.json")):
    cid, did, qn = it["campana_id"], it["did"], it["queue"]
    nm = qn.split("_", 1)[1]
    if R("exists", "OML:CAMP:%d" % cid) != "1":
        h = dict(base); h.update(QNAME=qn, SHOWCAMPNAME=nm, QUEUETIME="20")
        args = []
        for k, v in h.items():
            args.extend([k, v])
        R("hset", "OML:CAMP:%d" % cid, *args); n += 1
    else:
        R("hset", "OML:CAMP:%d" % cid, "QUEUETIME", "20")
    R("hset", "OML:INR:%s" % did, "NAME", nm, "DST", "1,%d" % cid, "ID", str(it["ruta_id"]), "LANG", "es")
    if R("exists", "OML:CAMPAIGN-AGENTS:%d" % cid) != "1":
        R("sadd", "OML:CAMPAIGN-AGENTS:%d" % cid, str(it["agente_id"]))
print("redis personal inbound OK (CAMP manually created: %d)" % n)
