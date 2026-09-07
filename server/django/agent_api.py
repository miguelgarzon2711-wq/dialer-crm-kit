# -*- coding: utf-8 -*-
"""
API para la APP MOVIL de los vendedores de el cliente (2026-09-04).

Expone lo minimo que necesita la app para levantar un telefono nativo:
  GET  /api/v1/app/session/          -> datos del agente + credenciales SIP efimeras + config WSS
  POST /api/v1/app/asterisk_login/   -> deja al agente disponible en sus colas (igual que entrar a la consola)
  POST /api/v1/app/asterisk_logout/  -> lo saca de las colas

Se usan endpoints propios (no los nativos de OML) porque los nativos exigen el
sistema de permisos por API de OMniLeads (TienePermisoOML), que no esta habilitado
para el rol Agente en esta instalacion. Aca la autorizacion es simple: token del
propio agente (POST /api/v1/login) y el agente solo puede pedir SUS datos.
"""
import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated

from django.conf import settings
from django.db import connection, transaction

from api_app.authentication import ExpiringTokenAuthentication
from ominicontacto_app.services.kamailio_service import KamailioService

logger = logging.getLogger(__name__)

# La consola web muestra las fechas en hora de Miami; la app hace lo mismo.
from zoneinfo import ZoneInfo
_TZ_MIAMI = ZoneInfo('America/New_York')


def _fecha_miami(dt, larga=False):
    try:
        f = '%d/%m/%Y - %H:%M' if larga else '%d/%m %H:%M'
        return dt.astimezone(_TZ_MIAMI).strftime(f) if dt else ''
    except Exception:
        return ''

# Host publico por el que la app llega al Kamailio (mismo camino que el webphone del navegador).
WS_HOST = 'dialer.example.com'
# Dominio SIP que espera Kamailio (KAMAILIO_HOSTNAME dentro del server).
SIP_DOMAIN = '127.0.0.1'
REGISTER_EXPIRES = 120


def _agente_de(request):
    user = request.user
    if not user or not user.is_authenticated or not user.get_is_agente():
        return None
    return user.get_agente_profile()


class AppSessionView(APIView):
    """Todo lo que la app necesita para registrarse por SIP y saber quien es."""
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)
    permission_classes = (IsAuthenticated,)
    http_method_names = ['get']

    def get(self, request):
        agente = _agente_de(request)
        if agente is None:
            return Response({'error': 'el usuario no es un agente'}, status=403)

        kam = KamailioService()
        ttl = getattr(settings, 'EPHEMERAL_USER_TTL', 28800)
        timestamp = kam.generar_sip_timestamp(ttl)
        sip_user = kam.generar_sip_user(agente.sip_extension, timestamp)
        sip_password = kam.generar_sip_password(sip_user)
        if sip_password is None:
            return Response({'error': 'no se pudo generar la password SIP'}, status=500)

        campanas = []
        try:
            for c in agente.get_campanas_preview_activas_miembro():
                campanas.append({'id': c.id, 'nombre': c.nombre, 'tipo': c.type})
        except Exception:
            # el related_name cambia entre versiones; la app puede vivir sin esto
            logger.info('APP session: no se pudieron listar campanas de %s', agente.id)

        user = request.user
        return Response({
            'agente': {
                'id': agente.id,
                'username': user.username,
                'nombre': (user.get_full_name() or user.username).strip(),
                'sip_extension': agente.sip_extension,
            },
            'sip': {
                # el usuario efimero va TAL CUAL en el URI (sip:<user>@<domain>) y en el
                # header Authorization: Kamailio saca la extension con $(fu{s.select,2,:})
                'user': sip_user,
                'password': sip_password,
                'domain': SIP_DOMAIN,
                'ws_url': 'wss://{0}/ws'.format(WS_HOST),
                'register_expires': REGISTER_EXPIRES,
                'expires_at': int(str(timestamp).split('.')[0]),
            },
            'campanas': campanas,
        })


