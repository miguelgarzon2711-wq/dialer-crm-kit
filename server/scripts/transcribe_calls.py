#!/usr/bin/env python3
"""
Whisper Dialer (El Negocio): Transcripcion + resumen CRM -> nota GHL
/root/transcribe_calls.py
Cron: * 12-02 * * 1-6 /root/run_transcribe.sh >> /var/log/transcripciones.log 2>&1
Clon del de otro cliente adaptado a otro cliente (coaching de bienestar/peso/nutrición, mercado latino USA).

Rastreo por callid (no por disposición). Cache de transcripciones por callid.
Cada audio se transcribe UNA sola vez en su vida.
"""

import re
import os, sys, json, io, time
import psycopg2
import requests
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from datetime import datetime, timedelta, timezone

# ─── Credenciales ────────────────────────────────────────────────────────────
for line in open('/root/.env_dialer'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
GHL_API_KEY    = os.environ.get('GHL_TAGS_API_KEY', '') or os.environ.get('GHL_API_TOKEN', '')
GHL_READ_TOKEN = os.environ.get('GHL_READ_TOKEN', '')
GHL_LOCATION   = os.environ.get('GHL_LOCATION_ID', 'CRM_LOCATION_ID')

if not OPENAI_API_KEY:
    print("OPENAI_API_KEY no configurada — abortando")
    sys.exit(0)

# ─── Config ──────────────────────────────────────────────────────────────────
CAMPAIGN_OUTBOUND = 1     # (no usado: query cubre 1-5)
CAMPAIGN_INBOUND  = -1    # inbound aun no montado en el cliente
MIN_DURATION_SECS = 30
STATE_FILE        = '/var/log/transcripciones_procesadas.json'
CACHE_FILE        = '/var/log/transcripts_cache.json'
# Credenciales por entorno. Los valores por defecto son los que trae OMniLeads
# de fábrica: si tu instalación los cambió (deberías), poné los tuyos en
# /root/.env_dialer en vez de escribirlos acá.
DB = dict(host=os.environ.get('DB_HOST', '127.0.0.1'),
          port=int(os.environ.get('DB_PORT', 5432)),
          dbname=os.environ.get('DB_NAME', 'omnileads'),
          user=os.environ.get('DB_USER', 'omnileads'),
          password=os.environ.get('DB_PASSWORD', 'CAMBIAR_CLAVE_POSTGRES'))
# Zona horaria con la que se arman las fechas y las carpetas de grabaciones.
# OJO: tiene que coincidir con la del servidor donde corre Asterisk, no con la
# del operador ni con la del cliente. Si no coincide, el script busca las
# grabaciones en la carpeta del dia equivocado y no encuentra nada.
from zoneinfo import ZoneInfo
TZ = ZoneInfo(os.environ.get('DIALER_TZ', 'America/New_York'))

DIAS  = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
         'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']

def format_fecha(dt):
    bog = dt.astimezone(TZ)
    return f"{DIAS[bog.weekday()]} {bog.day} de {MESES[bog.month - 1]}"

# Disposiciones sin conversación real: no vale la pena gastar transcripción.
# Ajustá esta lista a las disposiciones de tu cliente.
SKIP_DISPOSITIONS = {'No contesto', 'Numero equivocado'}

# ============================================================================
#  PROMPTS — ESTO ES LO PRIMERO QUE TENÉS QUE ADAPTAR A TU CLIENTE
#
#  De estos dos textos depende la calidad de todas las notas. Un prompt genérico
#  produce notas genéricas e inútiles ("el cliente mostró interés"); un prompt
#  que nombra los datos concretos del negocio produce notas que el vendedor
#  realmente lee antes de volver a llamar.
#
#  Cómo escribirlo bien:
#   1. Decí qué vende el negocio y a quién, en una línea.
#   2. Listá los 5 o 6 datos que el vendedor NECESITA de una llamada.
#      Preguntáselo al cliente: "¿qué querés saber sí o sí de cada llamada?"
#      Sean cuales sean, ponelos como reglas que la IA nunca puede omitir.
#   3. Fijá el idioma, el tono y el largo máximo.
#   4. Definí qué pasa cuando la persona no dijo casi nada (una sola frase),
#      para que no invente relleno.
#
#  Probalo con 10 llamadas reales antes de dejarlo corriendo.
# ============================================================================

