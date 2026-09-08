# ============================================================================
#  Routes added by this kit.
#
#  Paste this block AT THE END of:
#      /opt/omnileads/ominicontacto/api_app/urls.py
#  inside the Django container (after the `urlpatterns` definition).
#
#  The imports use __import__ on purpose: if a kit module had an
#  error, Django still starts up and only that route fails, instead of the
#  entire application going down.
#
#  After pasting it:
#      python3 -c "import ast; ast.parse(open('urls.py').read())"   # validate
#      docker cp urls.py prod-env-django-app-1:/opt/omnileads/ominicontacto/api_app/urls.py
#      docker restart prod-env-django-app-1
#      curl -s -o /dev/null -w "%{http_code}\n" https://<YOUR_DOMAIN>/accounts/login/   # should give 200
# ============================================================================

_M_CRM = 'api_app.views.crm_webhooks'
_M_APP = 'api_app.views.agent_api'


def _v(modulo, clase):
    """Imports a kit view without breaking Django if the module fails."""
    return __import__(modulo, fromlist=[clase]).__dict__[clase].as_view()


urlpatterns += [
    # ── Lead entry from the CRM ───────────────────────────────────────
    # The CRM calls this route every time a lead must enter, leave or
    # change priority in the dialing queue.
    path('api/v1/crm/lead_action/', _v(_M_CRM, 'CRMLeadActionView'),
         name='crm_lead_action'),

    # Inbound call nobody answered: the lead goes back to the queue with
    # high priority (the customer called, they're waiting).
    path('api/v1/dialer/missed_call/', _v(_M_CRM, 'CRMMissedCallView'),
         name='crm_missed_call'),

    # ── Agent console queries ──────────────────────────────────
    path('api/v1/contact_history/', _v(_M_CRM, 'ContactoHistorialView'),
         name='contact_history'),
    path('api/v1/agente/skip_lead/', _v(_M_CRM, 'SkipLeadView'),
         name='skip_lead'),
    path('api/v1/agente/en_llamada/', _v(_M_CRM, 'EnLlamadaView'),
         name='en_llamada'),
    path('api/v1/agente/call_outcome/', _v(_M_CRM, 'CRMCallOutcomeView'),
         name='call_outcome'),

    # ── REST API for mobile clients ──────────────────────────────────────
    # Only needed if you're going to build your own application for the
    # salespeople. If not, you can delete this entire block.
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

# Importing the disposition engine registers its Django signals: that's what
# makes it so that saving a disposition automatically writes to the CRM.
# Without this line the rest works, but nothing goes back to the CRM.
from api_app.views import crm_dispositions  # noqa: E402,F401