class AppAsteriskLoginView(APIView):
    """Marca al agente como logueado/disponible en Asterisk (colas entrantes + boton unificado)."""
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)
    permission_classes = (IsAuthenticated,)
    http_method_names = ['post']

    def post(self, request):
        agente = _agente_de(request)
        if agente is None:
            return Response({'error': 'el usuario no es un agente'}, status=403)
        # sesión única: entra el celular -> la consola del computador queda fuera
        _expulsar_consola_web(agente)
        _marcar_dispositivo(agente, 'app')
        try:
            from ominicontacto_app.services.asterisk.agent_activity import AgentActivityAmiManager
            error = AgentActivityAmiManager().login_agent(agente, manage_connection=True)
        except Exception as e:
            logger.error('APP asterisk_login error agente=%s: %s', agente.id, e)
            return Response({'status': 'ERROR', 'detalle': str(e)}, status=500)
        if error:
            return Response({'status': 'ERROR'}, status=500)
        logger.info('APP asterisk_login OK agente=%s', agente.id)
        # Sesión única también entre dos celulares (2026-09-06): el token de DRF es uno por
        # usuario, así que dos aparatos con el mismo login compartían token y ninguno salía.
        # Se rota: el aparato que entra recibe el token nuevo y el otro queda fuera (401).
        token_nuevo = None
        # (solo al arrancar la app: al reengancharse desde segundo plano no se rota, para
        #  no dejar el celular sin token si se pierde la respuesta en una red mala)
        rotar = bool(request.data.get('rotar_token')) if hasattr(request, 'data') else False
        try:
            if not rotar:
                raise StopIteration()
            from rest_framework.authtoken.models import Token
            Token.objects.filter(user=agente.user).delete()
            token_nuevo = Token.objects.create(user=agente.user).key
        except StopIteration:
            pass
        except Exception as e:
            logger.error('APP asterisk_login: no se pudo rotar el token agente=%s: %s', agente.id, e)
        resp = {'status': 'OK'}
        if token_nuevo:
            resp['token'] = token_nuevo
        return Response(resp)


class AppAsteriskLogoutView(APIView):
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)
    permission_classes = (IsAuthenticated,)
    http_method_names = ['post']

    def post(self, request):
        agente = _agente_de(request)
        if agente is None:
            return Response({'error': 'el usuario no es un agente'}, status=403)
        try:
            from ominicontacto_app.services.asterisk.agent_activity import AgentActivityAmiManager
            AgentActivityAmiManager().logout_agent(agente, manage_connection=True)
        except Exception as e:
            logger.error('APP asterisk_logout error agente=%s: %s', agente.id, e)
            return Response({'status': 'ERROR', 'detalle': str(e)}, status=500)
        logger.info('APP asterisk_logout OK agente=%s', agente.id)
        return Response({'status': 'OK'})


# ─────────────────────────────────────────────────────────────────────────────
# CONSOLA DEL AGENTE (2026-09-04): mismo comportamiento que la consola web.
# Ver app-movil/COMPORTAMIENTO_DIALER.md — la app NO decide nada, solo pinta.
# ─────────────────────────────────────────────────────────────────────────────

def _enmascarar(telefono):
    t = str(telefono or '')
    return '***-***-' + t[-4:] if len(t) >= 4 else ''


def _contexto_del_lead(contacto_id, campana_id):
    """Lo que el vendedor necesita ver de una, sin consultar a GoHighLevel.
    Sale de la base del propio dialer: ultima disposicion, quien y cuando, y la
    nota que escribio la IA a partir de la grabacion."""
    ctx = {'ultima_disposicion': None, 'nota_ia': None, 'historial': []}
    # OJO: en PostgreSQL un error SQL aborta la transaccion COMPLETA de la request
    # (aunque se capture la excepcion) y se pierde la entrega del lead. Por eso cada
    # consulta de contexto va en su propio savepoint.
    try:
        # La tabla de la pagina de calificacion de la web lista el HISTORIAL de la
        # calificacion vigente (una fila por cada guardado: django-simple-history),
        # no solo el estado actual. Aca igual.
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute("""
                SELECT oc.nombre, h.observaciones, h.history_date,
                       COALESCE(NULLIF(TRIM(u.first_name || ' ' || u.last_name), ''), u.username)
                  FROM ominicontacto_app_historicalcalificacioncliente h
                  JOIN ominicontacto_app_opcioncalificacion oc ON oc.id = h.opcion_calificacion_id
                  LEFT JOIN ominicontacto_app_agenteprofile a ON a.id = h.agente_id
                  LEFT JOIN ominicontacto_app_user u ON u.id = a.user_id
                 WHERE h.id = (SELECT cc.id FROM ominicontacto_app_calificacioncliente cc
                                JOIN ominicontacto_app_opcioncalificacion o2 ON o2.id = cc.opcion_calificacion_id
                               WHERE cc.contacto_id = %s AND o2.campana_id = %s
                               ORDER BY cc.modified DESC LIMIT 1)
                   AND h.history_type <> '-'
                 ORDER BY h.history_date DESC LIMIT 10
            """, [contacto_id, campana_id])
            for nombre, obs, fecha, agente in cur.fetchall():
                item = {
                    'disposicion': nombre,
                    'observaciones': obs or '',
                    'fecha': _fecha_miami(fecha),
                    'fecha_larga': _fecha_miami(fecha, larga=True),
                    'agente': agente,
                }
                if ctx['ultima_disposicion'] is None:
                    ctx['ultima_disposicion'] = item
                ctx['historial'].append(item)
    except Exception as e:
        logger.error('APP contexto disposiciones error contacto=%s: %s', contacto_id, e)

    # Nota escrita por la IA (Whisper + GPT) de la ultima llamada grabada.
    try:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute("""
                SELECT nota_crm, fecha FROM dialer_call_audit
                 WHERE contacto_id = %s AND nota_crm IS NOT NULL AND nota_crm <> ''
                 ORDER BY fecha DESC LIMIT 1
            """, [contacto_id])
            fila = cur.fetchone()
            if fila:
                ctx['nota_ia'] = {'texto': fila[0], 'fecha': _fecha_miami(fila[1])}
    except Exception:
        # la columna nota_crm se agrega junto con el cambio de transcribe_calls.py
        pass
    return ctx