_NEGOCIO = "<QUÉ VENDE EL NEGOCIO Y A QUIÉN>"          # ej: "muebles a medida para casas"
_DATOS_CLAVE = """\
  - <DATO 1 que el vendedor necesita>
  - <DATO 2>
  - <DATO 3>
  - <DATO 4>"""

SYSTEM_PROMPT_OUTBOUND = f"""\
Eres el asistente de notas del equipo de ventas de un negocio que vende {_NEGOCIO}.

El vendedor hizo una llamada de ventas. Te doy la disposición, la duración y la
transcripción. Escribí la nota en español, en primera persona del vendedor
("Hablé con...", "me dijo que...", "quedamos en..."). Texto corrido, sin títulos
ni viñetas.

Si la persona NO compartió nada de su caso (solo "llamame luego", "estoy
ocupado", sin conversación real):
  Una sola frase con la razón, más la hora del callback si la hay.

Si SÍ compartió información, aunque sea poca:
  Párrafo 1 — Quién es y qué busca.
  Párrafo 2 — Contexto y próximo paso concreto (cita con día y hora, callback
  con hora, o qué quedó pendiente).

REGLAS QUE NUNCA SE IGNORAN — si aparece en la llamada, va sí o sí en la nota:
{_DATOS_CLAVE}
  - Cita o callback: siempre con día y hora exactos, al final.
  - Señales de urgencia o de que ya intentó con la competencia.

Máximo 130 palabras. Si cabe en menos sin perder información, usá menos.
No inventes nada que no esté en la transcripción.
"""

SYSTEM_PROMPT_INBOUND = f"""\
Eres el asistente de notas del equipo de ventas de un negocio que vende {_NEGOCIO}.

Esta fue una llamada ENTRANTE: la persona llamó por iniciativa propia, así que su
intención es más alta que en una llamada saliente. Anotalo brevemente.

Escribí la nota en español, en primera persona del vendedor ("Llamó preguntando
por...", "me contó que...", "quedamos en..."). Texto corrido, sin títulos ni viñetas.

Si solo preguntó algo puntual y colgó:
  Una sola frase con lo que preguntó.

Si compartió información sobre su caso:
  Párrafo 1 — Quién es y qué necesita.
  Párrafo 2 — Qué intentó antes, contexto y próximo paso.

REGLAS QUE NUNCA SE IGNORAN — si aparece en la llamada, va sí o sí en la nota:
{_DATOS_CLAVE}
  - Cómo llegó al negocio, si lo menciona.
  - Cita o callback: siempre con día y hora exactos, al final.

Máximo 130 palabras. No inventes nada que no esté en la transcripción.
"""


# ─── MinIO ───────────────────────────────────────────────────────────────────
def get_s3():
    return boto3.client('s3',
        endpoint_url=os.environ.get('STORAGE_ENDPOINT', 'http://127.0.0.1:9000'),
        aws_access_key_id=os.environ.get('STORAGE_ACCESS_KEY', 'CAMBIAR_USUARIO_STORAGE'),
        aws_secret_access_key=os.environ.get('STORAGE_SECRET_KEY', 'CAMBIAR_CLAVE_STORAGE'),
        config=Config(signature_version='s3v4'),
        region_name='us-east-1'
    )

def download_recording(s3, callid, archivo, rec_time, is_inbound=False):
    base_date = rec_time.astimezone(TZ).date()
    candidates = []
    if archivo and archivo not in ('', '-1'):
        candidates.append(archivo)
    if is_inbound:
        candidates.append(f'Inbound-{callid}')
    else:
        for prefix in ('previewCall', 'click2Call'):
            candidates.append(f'{prefix}-{callid}')

    for nombre in candidates:
        for delta in [0, -1, 1]:
            key = f'{(base_date + timedelta(days=delta)).strftime("%Y-%m-%d")}/{nombre}.mp3'
            try:
                resp = s3.get_object(Bucket='omnileads', Key=key)
                data = resp['Body'].read()
                return data, key, len(data) // 1024
            except ClientError as e:
                if e.response['Error']['Code'] in ('NoSuchKey', '404'):
                    continue
    return None, None, 0

