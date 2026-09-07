#!/bin/bash
# Daemon de 15s — verifica y restaura keys críticas de Redis
# Supervisado por check_dashboard.sh, arrancado por restore_dialer.sh
# El contenedor de Redis puede tener un prefijo de hash distinto en cada
# instalación: se detecta solo. Si tu nombre no contiene "redis", ponelo fijo.
REDIS="$(docker ps --format '{{.Names}}' | grep -m1 redis)"
[ -z "$REDIS" ] && { echo "no encuentro el contenedor de Redis"; exit 1; }
DJANGO="prod-env-django-app-1"
LOG="/var/log/redis_watchdog.log"

while true; do
    OUTR_KEYS=$(docker exec $REDIS redis-cli -n 0 EXISTS "OML:OUTR:1" "OML:TRUNK:2" "OML:CAMP:9" 2>/dev/null)
    INR_KEY=$(docker exec $REDIS redis-cli -n 0 EXISTS "OML:INR:<DID>" 2>/dev/null)

    if [ "$OUTR_KEYS" != "3" ]; then
        TS=$(date '+%Y-%m-%d %H:%M:%S')
        echo "$TS [ALERTA] Keys salientes faltantes ($OUTR_KEYS/3) — restaurando OUTR/TRUNK/CAMP..." >> $LOG
        docker exec $DJANGO python3 -c "
import django,os; os.environ['DJANGO_SETTINGS_MODULE']='ominicontacto.settings.production'
django.setup()
from ominicontacto_app.services.asterisk.redis_database import RutaSalienteFamily, TrunkFamily, RegenerarAsteriskFamilysOML
RutaSalienteFamily().regenerar_families()
TrunkFamily().regenerar_families()
RegenerarAsteriskFamilysOML().regenerar_asterisk()
print('Keys salientes restauradas')
" >> $LOG 2>&1
        VERIFY=$(docker exec $REDIS redis-cli -n 0 EXISTS "OML:OUTR:1" "OML:TRUNK:2" "OML:CAMP:9" 2>/dev/null)
        echo "$(date '+%Y-%m-%d %H:%M:%S') [OK] Post-restauracion salientes: $VERIFY/3" >> $LOG
    fi

    if [ "$INR_KEY" != "1" ]; then
        TS=$(date '+%Y-%m-%d %H:%M:%S')
        echo "$TS [ALERTA] OML:INR:<DID> faltante — restaurando inbound..." >> $LOG
        docker exec $DJANGO python3 -c "
import django,os; os.environ['DJANGO_SETTINGS_MODULE']='ominicontacto.settings.production'
django.setup()
from configuracion_telefonia_app.regeneracion_configuracion_telefonia import SincronizadorDeConfiguracionTelefonicaEnAsterisk
SincronizadorDeConfiguracionTelefonicaEnAsterisk().sincronizar_en_asterisk()
print('Keys inbound restauradas')
" >> $LOG 2>&1
        VERIFY_INR=$(docker exec $REDIS redis-cli -n 0 EXISTS "OML:INR:<DID>" 2>/dev/null)
        echo "$(date '+%Y-%m-%d %H:%M:%S') [OK] Post-restauracion inbound: $VERIFY_INR/1" >> $LOG
    fi

    sleep 15
done