class AppLeadView(APIView):
    """GET /api/v1/app/lead/ -> el lead que le toca al agente (boton unificado).

    Es el mismo `entregar_contacto` de la consola web: respeta prioridades 0-6,
    el dueño de por vida, "un solo lead a la vez" y el bloqueo anti-colision.
    """
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)
    permission_classes = (IsAuthenticated,)
    http_method_names = ['get']

    def get(self, request):
        agente = _agente_de(request)
        if agente is None:
            return Response({'error': 'el usuario no es un agente'}, status=403)

        from ominicontacto_app.models import AgenteEnContacto, Campana
        campanas = list(agente.get_campanas_preview_activas_miembro().values_list('id', flat=True))
        if not campanas:
            with connection.cursor() as cur:
                cur.execute("SELECT DISTINCT split_part(id_campana,'_',1)::int "
                            "FROM queue_member_table WHERE member_id=%s", [agente.id])
                ids = [r[0] for r in cur.fetchall()]
            campanas = list(Campana.objects.filter(
                id__in=ids, estado=Campana.ESTADO_ACTIVA,
                type=Campana.TYPE_PREVIEW).values_list('id', flat=True))
        if not campanas:
            return Response({'hay_lead': False, 'motivo': 'el agente no tiene campañas activas'})

        data = AgenteEnContacto.entregar_contacto(agente, campanas[0])
        if data.get('result') != 'OK':
            return Response({'hay_lead': False, 'motivo': data.get('data') or data.get('code')})

        datos = data.get('datos_contacto') or {}
        contacto_id = data.get('contacto_id')
        campana_id = data.get('campana_id')
        campana = Campana.objects.filter(pk=campana_id).first()
        _precargar_notas(contacto_id, campana_id)  # notas del CRM listas antes de que la app las pida

        return Response({
            'hay_lead': True,
            'contacto_id': contacto_id,
            'campana': {'id': campana_id, 'nombre': campana.nombre if campana else ''},
            'nombre': datos.get('nombre') or datos.get('NOMBRE') or datos.get('Nombre') or '',
            'tipo': datos.get('TIPO') or '',
            'ghl_id': datos.get('GHL_ID') or '',
            'telefono': _enmascarar(data.get('telefono_contacto')),
            'datos': {k: v for k, v in datos.items() if k not in ('GHL_ID',)},
            'ya_estaba_asignado': data.get('code') == 'contacto-asignado',
            'contexto': _contexto_del_lead(contacto_id, campana_id),
        })