# ─── Estado + Cache ──────────────────────────────────────────────────────────
def load_state():
    """
    Devuelve dict:
      processed_callids: set de callids ya procesados (con nota publicada)
      legacy_keys:        set de keys del formato viejo 'disp_id_modified'
                          (para evitar tormenta de re-procesamiento al migrar)
    """
    try:
        raw = json.load(open(STATE_FILE))
    except Exception:
        return {'processed_callids': set(), 'legacy_keys': set()}

    if isinstance(raw, list):
        # Formato VIEJO: lista de "disp_id_modified"
        return {'processed_callids': set(), 'legacy_keys': set(str(x) for x in raw)}

    return {
        'processed_callids': set(raw.get('processed_callids', [])),
        'legacy_keys':        set(raw.get('legacy_keys', [])),
    }

def save_state(state):
    # Limpieza: legacy_keys más viejos que 7 días ya no aplican (cutoff SQL = 48h)
    cutoff_iso = (datetime.now(tz=timezone.utc) - timedelta(days=7)).isoformat()
    legacy_clean = {k for k in state['legacy_keys']
                    if '_' in k and k.split('_', 1)[1] > cutoff_iso}
    json.dump({
        'processed_callids': sorted(state['processed_callids']),
        'legacy_keys':        sorted(legacy_clean),
    }, open(STATE_FILE, 'w'))

def load_cache():
    try:
        return json.load(open(CACHE_FILE))
    except Exception:
        return {}

def save_cache(cache):
    json.dump(cache, open(CACHE_FILE, 'w'), ensure_ascii=False)

# ─── GHL lookup por teléfono ─────────────────────────────────────────────────
def lookup_ghl_by_phone(phone):
    if not GHL_READ_TOKEN:
        return None
    try:
        resp = requests.get(
            'https://services.leadconnectorhq.com/contacts/',
            headers={
                'Authorization': f'Bearer {GHL_READ_TOKEN}',
                'Version': '2021-07-28'
            },
            params={'phone': phone, 'locationId': GHL_LOCATION},
            timeout=15
        )
        if resp.status_code == 200:
            contacts = resp.json().get('contacts', [])
            if contacts:
                return contacts[0].get('id')
    except Exception as e:
        print(f"  GHL lookup error: {e}")
    return None

# ─── DB ──────────────────────────────────────────────────────────────────────
def get_dispositions(conn):
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=48)
    cur = conn.cursor()
    cur.execute("""
        SELECT cc.id, cc.contacto_id, cc.fecha, cc.modified,
               oc.nombre AS disposition,
               c.id_externo AS ghl_id,
               c.telefono,
               u.first_name || ' ' || u.last_name AS agente,
               oc.campana_id
        FROM ominicontacto_app_calificacioncliente cc
        JOIN ominicontacto_app_opcioncalificacion oc ON oc.id = cc.opcion_calificacion_id
        JOIN ominicontacto_app_contacto c ON c.id = cc.contacto_id
        JOIN ominicontacto_app_agenteprofile ap ON ap.id = cc.agente_id
        JOIN ominicontacto_app_user u ON u.id = ap.user_id
        WHERE cc.modified >= %s
          AND oc.campana_id IN (1,2,3,4,5)
        ORDER BY cc.modified ASC
    """, (cutoff,))
    rows = cur.fetchall()
    cur.close()
    return rows

