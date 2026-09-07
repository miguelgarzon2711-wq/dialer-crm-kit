#!/bin/bash
# Restore el cliente v1 (2026-08-21): nginx conf OML + certs. Crece con el proyecto (campañas, agentes, etc.)
LOG=/var/log/restore_patches.log
NGINX=$(docker ps --format '{{.Names}}' | grep nginx | head -1)
if [ -n "$NGINX" ]; then
  if ! docker exec $NGINX test -f /etc/nginx/conf.d/ominicontacto.conf 2>/dev/null; then
    docker cp /opt/dialer-kit/patches/nginx/ominicontacto.conf $NGINX:/etc/nginx/conf.d/ominicontacto.conf
    docker cp /opt/dialer-kit/patches/nginx/environment $NGINX:/etc/nginx/conf.d/environment
    docker exec $NGINX nginx -s reload
    echo "$(date): nginx conf OML restaurada en $NGINX" >> $LOG
  fi
fi
echo "$(date): restore_patches OK" >> $LOG

# Parche anti-autofinalizacion de campanas Preview (decisión de producto)
if ! docker exec prod-env-django-app-1 grep -q 'PATCH' /opt/omnileads/ominicontacto/ominicontacto_app/models.py 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/models.py.patched prod-env-django-app-1:/opt/omnileads/ominicontacto/ominicontacto_app/models.py
  docker restart prod-env-django-app-1
  echo "$(date): models.py re-parcheado (anti-autofinalize)" >> /var/log/restore_patches.log
fi

# Queues de campañas Preview (2026-08-22)
if ! docker exec prod-env-acd-1 grep -q '1_Grupo 1' /etc/asterisk/oml_queues_override.conf 2>/dev/null; then
  cat /opt/dialer-kit/patches/queues.conf | docker exec -i prod-env-acd-1 tee /etc/asterisk/oml_queues_override.conf > /dev/null
  docker exec prod-env-acd-1 asterisk -rx 'module reload app_queue.so'
  echo "$(date): queues Preview restauradas" >> /var/log/restore_patches.log
fi

# Parche caller ID sticky+rampa en dialplan (2026-08-22)
if ! docker exec prod-env-acd-1 grep -q 'PATCH CID' /etc/asterisk/oml_extensions_precall.conf 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/oml_extensions_precall.conf.patched prod-env-acd-1:/etc/asterisk/oml_extensions_precall.conf
  docker exec prod-env-acd-1 asterisk -rx 'dialplan reload'
  echo "$(date): dialplan CID re-parcheado" >> /var/log/restore_patches.log
fi
systemctl is-active did-picker >/dev/null || systemctl restart did-picker

# Endpoint GHL lead_action (2026-08-22)
if ! docker exec prod-env-django-app-1 test -f /opt/omnileads/ominicontacto/api_app/views/crm_webhooks.py 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/crm_webhooks.py prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/views/crm_webhooks.py
  docker cp /opt/dialer-kit/patches/api_urls.py.patched prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/urls.py
  docker restart prod-env-django-app-1
  echo "$(date): endpoint crm_webhooks restaurado" >> /var/log/restore_patches.log
fi

# API de la APP MOVIL (2026-09-04)
if ! docker exec prod-env-django-app-1 test -f /opt/omnileads/ominicontacto/api_app/views/agent_api.py 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/agent_api.py prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/views/agent_api.py
  docker cp /opt/dialer-kit/patches/api_urls.py.patched prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/urls.py
  docker restart prod-env-django-app-1
  echo "$(date): api app movil restaurada" >> /var/log/restore_patches.log
fi

# Motor disposiciones GHL + env (2026-08-22)
# STICKY DE POR VIDA (2026-09-03)
if ! docker exec prod-env-django-app-1 test -f /opt/omnileads/ominicontacto/api_app/views/lead_ownership.py 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/lead_ownership.py prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/views/lead_ownership.py
  echo "$(date): lead_ownership restaurado" >> /var/log/restore_patches.log