class AppLlamarView(APIView):
    """POST /api/v1/app/llamar/ {contacto_id, campana_id} -> click2call.

    El numero real NUNCA viaja al telefono: se manda el id del contacto y el
    servidor arma la llamada. Sirve tambien para la "doble llamada" (volver a
    marcarle al mismo lead antes de disposicionar).
    """
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)
    permission_classes = (IsAuthenticated,)
    http_method_names = ['post']

    def post(self, request):
        agente = _agente_de(request)
        if agente is None:
            return Response({'error': 'el usuario no es un agente'}, status=403)
        contacto_id = str(request.data.get('contacto_id') or '')
        campana_id = str(request.data.get('campana_id') or '')
        if not contacto_id or not campana_id:
            return Response({'error': 'contacto_id y campana_id son requeridos'}, status=400)

        from ominicontacto_app.models import AgenteEnContacto, Campana, Contacto
        from ominicontacto_app.services.click2call import Click2CallOriginator

        contacto = Contacto.objects.filter(pk=contacto_id).first()
        if contacto is None:
            return Response({'error': 'no se encontró el contacto'}, status=404)
        campana = Campana.objects.obtener_actuales().filter(pk=campana_id).first()
        if campana is None:
            return Response({'error': 'la campaña no está activa'}, status=400)

        # Misma regla que la consola web: sin calificar la última llamada no hay otra.
        if _ultima_llamada_sin_calificar(agente):
            return Response({'error': MSG_CALIFICAR_PRIMERO, 'codigo': 'calificar'}, status=409)
        # ¿Asterisk tiene ruta al teléfono? Si no, la llamada moriría en silencio.
        if not _telefono_alcanzable(agente):
            logger.warning('APP llamar: extension %s sin contacto en Asterisk (agente=%s)', agente.sip_extension, agente.id)
            return Response({'error': MSG_TELEFONO_RECONECTANDO, 'codigo': 'telefono'}, status=409)

        if campana.type == Campana.TYPE_PREVIEW:
            if not AgenteEnContacto.asignar_contacto(contacto.id, campana.pk, agente):
                # DOBLE DIAL DESPUES DE GUARDAR (decisión de producto): al guardar la disposicion
                # OML finaliza la reserva (AEC=FINALIZADO), pero la web deja volver a marcar al
                # mismo lead hasta que el agente pide el siguiente. Si la reserva la cerro ESTE
                # agente, se deja llamar; si el lead volvio a la cola (10 min sin llamar) o es
                # de otro, no.
                finalizado_por_mi = AgenteEnContacto.objects.filter(
                    contacto_id=contacto.id, campana_id=campana.pk, agente_id=agente.id,
                    estado=AgenteEnContacto.ESTADO_FINALIZADO).exists()
                if not finalizado_por_mi:
                    return Response({'error': 'Este lead ya no está reservado para vos (volvió a la cola). Pedí uno nuevo.',
                                     'codigo': 'perdido'}, status=409)

        try:
            # la central solo marca dígitos (un "+" hace morir la llamada sin aviso)
            Click2CallOriginator().call_originate(
                agente, str(campana.pk), str(campana.type), contacto_id,
                _solo_digitos(contacto.telefono), 'preview')
        except Exception as e:
            logger.error('APP llamar error agente=%s contacto=%s: %s', agente.id, contacto_id, e)
            return Response({'error': 'no se pudo iniciar la llamada'}, status=500)
        logger.info('APP llamar agente=%s contacto=%s campana=%s', agente.id, contacto_id, campana.pk)
        return Response({'status': 'OK'})


class AppLiberarView(APIView):
    """POST /api/v1/app/liberar/ {campana_id} -> devuelve el lead a la cola."""
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)
    permission_classes = (IsAuthenticated,)
    http_method_names = ['post']

    def post(self, request):
        agente = _agente_de(request)
        if agente is None:
            return Response({'error': 'el usuario no es un agente'}, status=403)
        from ominicontacto_app.models import AgenteEnContacto
        campana_id = request.data.get('campana_id')
        liberado, __ = AgenteEnContacto.liberar_contacto(agente.id, campana_id)
        return Response({'status': 'OK', 'liberado': bool(liberado)})


class AppOpcionesView(APIView):
    """GET /api/v1/app/opciones/?campana=1 -> disposiciones EN ORDEN ALFABETICO."""
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)
    permission_classes = (IsAuthenticated,)
    http_method_names = ['get']

    def get(self, request):
        from ominicontacto_app.models import OpcionCalificacion
        campana_id = request.query_params.get('campana')
        if not campana_id:
            return Response({'error': 'falta el parámetro campana'}, status=400)
        opciones = []
        for o in OpcionCalificacion.objects.filter(campana_id=campana_id).order_by('nombre'):
            subs = []
            try:
                subs = [s for s in (o.subcalificaciones or []) if s]
            except Exception:
                subs = []
            opciones.append({'id': o.id, 'nombre': o.nombre, 'tipo': o.tipo, 'subcalificaciones': subs})
        return Response({'opciones': opciones})


