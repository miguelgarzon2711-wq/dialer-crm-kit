#!/bin/bash
# Firewall - closes OMniLeads internal ports to the outside (internal network only).
# Origin: a real incident where Redis (6379) and 12 other internal ports were exposed to
# the internet via docker-proxy; attackers exploited Redis -> dialer outages.
# The -i enp1s0 DROP rule only blocks INBOUND traffic from the internet; internal traffic
# (containers / hairpin to the public IP) does NOT come in through enp1s0 -> unaffected.
WAN=enp1s0
# internal ports that must NOT be public:
PORTS="1440 4573 4730 5038 6379 7088 8000 8098 8099 8888 9000 9001 9191 22223"
for P in $PORTS; do
  iptables -C DOCKER-USER -i $WAN -p tcp --dport $P -j DROP 2>/dev/null || \
    iptables -I DOCKER-USER -i $WAN -p tcp --dport $P -j DROP
done
echo "firewall: $(date) - internal ports closed to the outside on $WAN"

# HOST service ports (dashboard, etc.) - block direct INPUT from the internet
HOST_PORTS="8001 8002 3001 6379 5038 9000 9001"
for P in $HOST_PORTS; do
  iptables -C INPUT -i $WAN -p tcp --dport $P -j DROP 2>/dev/null || \
    iptables -I INPUT -i $WAN -p tcp --dport $P -j DROP
done
echo "firewall: host INPUT ports closed"

# Real case: softphones from the internet registered as agents (1100-1110) over 5060 with no password.
# The SIP provider never sends REGISTER and agents come in through the proxy (loopback): REGISTER is blocked
# from the internet on 5060, plus the whole agent port 5160.
for CH in INPUT DOCKER-USER; do
  for PR in udp tcp; do
    iptables -C $CH -i $WAN -p $PR --dport 5060 -m string --string "REGISTER sip:" --algo bm -j DROP 2>/dev/null || \
      iptables -I $CH -i $WAN -p $PR --dport 5060 -m string --string "REGISTER sip:" --algo bm -j DROP
    iptables -C $CH -i $WAN -p $PR --dport 5160 -j DROP 2>/dev/null || \
      iptables -I $CH -i $WAN -p $PR --dport 5160 -j DROP
  done
done