fi
if ! docker exec prod-env-django-app-1 test -f /opt/omnileads/ominicontacto/api_app/views/crm_dispositions.py 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/crm_dispositions.py prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/views/crm_dispositions.py
  docker cp /root/.env_dialer prod-env-django-app-1:/opt/omnileads/.env_dialer
  docker exec prod-env-django-app-1 chmod 644 /opt/omnileads/.env_dialer
  echo "$(date): motor dispo GHL restaurado" >> /var/log/restore_patches.log
fi

# uwsgi 4 workers (2026-08-22)
if ! docker exec prod-env-django-app-1 grep -q 'processes=4' /opt/omnileads/run/oml_uwsgi.ini 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/oml_uwsgi.ini.patched prod-env-django-app-1:/opt/omnileads/run/oml_uwsgi.ini
  docker restart prod-env-django-app-1
  echo "$(date): uwsgi re-parcheado" >> /var/log/restore_patches.log
fi

# Boton unificado: JS consola + rebuild bundle (2026-08-22)
if ! docker exec prod-env-django-app-1 grep -q campEfectiva /opt/omnileads/ominicontacto/ominicontacto_app/static/ominicontacto/JS/campanasPreviewAgente.js 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/campanasPreviewAgente.js.patched prod-env-django-app-1:/opt/omnileads/ominicontacto/ominicontacto_app/static/ominicontacto/JS/campanasPreviewAgente.js
  docker exec prod-env-django-app-1 sh -c 'cd /opt/omnileads/ominicontacto && python3 manage.py collectstatic --noinput >/dev/null 2>&1 && python3 manage.py compress --force >/dev/null 2>&1'
  echo "$(date): JS boton unificado restaurado + bundle regenerado" >> /var/log/restore_patches.log
fi

# Override DID inbound Telnyx (2026-08-22)
if ! docker exec prod-env-acd-1 grep -q 'Workaround Telnyx' /etc/asterisk/oml_extensions_override.conf 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/extensions_override.conf prod-env-acd-1:/etc/asterisk/oml_extensions_override.conf
  docker exec prod-env-acd-1 asterisk -rx 'dialplan reload'
  echo "$(date): override DID inbound restaurado" >> /var/log/restore_patches.log
fi

# Endpoints PJSIP de agentes (append-only, fuente = DB) (2026-08-25)
bash /root/sync_agents_pjsip.sh >> /var/log/restore_patches.log 2>&1

# CSRF trusted origins (2026-08-25)
if ! docker exec prod-env-django-app-1 grep -q 'CSRF_TRUSTED' /opt/omnileads/ominicontacto/ominicontacto/settings/oml_settings_local.py 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/oml_settings_local.py.patched prod-env-django-app-1:/opt/omnileads/ominicontacto/ominicontacto/settings/oml_settings_local.py
  docker restart prod-env-django-app-1
  echo "$(date): CSRF settings restaurados" >> /var/log/restore_patches.log
fi

# Parches paridad otro cliente: pausa libera ENTREGADO + race ACW 5s (2026-08-26)
if ! docker exec prod-env-django-app-1 grep -q 'liberar contacto ENTREGADO' /opt/omnileads/ominicontacto/ominicontacto_app/services/asterisk/agent_activity.py 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/agent_activity.py.patched prod-env-django-app-1:/opt/omnileads/ominicontacto/ominicontacto_app/services/asterisk/agent_activity.py
  docker restart prod-env-django-app-1
fi
if ! docker exec prod-env-dialer-acd-dialplan-1 grep -q '_delayed_decr' /app/app.py 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/dialer_app.py.patched prod-env-dialer-acd-dialplan-1:/app/app.py
  docker restart prod-env-dialer-acd-dialplan-1
fi

# Template disposición con control de flujo (2026-08-26)
if ! docker exec prod-env-django-app-1 grep -q 'callInitiatedTimer' /opt/omnileads/ominicontacto/ominicontacto_app/templates/formulario/calificacion_create_update_agente.html 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/calificacion_create_update_agente.html prod-env-django-app-1:/opt/omnileads/ominicontacto/ominicontacto_app/templates/formulario/calificacion_create_update_agente.html
  docker restart prod-env-django-app-1
  echo "$(date): template disposicion restaurado" >> /var/log/restore_patches.log