class AppCalificarView(APIView):
    """POST /api/v1/app/calificar/ {contacto_id, opcion_id, subcalificacion, observaciones}

    Crea o actualiza la calificacion igual que la web (solo puede haber UNA por
    contacto+campaña). Al guardar, el propio modelo finaliza la relacion
    agente-contacto y las señales sincronizan con GoHighLevel (etiqueta, nota,
    dueño de por vida y reinyeccion segun corresponda).
    """
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)
    permission_classes = (IsAuthenticated,)
    http_method_names = ['post']

    def post(self, request):
        agente = _agente_de(request)
        if agente is None:
            return Response({'error': 'el usuario no es un agente'}, status=403)
        contacto_id = request.data.get('contacto_id')
        opcion_id = request.data.get('opcion_id')
        if not contacto_id or not opcion_id:
            return Response({'error': 'contacto_id y opcion_id son requeridos'}, status=400)

        from ominicontacto_app.models import CalificacionCliente, OpcionCalificacion, Contacto
        opcion = OpcionCalificacion.objects.filter(pk=opcion_id).first()
        contacto = Contacto.objects.filter(pk=contacto_id).first()
        if opcion is None or contacto is None:
            return Response({'error': 'disposición o contacto inexistente'}, status=404)

        observaciones = (request.data.get('observaciones') or '').strip()
        subcalificacion = (request.data.get('subcalificacion') or '').strip()
        fam = _familia_calificacion(agente)
        call_id = str(request.data.get('call_id') or fam.get('CALLID') or '').strip()
        if call_id == 'NONE':
            call_id = ''

        calificacion = CalificacionCliente.objects.filter(
            contacto=contacto, opcion_calificacion__campana=opcion.campana).first()
        try:
            if calificacion is None:
                calificacion = CalificacionCliente(
                    contacto=contacto, opcion_calificacion=opcion, agente=agente,
                    observaciones=observaciones, subcalificacion=subcalificacion)
            else:
                calificacion.opcion_calificacion = opcion
                calificacion.agente = agente
                calificacion.observaciones = observaciones
                calificacion.subcalificacion = subcalificacion
            if call_id:
                calificacion.callid = call_id
            calificacion.save()
        except Exception as e:
            logger.error('APP calificar error agente=%s contacto=%s: %s', agente.id, contacto_id, e)
            return Response({'error': str(e)}, status=400)

        # Igual que la web al guardar: la llamada queda CALIFICADA (habilita la doble
        # marcación) y el agente sale de ACW.
        try:
            from api_app.services.calificacion_llamada import CalificacionLLamada
            call_data = _armar_call_data(call_id or calificacion.callid or '', opcion.campana, contacto)
            CalificacionLLamada().create_family(
                agente, call_data, _json.dumps(call_data),
                calificado=True, gestion=False, id_calificacion=calificacion.pk)
        except Exception as e:
            logger.error('APP marcar calificada agente=%s: %s', agente.id, e)
        try:
            from ominicontacto_app.services.asterisk.agent_activity import AgentActivityAmiManager
            if _estado_agente(agente)['acw']:
                AgentActivityAmiManager().unpause_agent(agente, '0', manage_connection=True)
        except Exception as e:
            logger.error('APP salir de ACW agente=%s: %s', agente.id, e)

        logger.info('APP calificar agente=%s contacto=%s opcion=%s callid=%s', agente.id, contacto_id, opcion.nombre, call_id)
        resp = {'status': 'OK', 'calificacion_id': calificacion.id,
                'contexto': _contexto_del_lead(contacto.id, opcion.campana_id)}
        resp.update(_estado_agente(agente))
        return Response(resp)


# ─────────────────────────────────────────────────────────────────────────────
# FLUJO DE LLAMADA IGUAL A LA CONSOLA WEB (2026-09-05, video de el operador)
#   colgar  -> ACW (pausa '0') + familia OML:CALIFICACION:LLAMADA con CALIFICADA=FALSE
#   llamar  -> si la última llamada no está calificada: 409 "No puede hacer una nueva
#              llamada hasta calificar la última llamada." (mismo aviso que la web)
#   guardar -> callid en la calificación, CALIFICADA=TRUE, sale de ACW -> doble marcación
# ─────────────────────────────────────────────────────────────────────────────
import json as _json
import re as _re

MSG_CALIFICAR_PRIMERO = 'No puede hacer una nueva llamada hasta calificar la última llamada.'


def _solo_digitos(tel):
    return _re.sub(r'\D', '', str(tel or ''))


def _familia_calificacion(agente):
    from api_app.services.calificacion_llamada import CalificacionLLamada
    try:
        return CalificacionLLamada().get_family(agente) or {}
    except Exception as e:
        logger.error('APP familia calificacion agente=%s: %s', agente.id, e)
        return {}


def _ultima_llamada_sin_calificar(agente):
    """Misma decisión que ApiStatusCalificacionLlamada (la que usa la consola)."""
    if not agente.grupo.obligar_calificacion:
        return False
    fam = _familia_calificacion(agente)
    if not fam or fam.get('CALIFICADA') == 'TRUE' or fam.get('GESTION') == 'TRUE':
        return False
    return bool(fam.get('CALLDATA') and fam.get('CALLDATA') != 'None')


def _armar_call_data(call_id, campana, contacto, call_type='4'):
    return {
        'id_campana': str(campana.id),
        'campana_type': str(campana.type),
        'telefono': contacto.telefono,
        'call_id': str(call_id),
        'call_type': str(call_type),
        'id_contacto': str(contacto.id),
        'rec_filename': 'click2Call-%s' % call_id,
        'call_wait_duration': '',
    }