def find_recordings(conn, contacto_id, disposition_time, campaign_id):
    window_start = disposition_time - timedelta(minutes=90)
    window_end   = disposition_time + timedelta(minutes=5)
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT callid, archivo_grabacion, time, duracion_llamada
        FROM reportes_app_llamadalog
        WHERE contacto_id = %s
          AND campana_id = %s
          AND duracion_llamada > %s
          AND time BETWEEN %s AND %s
          AND event IN ('COMPLETEAGENT', 'COMPLETEOUTNUM')
        ORDER BY time ASC
    """, (contacto_id, campaign_id, MIN_DURATION_SECS, window_start, window_end))
    rows = cur.fetchall()
    cur.close()
    return rows

# ─── Whisper ─────────────────────────────────────────────────────────────────
def transcribe_audio(audio_data, filename):
    resp = requests.post(
        'https://api.openai.com/v1/audio/transcriptions',
        headers={'Authorization': f'Bearer {OPENAI_API_KEY}'},
        files={'file': (filename, io.BytesIO(audio_data), 'audio/mpeg')},
        data={
            'model': 'whisper-1',
            # sin 'language': Whisper detecta el idioma (los buzones pueden estar en ingles o espanol)
            'response_format': 'verbose_json',
            'prompt': 'Llamada de ventas de un concesionario de carros el negocio en Miami a clientes latinos. Financiamiento, aprobacion, down payment, trade-in, cita en el dealer. Sales call from a el negocio dealership in Miami; voicemail greetings may be in English: "leave a message after the tone".'
        },
        timeout=180
    )
    if resp.status_code == 200:
        j = resp.json()
        LAST_LANGUAGE['lang'] = (j.get('language') or '')[:12]
        return (j.get('text') or '').strip()
    print(f"  Whisper error {resp.status_code}: {resp.text[:150]}")
    return None


LAST_LANGUAGE = {'lang': ''}

# ─── AUDITORIA BUZON (decisión de producto) ─────────────────────────────────────
# Detecta si la "conversacion" fue en realidad un buzon de voz (espanol o ingles) y lo guarda en
# dialer_call_audit para el reporte (/root/call_report.py). Cero costo extra: usa la
# transcripcion que ya se hace. Si el vendedor puso una disposicion de conversacion sobre un
# buzon, queda una ALERTA en /var/log/voicemail_alerts.log.
BUZON_PATTERNS = [
    # espanol
    r'deje? (su|un|tu) mensaje', r'despu[eé]s del tono', r'despu[eé]s de la se[nñ]al', r'buz[oó]n de voz',
    r'no (est[aá]|se encuentra) disponible', r'la persona (a la )?que (usted )?(llama|marc[oó])',
    r'el n[uú]mero (que usted )?marc[oó]', r'grabe? (su|tu) mensaje', r'correo de voz', r'contestador',
    r'ha sido (transferid[oa]|redirigid[oa]) (a|al) (un )?(sistema|buz[oó]n|correo)', r'intente (m[aá]s tarde|de nuevo)',
    r'cuelgue', r'presione (la tecla )?(uno|1|numeral|gato)',
    # ingles
    r'leave (a|your) message', r'after the (tone|beep)', r'at the tone', r'voice ?mail', r'mailbox',
    r'(is|are) not available', r'the person you (are|\'re) (trying to reach|calling)', r'has a voice',
    r'google voice', r'please record your message', r'when you(\'re| are) finished', r'hang up',
    r'to leave a callback number', r'the number you (have )?dialed', r'is unavailable', r'cannot take your call',
    r'can\'t take your call', r'not able to (take|answer) your call', r'press (one|1|pound)',
]
BUZON_RE = re.compile('|'.join(BUZON_PATTERNS), re.IGNORECASE)


def es_buzon(text):
    """True si la transcripcion parece un buzon de voz (ES/EN)."""
    if not text:
        return False
    head = text[:600]          # el saludo del buzon esta al inicio
    hits = BUZON_RE.findall(head)
    return len(hits) >= 1


def ensure_audit_table(conn):
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS dialer_call_audit (
        callid text PRIMARY KEY, contacto_id integer, agente text, disposicion text, campana_id integer,
        duracion integer, es_buzon boolean, idioma text, alerta boolean DEFAULT false,
        transcript_inicio text, fecha timestamptz DEFAULT now())""")
    conn.commit(); cur.close()


def registrar_audit(conn, callid, contacto_id, agente, disposition, campana_id, duracion, text, idioma):
    buzon = es_buzon(text)
    alerta = bool(buzon and disposition not in SKIP_DISPOSITIONS)
    cur = conn.cursor()
    cur.execute("""INSERT INTO dialer_call_audit (callid, contacto_id, agente, disposicion, campana_id, duracion, es_buzon, idioma, alerta, transcript_inicio)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (callid) DO UPDATE SET disposicion=EXCLUDED.disposicion, es_buzon=EXCLUDED.es_buzon,
                       alerta=EXCLUDED.alerta, idioma=COALESCE(NULLIF(EXCLUDED.idioma,''), dialer_call_audit.idioma)""",
                (str(callid), contacto_id, agente, disposition, campana_id, duracion, buzon, idioma or '', alerta, (text or '')[:300]))
    conn.commit(); cur.close()
    if alerta:
        with open('/var/log/voicemail_alerts.log', 'a') as f:
            f.write("%s ALERTA BUZON: %s puso '%s' (contacto %s, campana %s, callid %s, %ss) sobre un buzon [%s]: %s\n" % (
                datetime.now(TZ).strftime('%F %T'), agente, disposition, contacto_id, campana_id, callid, duracion,
                idioma or '?', (text or '')[:120].replace('\n', ' ')))
        print(f"  ⚠ ALERTA BUZON: {agente} puso '{disposition}' sobre un buzon (callid {callid})")
    return buzon