fi

# Kamailio TLS cfg (certs correctos — sin esto el webphone da SIP Proxy no responde) (2026-09-02)
if ! docker exec prod-env-kamailio-webrtc-1 grep -q 'certs/cert.pem' /etc/kamailio/tls.cfg 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/kamailio_tls.cfg.patched prod-env-kamailio-webrtc-1:/etc/kamailio/tls.cfg
  docker restart prod-env-kamailio-webrtc-1
  echo "$(date): kamailio tls.cfg restaurado" >> /var/log/restore_patches.log
fi

# nginx /ws TLS legacy hacia kamailio (2026-09-02) — sin esto: SIP Proxy no responde
if ! docker exec prod-env-nginx-1 grep -q 'proxy_ssl_protocols' /etc/nginx/conf.d/environment/oml_env.conf 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/nginx/oml_env.conf.patched prod-env-nginx-1:/etc/nginx/conf.d/environment/oml_env.conf
  docker exec prod-env-nginx-1 nginx -s reload
  echo "$(date): nginx /ws restaurado" >> /var/log/restore_patches.log
fi

# Template preview con Ir al CRM + historial + Saltar WA (2026-09-02)
if ! docker exec prod-env-django-app-1 grep -q 'CRM_LOCATION_ID' /opt/omnileads/ominicontacto/ominicontacto_app/templates/agente/campanas_preview.html 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/campanas_preview.html.patched prod-env-django-app-1:/opt/omnileads/ominicontacto/ominicontacto_app/templates/agente/campanas_preview.html
  docker restart prod-env-django-app-1
fi

# Ruta saliente oml-outr-1 (generador roto) (2026-09-03)
if ! docker exec prod-env-acd-1 grep -q '^\[oml-outr\]' /etc/asterisk/oml_extensions_outr_override.conf 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/extensions_outbound_route.conf prod-env-acd-1:/etc/asterisk/oml_extensions_outr_override.conf
  docker exec prod-env-acd-1 asterisk -rx 'dialplan reload'
  echo "$(date): oml-outr-1 restaurado" >> /var/log/restore_patches.log
fi

# INBOUND STICKY POR VENDEDOR (2026-09-03): asegurar Redis de las campañas inbound personales
if [ -f /root/agent_inbound.json ]; then python3 /root/inbound_redis_sync.py >> /var/log/restore_patches.log 2>&1; fi

# MASCARA + ORDEN ALFABETICO (2026-09-03): archivos OML parcheados
if ! docker exec prod-env-django-app-1 grep -q 'PATCH MASCARA' /opt/omnileads/ominicontacto/ominicontacto_app/views_campana_preview.py 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/views_campana_preview.py.patched prod-env-django-app-1:/opt/omnileads/ominicontacto/ominicontacto_app/views_campana_preview.py
  docker cp /opt/dialer-kit/patches/forms_base.py.patched prod-env-django-app-1:/opt/omnileads/ominicontacto/ominicontacto_app/forms/base.py
  echo "$(date): mascara/orden restaurados" >> /var/log/restore_patches.log
fi

# MASCARA en formulario de disposicion (2026-09-03)
if ! docker exec prod-env-django-app-1 grep -q 'PATCH MASCARA' /opt/omnileads/ominicontacto/ominicontacto_app/views_agente.py 2>/dev/null; then
  docker cp /opt/dialer-kit/patches/views_calificacion_cliente.py.patched prod-env-django-app-1:/opt/omnileads/ominicontacto/ominicontacto_app/views_calificacion_cliente.py
  docker cp /opt/dialer-kit/patches/views_agente.py.patched prod-env-django-app-1:/opt/omnileads/ominicontacto/ominicontacto_app/views_agente.py
  echo "$(date): mascara formulario restaurada" >> /var/log/restore_patches.log
fi
