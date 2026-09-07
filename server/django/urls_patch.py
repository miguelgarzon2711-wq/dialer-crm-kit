# ============================================================================
#  Rutas que agrega este kit.
#
#  Pegar este bloque AL FINAL de:
#      /opt/omnileads/ominicontacto/api_app/urls.py
#  dentro del contenedor de Django (después de la definición de `urlpatterns`).
#
#  Los import van con __import__ a propósito: si un módulo del kit tuviera un
#  error, Django arranca igual y solo falla esa ruta, en vez de quedar la
#  aplicación entera caída.
#
#  Después de pegarlo:
#      python3 -c "import ast; ast.parse(open('urls.py').read())"   # validar
#      docker cp urls.py prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/urls.py
#      docker restart prod-env-django-app-1
#      curl -s -o /dev/null -w "%{http_code}\n" https://<TU_DOMINIO>/accounts/login/   # debe dar 200
# ============================================================================

_M_CRM = 'api_app.views.crm_webhooks'
_M_APP = 'api_app.views.agent_api'


def _v(modulo, clase):
    """Importa una vista del kit sin romper Django si el módulo falla."""
    return __import__(modulo, fromlist=[clase]).__dict__[clase].as_view()


urlpatterns += [
    # ── Entrada de leads desde el CRM ───────────────────────────────────────
    # El CRM llama a esta ruta cada vez que un lead debe entrar, salir o
    # cambiar de prioridad en la cola de marcación.
    path('api/v1/crm/lead_action/', _v(_M_CRM, 'CRMLeadActionView'),
         name='crm_lead_action'),

    # Llamada entrante que nadie contestó: el lead vuelve a la cola con
    # prioridad alta (el cliente llamó, está esperando).
    path('api/v1/dialer/missed_call/', _v(_M_CRM, 'CRMMissedCallView'),
         name='crm_missed_call'),

    # ── Consultas de la consola del agente ──────────────────────────────────
    path('api/v1/contact_history/', _v(_M_CRM, 'ContactoHistorialView'),
         name='contact_history'),
    path('api/v1/agente/skip_lead/', _v(_M_CRM, 'SkipLeadView'),
         name='skip_lead'),
    path('api/v1/agente/en_llamada/', _v(_M_CRM, 'EnLlamadaView'),
         name='en_llamada'),
    path('api/v1/agente/call_outcome/', _v(_M_CRM, 'CRMCallOutcomeView'),
         name='call_outcome'),

    # ── API REST para clientes móviles ──────────────────────────────────────
    # Solo hace falta si vas a construir una aplicación propia para los
    # vendedores. Si no, podés borrar este bloque entero.
    path('api/v1/app/session/', _v(_M_APP, 'AppSessionView'),
         name='app_session'),
    path('api/v1/app/asterisk_login/', _v(_M_APP, 'AppAsteriskLoginView'),
         name='app_asterisk_login'),
    path('api/v1/app/asterisk_logout/', _v(_M_APP, 'AppAsteriskLogoutView'),
         name='app_asterisk_logout'),
    path('api/v1/app/lead/', _v(_M_APP, 'AppLeadView'),
         name='app_lead'),
    path('api/v1/app/llamar/', _v(_M_APP, 'AppLlamarView'),
         name='app_llamar'),
    path('api/v1/app/liberar/', _v(_M_APP, 'AppLiberarView'),
         name='app_liberar'),
    path('api/v1/app/opciones/', _v(_M_APP, 'AppOpcionesView'),
         name='app_opciones'),
    path('api/v1/app/calificar/', _v(_M_APP, 'AppCalificarView'),
         name='app_calificar'),
    path('api/v1/app/estado/', _v(_M_APP, 'AppEstadoView'),
         name='app_estado'),
    path('api/v1/app/llamada_terminada/', _v(_M_APP, 'AppLlamadaTerminadaView'),
         name='app_llamada_terminada'),
    path('api/v1/app/despausar/', _v(_M_APP, 'AppDespausarView'),
         name='app_despausar'),
    path('api/v1/app/notas/', _v(_M_APP, 'AppNotasView'),
         name='app_notas'),
]

# Importar el motor de disposiciones registra sus señales de Django: es lo que
# hace que al guardar una disposición se escriba automáticamente en el CRM.
# Sin esta línea el resto funciona, pero nada vuelve al CRM.
from api_app.views import crm_dispositions  # noqa: E402,F401
