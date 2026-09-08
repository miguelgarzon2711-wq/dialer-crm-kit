#!/bin/bash
# 15s daemon - checks and restores critical Redis keys
# Supervised by check_dashboard.sh, started by restore_patches.sh
# The Redis container may carry a different hash prefix in each
# installation: it is auto-detected. If your name has no "redis" in it, hardcode it.
REDIS="$(docker ps --format '{{.Names}}' | grep -m1 redis)"
[ -z "$REDIS" ] && { echo "cannot find the Redis container"; exit 1; }
DJANGO="prod-env-django-app-1"
LOG="/var/log/redis_watchdog.log"

while true; do
    OUTR_KEYS=$(docker exec $REDIS redis-cli -n 0 EXISTS "OML:OUTR:1" "OML:TRUNK:2" "OML:CAMP:9" 2>/dev/null)
    INR_KEY=$(docker exec $REDIS redis-cli -n 0 EXISTS "OML:INR:<DID>" 2>/dev/null)

    if [ "$OUTR_KEYS" != "3" ]; then
        TS=$(date '+%Y-%m-%d %H:%M:%S')
        echo "$TS [ALERT] Outbound keys missing ($OUTR_KEYS/3) - restoring OUTR/TRUNK/CAMP..." >> $LOG
        docker exec $DJANGO python3 -c "
import django,os; os.environ['DJANGO_SETTINGS_MODULE']='ominicontacto.settings.production'
django.setup()
from ominicontacto_app.services.asterisk.redis_database import RutaSalienteFamily, TrunkFamily, RegenerarAsteriskFamilysOML
RutaSalienteFamily().regenerar_families()
TrunkFamily().regenerar_families()
RegenerarAsteriskFamilysOML().regenerar_asterisk()
print('Outbound keys restored')
" >> $LOG 2>&1
        VERIFY=$(docker exec $REDIS redis-cli -n 0 EXISTS "OML:OUTR:1" "OML:TRUNK:2" "OML:CAMP:9" 2>/dev/null)
        echo "$(date '+%Y-%m-%d %H:%M:%S') [OK] After outbound restore: $VERIFY/3" >> $LOG
    fi

    if [ "$INR_KEY" != "1" ]; then
        TS=$(date '+%Y-%m-%d %H:%M:%S')
        echo "$TS [ALERT] OML:INR:<DID> missing - restoring inbound..." >> $LOG
        docker exec $DJANGO python3 -c "
import django,os; os.environ['DJANGO_SETTINGS_MODULE']='ominicontacto.settings.production'
django.setup()
from configuracion_telefonia_app.regeneracion_configuracion_telefonia import SincronizadorDeConfiguracionTelefonicaEnAsterisk
SincronizadorDeConfiguracionTelefonicaEnAsterisk().sincronizar_en_asterisk()
print('Inbound keys restored')
" >> $LOG 2>&1
        VERIFY_INR=$(docker exec $REDIS redis-cli -n 0 EXISTS "OML:INR:<DID>" 2>/dev/null)
        echo "$(date '+%Y-%m-%d %H:%M:%S') [OK] After inbound restore: $VERIFY_INR/1" >> $LOG
    fi

    sleep 15
done