def _estado_agente(agente):
    try:
        r = _redis_para_agente()
        d = r.hgetall('OML:AGENT:%s' % agente.id) or {}
    except Exception:
        d = {}
    status = d.get('STATUS') or ''
    return {
        'status': status,
        'acw': status.startswith('PAUSE-ACW'),
        'en_pausa': status.startswith('PAUSE'),
        'en_llamada': bool(d.get('CONTACT_NUMBER')) and 'ACW' not in status,
        'calificar_pendiente': _ultima_llamada_sin_calificar(agente),
    }


def _latido_app(agente):
    """La app avisa cada 15 s que sigue viva (2026-09-06). Si deja de avisar 3 min
    (la cerraron o iOS la durmió en segundo plano) sync_agent_pause.py la saca de las
    colas, como si cerrara el navegador. Devuelve True si eso ya pasó: la app entonces
    vuelve a entrar en colas sola."""
    try:
        r = _redis_para_agente()
        k = _REDIS_SESION % agente.id
        d = r.hgetall(k) or {}
        r.hset(k, 'ultimo_visto', int(_time.time()))
        return d.get('dispositivo') == 'app_dormida'
    except Exception:
        return False


def _redis_para_agente():
    import redis as redis_lib
    return redis_lib.Redis(host=settings.REDIS_HOSTNAME,
                           port=settings.CONSTANCE_REDIS_CONNECTION['port'],
                           decode_responses=True)


class AppEstadoView(APIView):
    """GET /api/v1/app/estado/ -> lo que muestra la barra superior de la consola."""
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)
    permission_classes = (IsAuthenticated,)
    http_method_names = ['get']

    def get(self, request):
        agente = _agente_de(request)
        if agente is None:
            return Response({'error': 'el usuario no es un agente'}, status=403)
        est = _estado_agente(agente)
        est['dormida'] = _latido_app(agente)
        return Response(est)


class AppLlamadaTerminadaView(APIView):
    """POST /api/v1/app/llamada_terminada/ {call_id, campana_id, contacto_id}

    Lo que hace la consola cuando cuelga: manda al agente a ACW y deja registrada
    la llamada como pendiente de calificar (Redis), que es lo que después bloquea
    una nueva llamada hasta que guarde la disposición.
    """
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)
    permission_classes = (IsAuthenticated,)
    http_method_names = ['post']

    def post(self, request):
        agente = _agente_de(request)
        if agente is None:
            return Response({'error': 'el usuario no es un agente'}, status=403)
        from ominicontacto_app.models import Campana, Contacto
        from ominicontacto_app.services.asterisk.agent_activity import AgentActivityAmiManager
        from api_app.services.calificacion_llamada import CalificacionLLamada

        call_id = str(request.data.get('call_id') or '').strip()
        campana = Campana.objects.filter(pk=request.data.get('campana_id')).first()
        contacto = Contacto.objects.filter(pk=request.data.get('contacto_id')).first()

        # 1) ACW, igual que la consola (pausa '0': NO libera el lead)
        try:
            AgentActivityAmiManager().pause_agent(agente, '0', manage_connection=True)
        except Exception as e:
            logger.error('APP ACW error agente=%s: %s', agente.id, e)

        # 2) llamada pendiente de calificar (solo si el grupo obliga a calificar)
        if call_id and campana and contacto and agente.grupo.obligar_calificacion:
            call_data = _armar_call_data(call_id, campana, contacto, request.data.get('call_type') or '4')
            try:
                CalificacionLLamada().create_family(
                    agente, call_data, _json.dumps(call_data),
                    calificado=False, gestion=False, id_calificacion=None)
            except Exception as e:
                logger.error('APP familia pendiente agente=%s: %s', agente.id, e)

        logger.info('APP llamada terminada agente=%s call_id=%s', agente.id, call_id)
        return Response(_estado_agente(agente))


class AppDespausarView(APIView):
    """POST /api/v1/app/despausar/ -> botón "Reanudar" de la consola (sale de ACW/pausa)."""
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)
    permission_classes = (IsAuthenticated,)
    http_method_names = ['post']

    def post(self, request):
        agente = _agente_de(request)
        if agente is None:
            return Response({'error': 'el usuario no es un agente'}, status=403)
        from ominicontacto_app.services.asterisk.agent_activity import AgentActivityAmiManager
        try:
            est = _estado_agente(agente)
            pause_id = '0'
            try:
                pause_id = _redis_para_agente().hget('OML:AGENT:%s' % agente.id, 'PAUSE_ID') or '0'
            except Exception:
                pass
            if est['en_pausa']:
                AgentActivityAmiManager().unpause_agent(agente, str(pause_id), manage_connection=True)
        except Exception as e:
            logger.error('APP despausar error agente=%s: %s', agente.id, e)
            return Response({'error': 'no se pudo reanudar'}, status=500)
        return Response(_estado_agente(agente))