def guardar_nota_local(conn, callids, contacto_id, nota):
    """La nota que escribe la IA queda TAMBIEN en el dialer (dialer_call_audit.nota_crm).
    La app movil la muestra al instante, sin tener que consultarle a GoHighLevel."""
    if not nota:
        return
    try:
        cur = conn.cursor()
        for cid in callids:
            cur.execute("UPDATE dialer_call_audit SET nota_crm=%s WHERE callid=%s", (nota, str(cid)))
        if contacto_id:
            cur.execute("""UPDATE dialer_call_audit SET nota_crm=%s
                            WHERE contacto_id=%s AND nota_crm IS NULL
                              AND fecha > now() - interval '2 hours'""", (nota, contacto_id))
        conn.commit(); cur.close()
    except Exception as e:
        print(f"  → No se pudo guardar la nota en el dialer: {e}")


# ─── GPT resumen CRM ─────────────────────────────────────────────────────────
def generate_crm_note(transcript, disposition, duracion_secs=0, is_inbound=False):
    system_prompt = SYSTEM_PROMPT_INBOUND if is_inbound else SYSTEM_PROMPT_OUTBOUND
    resp = requests.post(
        'https://api.openai.com/v1/chat/completions',
        headers={
            'Authorization': f'Bearer {OPENAI_API_KEY}',
            'Content-Type': 'application/json'
        },
        json={
            'model': 'gpt-4o-mini',
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': f'Disposición: {disposition}\nDuración: {duracion_secs} segundos ({duracion_secs//60} minutos)\n\nTranscripción:\n{transcript}'}
            ],
            'max_tokens': 200,
            'temperature': 0.3
        },
        timeout=30
    )
    if resp.status_code == 200:
        return resp.json()['choices'][0]['message']['content'].strip()
    print(f"  GPT error {resp.status_code}: {resp.text[:150]}")
    return None

# ─── GHL notas ───────────────────────────────────────────────────────────────
def post_ghl_note(ghl_id, body):
    resp = requests.post(
        f'https://services.leadconnectorhq.com/contacts/{ghl_id}/notes',
        headers={
            'Authorization': f'Bearer {GHL_API_KEY}',
            'Version': '2021-07-28',
            'Content-Type': 'application/json'
        },
        json={'body': body},
        timeout=15
    )
    return resp.status_code in (200, 201)

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    state = load_state()
    cache = load_cache()
    conn = psycopg2.connect(**DB)
    ensure_audit_table(conn)
    s3 = get_s3()

    rows = get_dispositions(conn)
    if not rows:
        conn.close()
        return

    ts = datetime.now(TZ).strftime('%Y-%m-%d %H:%M')
    print(f"{ts} — {len(rows)} disposición(es) en ventana de 48h")

    for (disp_id, contacto_id, fecha, modified, disposition, ghl_id, telefono, agente, campana_id) in rows:
        is_inbound = (campana_id == CAMPAIGN_INBOUND)
        tipo_str = "INBOUND" if is_inbound else "OUTBOUND"
        legacy_key = f"{disp_id}_{modified.isoformat()}"
        is_legacy = legacy_key in state['legacy_keys']

        # Skip disposiciones que no aportan
        if disposition in SKIP_DISPOSITIONS:
            continue

        # Buscar grabaciones en ventana de 90 min antes de la disposición
        recordings = find_recordings(conn, contacto_id, modified, campana_id)

        # ── Caso 1: sin grabaciones en MinIO/llamadalog ──
        if not recordings:
            if is_legacy:
                continue
            pseudo_callid = f"NOAUDIO_{disp_id}_{modified.isoformat()}"
            if pseudo_callid in state['processed_callids']:
                continue
            age_min = (datetime.now(tz=timezone.utc) - modified).total_seconds() / 60
            if age_min < 3:
                continue
            if not ghl_id and telefono:
                time.sleep(1.2)
                ghl_id = lookup_ghl_by_phone(telefono)
            if not ghl_id:
                state['processed_callids'].add(pseudo_callid)
                continue
            note_body = f"📞 {disposition} — {agente}"
            ok = post_ghl_note(ghl_id, note_body)
            print(f"  disp_id={disp_id} [{tipo_str}] '{disposition}' — Nota simple {'✓' if ok else 'ERROR'} (sin grabación útil)")
            if ok:
                state['processed_callids'].add(pseudo_callid)
            continue

        # ── Caso 2: hay grabaciones ──
        if is_legacy:
            for r in recordings:
                state['processed_callids'].add(str(r[0]))
            continue

        new_recordings = [r for r in recordings if str(r[0]) not in state['processed_callids']]
        if not new_recordings:
            continue

        age_min = (datetime.now(tz=timezone.utc) - modified).total_seconds() / 60
        if age_min < 3:
            print(f"  disp_id={disp_id} [{tipo_str}] '{disposition}' — Reciente ({age_min:.1f} min), espero próxima corrida")
            continue

        print(f"  disp_id={disp_id} [{tipo_str}] '{disposition}' — {len(new_recordings)} callid(s) nuevos")

        if not ghl_id and telefono:
            time.sleep(1.2)
            ghl_id = lookup_ghl_by_phone(telefono)
        if not ghl_id:
            print(f"  → Sin GHL_ID — marco callids procesados para no reintentar")
            for r in new_recordings:
                state['processed_callids'].add(str(r[0]))
            continue

        transcripts = []
        callids_transcritos = []
        for (callid_rec, archivo, rec_time, duracion) in new_recordings:
            callid_key = str(callid_rec)
            if callid_key in cache:
                transcripts.append(cache[callid_key])
                callids_transcritos.append(callid_key)
                registrar_audit(conn, callid_key, contacto_id, agente, disposition, campana_id, duracion, cache[callid_key], '')
                print(f"  → callid {callid_key} desde CACHE ({duracion}s)")
                continue
            audio_data, s3_key, size_kb = download_recording(s3, callid_rec, archivo, rec_time, is_inbound=is_inbound)
            if not audio_data:
                print(f"  → No encontrado en MinIO: callid={callid_rec}")
                continue
            print(f"  → Transcribiendo {s3_key} ({duracion}s, {size_kb}KB)...")
            fname = s3_key.split('/')[-1] if s3_key else f'{callid_rec}.mp3'
            text = transcribe_audio(audio_data, fname)
            if text:
                cache[callid_key] = text
                save_cache(cache)
                transcripts.append(text)
                callids_transcritos.append(callid_key)
                registrar_audit(conn, callid_key, contacto_id, agente, disposition, campana_id, duracion, text, LAST_LANGUAGE.get('lang', ''))
                print(f"  → Whisper OK + cacheado [{LAST_LANGUAGE.get('lang','?')}]: {text[:60]}...")

        if not transcripts:
            if age_min > 15:
                note_body = f"📞 {disposition} — {agente}"
                ok = post_ghl_note(ghl_id, note_body)
                print(f"  → Nota simple {'✓' if ok else 'ERROR'} (audios no descargables, {age_min:.0f} min)")
                if ok:
                    for r in new_recordings:
                        state['processed_callids'].add(str(r[0]))
            continue

        full_transcript = '\n---\n'.join(transcripts)
        total_duracion = sum(r[3] for r in new_recordings if r[3])
        crm_note = generate_crm_note(full_transcript, disposition, total_duracion, is_inbound=is_inbound)
        if not crm_note:
            continue

        if crm_note.strip().lower() == 'skip':
            print(f"  → GPT: skip — marco callids procesados")
            for cid in callids_transcritos:
                state['processed_callids'].add(cid)
            continue

        print(f"  → CRM: {crm_note[:80]}...")

        emoji = "📲" if is_inbound else "📞"
        note_body = f"{emoji} {disposition} — {agente}\n\n{crm_note}"
        ok = post_ghl_note(ghl_id, note_body)
        guardar_nota_local(conn, callids_transcritos, contacto_id, crm_note)
        if ok:
            for cid in callids_transcritos:
                state['processed_callids'].add(cid)
            print(f"  → Nota GHL publicada ✓ ({len(callids_transcritos)} callid(s) marcados)")
        else:
            print(f"  → Error publicando nota GHL — no marco callids, reintentaré")

    conn.close()
    save_state(state)
    save_cache(cache)

if __name__ == '__main__':
    main()
