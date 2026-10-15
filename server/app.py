"""HTTP server (stdlib only). Run:  python3 -m server.app  [--port 8765] [--workspace .]"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shlex
import subprocess
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .board import sysfs_stream_cmd
from .planner import Knowledge, PinResolver, plan
from .workspace import PERIPHERAL_TEMPLATE, Workspace, safe_name

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), 'web')
KB = Knowledge(os.path.join(os.path.dirname(HERE), 'knowledge'))
WS: Workspace = None  # set in main()

ROUTES = {}


def route(method, path):
    def deco(fn):
        ROUTES[(method, path)] = fn
        return fn
    return deco


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg); self.code = code


# ---------------------------------------------------------------- status & config
@route('GET', '/api/status')
def status(q, body):
    snap = WS.snapshot()
    soc = KB.soc.get(WS.cfg.get('soc'), {})
    return {
        'soc': {'id': soc.get('id'), 'title': soc.get('title'), 'status': soc.get('status')},
        'board': {'transport': WS.transport.describe(), 'live': WS.transport.live,
                  'scanned': snap['meta'] if snap else None,
                  'model': snap['system'].get('model') if snap else None,
                  'findings': snap['findings'] if snap else None},
        'source': {'kernel': WS.ktree.root if WS.ktree else None,
                   'board_dts': WS.dts.rel(WS.dts.board_dts) if WS.dts else None,
                   'ok': bool(WS.dts and WS.dts.ok)},
        'problems': [p for p in [
            None if soc else f"SoC profile '{WS.cfg.get('soc')}' not found in knowledge/soc/",
            None if WS.ktree else 'kernel_src not set or missing: source-side checks are disabled',
            None if (WS.dts and WS.dts.ok) else 'board_dts not found: set it in workspace.yaml',
        ] if p],
    }


@route('GET', '/api/config')
def get_config(q, body):
    return {'text': open(WS.cfg_path).read()}


@route('PUT', '/api/config')
def put_config(q, body):
    WS.save_cfg_text(body['text'])
    return {'ok': True}


@route('GET', '/api/knowledge')
def knowledge(q, body):
    KB.reload()
    return KB.catalog()


# ---------------------------------------------------------------- board
@route('POST', '/api/scan')
def do_scan(q, body):
    snap = WS.rescan()
    return {'ok': True, 'meta': snap['meta'], 'findings': snap['findings']}


@route('GET', '/api/board')
def board(q, body):
    snap = WS.snapshot()
    if not snap:
        return {'empty': True}
    out = {k: v for k, v in snap.items() if k != 'kconfig'}
    out['kconfig_count'] = len(snap.get('kconfig', {}))
    return out


@route('GET', '/api/board/kconfig')
def board_kconfig(q, body):
    snap = WS.snapshot() or {}
    needle = q.get('q', '').upper()
    return {k: v for k, v in snap.get('kconfig', {}).items() if needle in k}


@route('POST', '/api/snapshot/save')
def snapshot_save(q, body):
    return {'path': WS.save_snapshot(body.get('name') or 'board')}


# ---------------------------------------------------------------- source
@route('GET', '/api/source')
def source(q, body):
    if not (WS.dts and WS.dts.ok):
        return {'ok': False}
    return {'ok': True, 'board_dts': WS.dts.rel(WS.dts.board_dts), 'include_tree': WS.dts.include_tree,
            'blocks': WS.dts.overview()}


@route('POST', '/api/source/reload')
def source_reload(q, body):
    if WS.dts:
        WS.dts.reload()
    return {'ok': True}


@route('GET', '/api/file')
def file(q, body):
    p = WS.resolve_file(q['path'])
    with open(p, errors='replace') as f:
        return {'path': q['path'], 'text': f.read()}


@route('GET', '/api/pins')
def pins(q, body):
    """Pin explorer: every function a pad can carry, straight from the pinfunc header."""
    soc = KB.soc.get(WS.cfg.get('soc'), {})
    pr = PinResolver(soc, WS.ktree.root if WS.ktree else None)
    if not pr.header:
        return {'pads': {}, 'note': 'this SoC profile has no pin header; use the datasheet AF table'}
    needle = q.get('q', '').upper()
    pads: dict[str, list] = {}
    for m in re.finditer(soc['pins']['macro_regex'].replace('{pad}', r'(?P<pad>\w+?)'), pr.header):
        pad = m.group('pad')
        if needle and needle not in pad and needle not in m.group(0).upper():
            continue
        pads.setdefault(pad, []).append(m.groups()[-1])
    used = {}
    if WS.dts and WS.dts.ok:
        for pad in list(pads)[:80]:
            hits = WS.dts.search(soc['pins']['usage_regex'].replace('{pad}', re.escape(pad)))
            if hits:
                used[pad] = [f"{h['node']} ({h['text'].split()[0]})" for h in hits]
    return {'pads': dict(list(pads.items())[:80]), 'used': used}


@route('GET', '/api/examples')
def examples(q, body):
    if not (WS.dts and WS.dts.ok):
        return {'examples': []}
    soc = KB.soc.get(WS.cfg.get('soc'), {})
    if q.get('label'):
        return {'examples': WS.dts.examples_for_label(q['label'], soc.get('sibling_prefix', ''))}
    if q.get('compatible'):
        roots = [WS.dts.dts_dir] + [os.path.join(WS.ktree.root, d) for d in
                                    ('arch/arm64/boot/dts', 'arch/arm/boot/dts')]
        ex = WS.dts.examples_for_compatible(q['compatible'], [roots[0]])
        if len(ex) < 3:
            ex += [e for e in WS.dts.examples_for_compatible(q['compatible'], roots[1:], 6 - len(ex))
                   if e['file'] not in {x['file'] for x in ex}]
        return {'examples': ex}
    return {'examples': []}


@route('GET', '/api/lookup')
def lookup(q, body):
    """compatible -> binding, driver, CONFIG_ (handy while filling in a form)."""
    c = q.get('compatible', '')
    if not (WS.ktree and WS.ktree.ok and c):
        return {}
    return {'binding': WS.ktree.binding(c), 'driver': WS.ktree.driver(c, q.get('bus', ''))}


@route('POST', '/api/open')
def open_in_editor(q, body):
    p = WS.resolve_file(body['path'])
    tpl = WS.cfg.get('editor') or 'xdg-open {path}'
    cmd = tpl.format(path=shlex.quote(p), line=int(body.get('line') or 1))
    subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {'ok': True, 'cmd': cmd}


# ---------------------------------------------------------------- peripherals & plans
@route('GET', '/api/peripherals')
def peripherals(q, body):
    return {'items': WS.peripherals()}


@route('GET', '/api/peripheral')
def get_periph(q, body):
    return {'id': q['id'], 'text': WS.periph_text(q['id'])}


@route('PUT', '/api/peripheral')
def put_periph(q, body):
    WS.save_periph(q['id'], body['text'])
    return {'ok': True}


@route('POST', '/api/peripheral/new')
def new_periph(q, body):
    pid = safe_name(body.get('id') or 'new-device')
    if os.path.exists(WS.periph_path(pid)):
        raise ApiError(f'{pid} already exists')
    WS.save_periph(pid, PERIPHERAL_TEMPLATE.format(name=pid, buses=' | '.join(KB.bus),
                                                   subsystems=' | '.join(KB.subsystem)))
    return {'id': pid}


@route('GET', '/api/plan')
def get_plan(q, body):
    KB.reload()
    form = WS.periph_form(q['id'])
    return plan(form, KB, WS, WS.progress().get(q['id'], {}), live_step=q.get('live_step'))


@route('POST', '/api/manual')
def manual(q, body):
    WS.set_manual(body['id'], body['step'], body.get('done'))
    return {'ok': True}


# ---------------------------------------------------------------- HTTP plumbing
class Handler(BaseHTTPRequestHandler):
    server_version = 'BringupStudio/0.1'

    def log_message(self, fmt, *args):
        if os.environ.get('BS_DEBUG'):
            super().log_message(fmt, *args)

    def _send(self, code, data, ctype='application/json'):
        raw = data if isinstance(data, bytes) else json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def _dispatch(self, method):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if method == 'GET' and u.path == '/api/live':
            return self._live(q)
        if method == 'GET' and not u.path.startswith('/api/'):
            return self._static(u.path)
        fn = ROUTES.get((method, u.path))
        if not fn:
            return self._send(404, {'error': f'no route {method} {u.path}'})
        body = {}
        if method in ('POST', 'PUT'):
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}') if n else {}
        try:
            self._send(200, fn(q, body))
        except ApiError as e:
            self._send(e.code, {'error': str(e)})
        except FileNotFoundError as e:
            self._send(404, {'error': f'not found: {e}'})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {'error': f'{type(e).__name__}: {e}'})

    def _static(self, path):
        rel = 'index.html' if path in ('/', '') else path.lstrip('/').removeprefix('web/')
        p = os.path.realpath(os.path.join(WEB, rel))
        if not p.startswith(os.path.realpath(WEB)) or not os.path.isfile(p):
            return self._send(404, b'not found', 'text/plain')
        ctype = mimetypes.guess_type(p)[0] or 'application/octet-stream'
        self._send(200, open(p, 'rb').read(), ctype)

    def _live(self, q):
        """Server-sent events: stream sysfs values from the board."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        paths = [p for p in q.get('paths', '').split(',') if p.startswith(('/sys/', '/proc/', '/dev/shm/'))]
        if not WS.transport.live or not paths:
            self.wfile.write(b'event: error\ndata: needs a live board (ssh/local) and /sys or /proc paths\n\n')
            return
        proc = WS.transport.popen(sysfs_stream_cmd(paths, float(q.get('hz', 10))))
        try:
            for line in proc.stdout:
                self.wfile.write(f'data: {json.dumps(line.rstrip(chr(10)).split(chr(9)))}\n\n'.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.kill()

    def do_GET(self): self._dispatch('GET')
    def do_POST(self): self._dispatch('POST')
    def do_PUT(self): self._dispatch('PUT')


def main():
    global WS
    ap = argparse.ArgumentParser(description='Bring-up Studio')
    ap.add_argument('--workspace', default=os.path.dirname(HERE))
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--host', default='127.0.0.1')
    a = ap.parse_args()
    WS = Workspace(a.workspace)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f'Bring-up Studio on http://{a.host}:{a.port}  (workspace {WS.root})')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
