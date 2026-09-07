#!/usr/bin/env python3
"""Asegura en Redis las familias de las campañas inbound personales (OML:CAMP:<id>, OML:INR:<did>,
OML:CAMPAIGN-AGENTS:<id>) a partir de /root/agent_inbound.json. Idempotente; lo llama
create_agent_inbound.sh y restore_patches.sh."""
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
print("redis inbound personales OK (CAMP creados manualmente: %d)" % n)