# ─────────────────────────────────────────────────────────────────────────────
# SESIÓN ÚNICA computador / celular (decisión de producto): el último que entra gana.
#   app entra  -> la consola web recibe el MISMO aviso que usa OML cuando el usuario
#                 abre otra consola ("Se ha detectado un nuevo inicio de sesión…"):
#                 cuelga el teléfono, queda suspendida y se borra su sesión.
#   web entra  -> el token del celular deja de servir; la app lo nota en segundos,
#                 cuelga el teléfono y vuelve al login con un aviso.
# ─────────────────────────────────────────────────────────────────────────────
import time as _time

_REDIS_SESION = 'OML:DIALER:SESION:%s'


def _expulsar_consola_web(agente):
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        async_to_sync(get_channel_layer().group_send)(
            'agent-console-%s' % agente.user_id,
            {'type': 'broadcast', 'payload': {'type': 'logout'}})
    except Exception as e:
        logger.error('APP sesion unica: aviso a la consola agente=%s: %s', agente.id, e)
    try:
        agente.force_logout()  # borra la sesión Django del navegador
    except Exception as e:
        logger.error('APP sesion unica: force_logout agente=%s: %s', agente.id, e)


def _expulsar_app(agente):
    try:
        from rest_framework.authtoken.models import Token
        Token.objects.filter(user=agente.user).delete()
    except Exception as e:
        logger.error('APP sesion unica: token agente=%s: %s', agente.id, e)


def _marcar_dispositivo(agente, dispositivo):
    try:
        _redis_para_agente().hset(_REDIS_SESION % agente.id,
                                  mapping={'dispositivo': dispositivo, 'desde': int(_time.time()),
                                           'ultimo_visto': int(_time.time())})
    except Exception:
        pass


from api_app.views.agente import AgentLoginAsterisk as _AgentLoginAsteriskOML


class AgentLoginAsteriskSingleSession(_AgentLoginAsteriskOML):
    """La consola web llama a este endpoint al cargar. Si la petición viene por
    sesión de navegador (la app usa token), el celular queda fuera."""

    def post(self, request):
        if getattr(request, 'auth', None) is None:
            try:
                agente = request.user.get_agente_profile()
                if agente is not None:
                    _expulsar_app(agente)
                    _marcar_dispositivo(agente, 'web')
                    logger.info('APP sesion unica: entró la consola web, celular fuera (agente=%s)', agente.id)
            except Exception as e:
                logger.error('APP sesion unica (web): %s', e)
        return super().post(request)


# ── ¿La central tiene ruta al teléfono del agente? (2026-09-05) ──────────────
# Kamailio reenvía a Asterisk TODOS los REGISTER, incluido el "me desconecto" del
# otro dispositivo (mismo contacto compartido sip:<ext>@127.0.0.1:10060). Al cambiar
# de computador a celular queda una ventana de ~1 min sin ruta hasta el próximo
# refresco. Antes de marcar se comprueba, y si no hay ruta la app re-registra y reintenta.
MSG_TELEFONO_RECONECTANDO = 'El teléfono se está reconectando. Intentá de nuevo en unos segundos.'


def _telefono_alcanzable(agente):
    try:
        from ominicontacto_app.services.asterisk.asterisk_ami import AmiManagerClient
        m = AmiManagerClient()
        if m.connect():
            return True  # sin AMI no bloqueamos
        try:
            data, err = m._ami_manager('command', 'pjsip show aor %s' % agente.sip_extension)
        finally:
            m.disconnect()
        if err:
            return True
        return bool(_re.search(r'Contact:\s+%s/sip:' % agente.sip_extension, str(data)))
    except Exception as e:
        logger.error('APP alcanzable agente=%s: %s', agente.id, e)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# NOTAS DEL CRM (decisión de producto): "todas las notas de lo más reciente que pasó",
# tal cual están en GoHighLevel (las escribe el dialer: disposiciones y Whisper, o
# cualquiera en el CRM). Se piden aparte de la tarjeta para no frenarla, con caché
# de 60 s en Redis. Si GHL no responde o el lead es demo, se usan las del dialer.
# ─────────────────────────────────────────────────────────────────────────────
NOTAS_CACHE_SEG = 60
NOTAS_MAX = 10


