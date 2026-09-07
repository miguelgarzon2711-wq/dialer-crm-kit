#!/bin/bash
# Firewall el cliente — cierra puertos internos de OMniLeads al exterior (solo red interna).
# Causa: incidente 2026-06-20, Redis (6379) y otros 12 puertos internos estaban expuestos a
# internet via docker-proxy; atacantes explotaban Redis -> caidas del dialer.
# La regla -i enp1s0 DROP solo bloquea trafico ENTRANTE desde internet; el trafico interno
# (containers / hairpin a la IP publica) NO entra por enp1s0 -> no se afecta.
WAN=enp1s0
# puertos internos que NO deben ser publicos:
PORTS="1440 4573 4730 5038 6379 7088 8000 8098 8099 8888 9000 9001 9191 22223"
for P in $PORTS; do
  iptables -C DOCKER-USER -i $WAN -p tcp --dport $P -j DROP 2>/dev/null || \
    iptables -I DOCKER-USER -i $WAN -p tcp --dport $P -j DROP
done
echo "firewall: $(date) — puertos internos cerrados al exterior en $WAN"

# Puertos de servicios del HOST (dashboard etc.) — bloquear INPUT directo desde internet
HOST_PORTS="8001 8002 3001 6379 5038 9000 9001"
for P in $HOST_PORTS; do
  iptables -C INPUT -i $WAN -p tcp --dport $P -j DROP 2>/dev/null || \
    iptables -I INPUT -i $WAN -p tcp --dport $P -j DROP
done
echo "firewall: INPUT host ports cerrados"

# 2026-09-05: softphones de internet se registraban como agentes (1100-1110) por el 5060 sin clave.
# Telnyx nunca manda REGISTER y los agentes entran por Kamailio (loopback): se bloquea REGISTER
# desde internet en 5060 y el puerto de agentes 5160 completo.
for CH in INPUT DOCKER-USER; do
  for PR in udp tcp; do
    iptables -C $CH -i $WAN -p $PR --dport 5060 -m string --string "REGISTER sip:" --algo bm -j DROP 2>/dev/null || \
      iptables -I $CH -i $WAN -p $PR --dport 5060 -m string --string "REGISTER sip:" --algo bm -j DROP
    iptables -C $CH -i $WAN -p $PR --dport 5160 -j DROP 2>/dev/null || \
      iptables -I $CH -i $WAN -p $PR --dport 5160 -j DROP
  done
done
