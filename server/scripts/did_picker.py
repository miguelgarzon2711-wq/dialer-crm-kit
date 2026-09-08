#!/usr/bin/env python3
"""Dialer caller ID selector — sticky+cap+ramp. GET /pick?tel=<number> -> DID (11 dig)."""
import subprocess, re
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

def pick(tel):
    tel = re.sub(r'\D', '', tel)[-10:]
    if len(tel) < 10:
        return ''
    try:
        out = subprocess.run(
            ['docker', 'exec', 'prod-env-postgresql-1', 'psql', '-U', 'omnileads',
             '-d', 'omnileads', '-tAc', "SELECT pick_did('%s');" % tel],
            capture_output=True, text=True, timeout=4)
        return out.stdout.strip()
    except Exception:
        return ''


def inbound_campana(caller):
    """Returns the virtual DID (9000000N) of the lead's group inbound campana, or ''."""
    tel = re.sub(r'\D', '', caller)[-10:]
    if len(tel) < 10:
        return ''
    # PER-REP INBOUND STICKY: if the lead has an owner, the call goes
    # ONLY to that owner's personal queue (virtual DID 900001<id>); otherwise to the group.
    try:
        out = subprocess.run(
            ['docker', 'exec', 'prod-env-postgresql-1', 'psql', '-U', 'omnileads',
             '-d', 'omnileads', '-tAc',
             "SELECT r.telefono FROM dialer_lead_owner o "
             "JOIN ominicontacto_app_contacto c ON c.id=o.contacto_id "
             "JOIN configuracion_telefonia_app_rutaentrante r ON r.telefono='900001'||lpad(o.agente_id::text,2,'0') "
             "WHERE c.telefono='%s' ORDER BY o.since DESC LIMIT 1;" % tel],
            capture_output=True, text=True, timeout=4)
        did = out.stdout.strip()
        if did.startswith('900001'):
            return did
    except Exception:
        pass
    try:
        out = subprocess.run(
            ['docker', 'exec', 'prod-env-postgresql-1', 'psql', '-U', 'omnileads',
             '-d', 'omnileads', '-tAc',
             "SELECT aec.campana_id FROM ominicontacto_app_agenteencontacto aec "
             "JOIN ominicontacto_app_contacto c ON c.id=aec.contacto_id "
             "WHERE c.telefono='%s' AND aec.campana_id BETWEEN 1 AND 5 "
             "ORDER BY aec.modificado DESC LIMIT 1;" % tel],
            capture_output=True, text=True, timeout=4)
        camp = out.stdout.strip()
        return '9000000%s' % camp if camp in ('1','2','3','4','5') else ''
    except Exception:
        return ''


def missed(caller):
    """Inbound NOT answered -> POST /api/v1/dialer/missed_call/ (lead enters as Llamada Perdida, order 0)."""
    tel = re.sub(r'\D', '', caller)[-10:]
    if len(tel) < 10:
        return ''
    tok = ''
    try:
        for line in open('/root/.env_dialer'):
            if line.startswith('DIALER_API_TOKEN='):
                tok = line.strip().split('=', 1)[1]
    except Exception:
        pass
    try:
        import urllib.request, json as _j
        req = urllib.request.Request('https://dialer.example.com/api/v1/dialer/missed_call/',
                                     data=_j.dumps({'from': tel}).encode(),
                                     headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'},
                                     method='POST')
        r = urllib.request.urlopen(req, timeout=8)
        body = r.read().decode()[:200]
        with open('/var/log/missed_calls.log', 'a') as f:
            f.write('%s tel=%s %s\n' % (__import__('datetime').datetime.now().strftime('%F %T'), tel, body))
        return 'ok'
    except Exception as e:
        with open('/var/log/missed_calls.log', 'a') as f:
            f.write('%s tel=%s ERROR %s\n' % (__import__('datetime').datetime.now().strftime('%F %T'), tel, e))
        return ''

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == '/pick':
            did = pick(q.get('tel', [''])[0])
        elif u.path == '/inbound_campana':
            did = inbound_campana(q.get('from', [''])[0])
        elif u.path == '/missed':
            did = missed(q.get('from', [''])[0])
        else:
            did = ''
        body = did.encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

srv = ThreadingHTTPServer(('10.22.22.1', 8055), H)
srv.request_queue_size = 64
srv.serve_forever()