def _notas_ghl(ghl_id):
    """Notas del contacto en GHL, de la más nueva a la más vieja. None si no se pudo consultar."""
    import requests as _rq
    from datetime import datetime as _dt
    from api_app.views import crm_dispositions as _d
    cfg = _d._env()
    token = cfg.get('GHL_API_TOKEN')
    if not token or not ghl_id or ghl_id.startswith('DEMO'):
        return None
    try:
        r = _rq.get('%s/contacts/%s/notes' % (_d.GHL_BASE, ghl_id), timeout=5,
                    headers={'Authorization': 'Bearer %s' % token, 'Version': _d.GHL_VERSION})
        if r.status_code != 200:
            logger.warning('APP notas GHL %s -> %s', ghl_id, r.status_code)
            return None
        notas = (r.json() or {}).get('notes') or []
    except Exception as e:
        logger.error('APP notas GHL %s: %s', ghl_id, e)
        return None
    salida = []
    for n in sorted(notas, key=lambda x: x.get('dateAdded') or '', reverse=True)[:NOTAS_MAX]:
        fecha = ''
        try:
            fecha = _fecha_miami(_dt.fromisoformat((n.get('dateAdded') or '').replace('Z', '+00:00')), larga=True)
        except Exception:
            pass
        salida.append({'texto': (n.get('body') or '').strip(), 'fecha': fecha, 'origen': 'crm'})
    return salida


def _notas_dialer(contacto_id, campana_id):
    ctx = _contexto_del_lead(contacto_id, campana_id)
    salida = []
    if ctx.get('nota_ia'):
        salida.append({'texto': ctx['nota_ia']['texto'], 'fecha': ctx['nota_ia']['fecha'], 'origen': 'dialer'})
    for h in ctx.get('historial') or []:
        texto = h['disposicion'] + (' — ' + h['agente'] if h.get('agente') else '')
        if h.get('observaciones'):
            texto += '\n' + h['observaciones']
        salida.append({'texto': texto, 'fecha': h.get('fecha_larga') or h.get('fecha'), 'origen': 'dialer'})
    return salida[:NOTAS_MAX]


class AppNotasView(APIView):
    """GET /api/v1/app/notas/?contacto_id=ID -> {notas:[{texto,fecha,origen}], fuente}"""
    authentication_classes = (SessionAuthentication, ExpiringTokenAuthentication)
    permission_classes = (IsAuthenticated,)
    http_method_names = ['get']

    def get(self, request):
        agente = _agente_de(request)
        if agente is None:
            return Response({'error': 'el usuario no es un agente'}, status=403)
        from ominicontacto_app.models import Contacto
        try:
            contacto_id = int(request.query_params.get('contacto_id') or 0)
        except ValueError:
            contacto_id = 0
        contacto = Contacto.objects.filter(pk=contacto_id).first()
        if contacto is None:
            return Response({'notas': [], 'fuente': 'ninguna'})
        campana_id = request.query_params.get('campana_id') or 1

        clave = 'OML:DIALER:NOTAS:%s' % contacto_id
        try:
            r = _redis_para_agente()
            cache = r.get(clave)
            if cache:
                return Response(_json.loads(cache))
        except Exception:
            r = None

        ghl_id = (contacto.id_externo or '').strip()
        notas = _notas_ghl(ghl_id)
        fuente = 'crm'
        if notas is None:          # GHL no disponible o lead demo
            notas, fuente = _notas_dialer(contacto_id, campana_id), 'dialer'
        elif not notas:            # el CRM no tiene nada: lo que sepa el dialer
            notas, fuente = _notas_dialer(contacto_id, campana_id), 'dialer'
        resp = {'notas': notas, 'fuente': fuente}
        try:
            if r is not None:
                r.setex(clave, NOTAS_CACHE_SEG, _json.dumps(resp))
        except Exception:
            pass
        return Response(resp)


def _precargar_notas(contacto_id, campana_id):
    """Al entregar el lead, las notas del CRM se traen en segundo plano y quedan en Redis:
    cuando la app las pide un instante después, salen de memoria (instantáneas)."""
    import threading

    def _tarea():
        try:
            from ominicontacto_app.models import Contacto
            c = Contacto.objects.filter(pk=contacto_id).first()
            if c is None:
                return
            notas = _notas_ghl((c.id_externo or '').strip())
            fuente = 'crm'
            if not notas:
                notas, fuente = _notas_dialer(contacto_id, campana_id), 'dialer'
            _redis_para_agente().setex('OML:DIALER:NOTAS:%s' % contacto_id, NOTAS_CACHE_SEG,
                                       _json.dumps({'notas': notas, 'fuente': fuente}))
        except Exception as e:
            logger.error('APP precarga notas %s: %s', contacto_id, e)

    threading.Thread(target=_tarea, daemon=True).start()
