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

import yaml
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import ai
from .board import sysfs_stream_cmd, test_connection
from .planner import Knowledge, PinResolver, plan
from .fetch import fetch_driver
from .validate import detect_from_board, validate_source
from .workspace import PERIPHERAL_TEMPLATE, Workspace, safe_name, set_yaml_fields

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
                  'release': _release(snap['system'].get('uname')) if snap and not snap['meta'].get('demo') else None,
                  'findings': snap['findings'] if snap else None},
        'source': source_info(),
        'ai': ai.config(WS),
        'problems': [p for p in [
            None if soc else f"SoC profile '{WS.cfg.get('soc')}' not found in knowledge/soc/",
            None if WS.ktree else 'kernel_src not set or missing: source-side checks are disabled',
            None if (WS.dts and WS.dts.ok) else 'board_dts not found: set it in workspace.yaml',
        ] if p],
    }


def _release(uname: str | None) -> str | None:
    m = re.search(r'\b(\d+\.\d+(?:\.\d+)?[\w.+-]*)', uname or '')
    return m.group(1) if m else None


def source_info() -> dict:
    """Kernel source state for the homepage, with plain-words checks (server/validate.py)."""
    kt, cfg = WS.ktree, WS.cfg
    snap = WS.snapshot()
    soc = KB.soc.get(cfg.get('soc'), {})
    v = validate_source(cfg.get('kernel_src'), cfg.get('board_dts'), soc, snap)
    out = {'kernel': kt.root if kt else None, 'configured': cfg.get('kernel_src'),
           'board_dts': WS.dts.rel(WS.dts.board_dts) if WS.dts else cfg.get('board_dts'),
           'ok': bool(WS.dts and WS.dts.ok) and v['ok'], 'tree_ok': bool(kt and kt.ok), 'checks': v['checks'],
           'warnings': [c['text'] + (' ' + c['fix'] if c.get('fix') else '') for c in v['checks'] if c['level'] != 'ok']}
    out.update(v['info'])
    # how to get the exact source the board runs, from the SoC profile + the board's uname -r
    rel = _release(snap['system'].get('uname')) if snap and not snap['meta'].get('demo') else None
    if rel and soc.get('kernel_repo'):
        m = re.match(r'(.+?)-g([0-9a-f]{7,40})$', rel)
        ver, commit = (m.group(1), m.group(2)) if m else (rel, None)
        tag = soc.get('kernel_tag', '{version}').format(version=ver)
        out['get_source'] = {'release': rel, 'commit': commit, 'tag': tag, 'repo': soc['kernel_repo'],
                             'cmd': f"git clone --depth 1 -b {tag} {soc['kernel_repo']} ~/{soc['kernel_repo'].rstrip('/').split('/')[-1]}-{ver}"}
    return out


@route('POST', '/api/source/detect')
def source_detect(q, body):
    """Find the kernel tree and board .dts that match the connected board (searches your home folder)."""
    soc = KB.soc.get(body.get('soc') or WS.cfg.get('soc'), {})
    roots = WS.cfg.get('source_search') or ['~']
    return detect_from_board(WS.snapshot(), soc, roots)


@route('POST', '/api/source/validate')
def source_validate(q, body):
    """Check kernel_src + board_dts from the setup form before saving them."""
    soc = KB.soc.get(body.get('soc') or WS.cfg.get('soc'), {})
    return validate_source(body.get('kernel_src'), body.get('board_dts'), soc, WS.snapshot())


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


@route('POST', '/api/config/fields')
def put_config_fields(q, body):
    allowed = {'soc', 'kernel_src', 'board_dts', 'board.transport', 'board.host', 'board.snapshot'}
    WS.set_cfg_fields({k: v for k, v in body.items() if k in allowed and v is not None})
    return {'ok': True}


# ---------------------------------------------------------------- homepage
@route('GET', '/api/overview')
def overview(q, body):
    """Everything the homepage shows: setup state, and each device's progress."""
    KB.reload()
    st = status(q, body)
    items = []
    for p in WS.peripherals():
        it = dict(p)
        form = {} if p.get('error') else WS.periph_form(p['id'])
        # still the untouched blank template? say so instead of planning nonsense
        it['unfilled'] = [k for k, blank in (('compatible', 'vendor,chip'), ('description', 'what it is and what you want from it'))
                          if form.get(k) == blank]
        if not p.get('error'):
            try:
                pl = plan(WS.periph_form(p['id']), KB, WS, WS.progress().get(p['id'], {}))
                cur = next((s for s in pl['steps'] if s['id'] == pl['current']), None)
                port = pl['summary'].get('port') or {}
                it.update({'total': len(pl['steps']),
                           'done': sum(s['result']['status'] == 'pass' for s in pl['steps']),
                           'next': cur and {'title': cur['title'], 'layer': cur['layer']},
                           'needs_driver': port.get('state') == 'missing' and not port.get('src_ok') and 'compatible' not in it['unfilled'],
                           'problems': [p['text'] for p in pl['problems']]})
            except Exception as e:   # one broken form must not break the homepage
                it['error'] = f'{type(e).__name__}: {e}'
        items.append(it)
    cfg = WS.cfg
    return {'status': st, 'items': items, 'ai': ai.config(WS),
            'config': {'soc': cfg.get('soc'), 'kernel_src': cfg.get('kernel_src'), 'board_dts': cfg.get('board_dts'),
                       'board': cfg.get('board') or {}},
            'socs': [{'id': k, 'title': v.get('title'), 'status': v.get('status')} for k, v in KB.soc.items()],
            'layers': KB.core['layers']}


# ---------------------------------------------------------------- add-a-device wizard
@route('GET', '/api/wizard/search')
def wizard_search(q, body):
    """Part name -> compatible strings in your kernel tree, with driver/binding status."""
    if not (WS.ktree and WS.ktree.ok):
        return {'items': [], 'note': 'kernel source not set, so nothing to search. You can still type the compatible string.'}
    items = WS.ktree.search_compatibles(q.get('q', ''))
    for it in items:
        it.update(WS.ktree.guess_kind(it['binding'], it['driver']))
    return {'items': items}


@route('GET', '/api/wizard/check')
def wizard_check(q, body):
    """One compatible string -> what the guide will do with it."""
    c = q.get('compatible', '').strip()
    if not c:
        return {}
    if not (WS.ktree and WS.ktree.ok):
        return {'compatible': c, 'state': 'unknown'}
    binding = WS.ktree.binding(c)
    driver = WS.ktree.driver(c)
    from .kernel import driver_requirements
    req = None
    if driver:
        with open(os.path.join(WS.ktree.root, driver['file']), errors='replace') as f:
            req = driver_requirements(f.read())
    guess = WS.ktree.guess_kind(binding and binding['file'], driver and driver['file'])
    bus = (req or {}).get('bus')
    if bus == 'i2c' and req and req.get('video_sensor'):
        bus = 'csi2'            # an I2C-controlled sensor with a video endpoint is a CSI camera
    elif bus == 'platform' and guess.get('subsystem') == 'alsa':
        bus = 'sai'             # codec without a control bus: only the I2S/SAI wires
    vendors = WS.ktree.vendor_prefixes()
    return {'compatible': c, 'vendor_known': (c.split(',')[0] in vendors) if (',' in c and vendors) else None,
            'binding': binding and {k: binding[k] for k in ('file', 'title', 'required')},
            'driver': driver and {k: driver.get(k) for k in ('file', 'name', 'config')},
            'state': 'ok' if driver and driver.get('config') else ('not_built' if driver else 'missing'),
            'guess': {'bus': bus if bus in KB.bus else None, 'subsystem': guess.get('subsystem')}}


@route('POST', '/api/peripheral/create')
def create_periph(q, body):
    """Create a peripheral form from the wizard's answers (writes a commented YAML file)."""
    pid = safe_name(body.get('id') or body.get('name') or 'new-device')
    if os.path.exists(WS.periph_path(pid)):
        raise ApiError(f'a device called {pid} already exists')
    f = body.get('form') or {}

    def parse(k, v):
        # the wizard sends text; turn "0x1a", "2", "{reset: ...}" or "GPIO_IO16, GPIO_IO26" into YAML values
        if not isinstance(v, str):
            return v
        v = v.strip()
        if k == 'pins' and v and v[0] not in '[{':
            return [x.strip() for x in v.split(',') if x.strip()]
        if re.fullmatch(r'0[xX][0-9a-fA-F]+', v):
            return v            # keep addresses in hex as typed
        try:
            out = yaml.safe_load(v)
        except yaml.YAMLError:
            return v
        return out if isinstance(out, (int, float, list, dict)) else v
    conn = {k: parse(k, v) for k, v in (f.get('connection') or {}).items() if v not in (None, '', [])}
    doc = {'name': pid, 'description': f.get('description') or '', 'bus': f.get('bus'),
           'subsystem': f.get('subsystem'), 'compatible': f.get('compatible')}
    if f.get('driver_src'):
        doc['driver_src'] = f['driver_src']
    for k in ('format', 'codec_ref', 'card_name'):
        if f.get(k):
            doc[k] = parse(k, f[k])
    doc['connection'] = conn
    if f.get('goal'):
        doc['goal'] = f['goal']
    text = ('# Created by the "Add a device" wizard. Edit freely: the guide re-reads this file.\n'
            '# Every field is explained in the form help (✎ Form in the workspace).\n'
            + yaml.safe_dump(doc, sort_keys=False, default_flow_style=None, allow_unicode=True))
    WS.save_periph(pid, text)
    return {'id': pid}


# ---------------------------------------------------------------- AI helper (optional)
@route('POST', '/api/ask')
def ask(q, body):
    pid = body['id']
    pl = plan(WS.periph_form(pid), KB, WS, WS.progress().get(pid, {}))
    step = next((s for s in pl['steps'] if s['id'] == body.get('step')), None)   # None = the device as a whole
    ctx = ai.build_context(WS, WS.periph_text(pid), pl, step)
    question = (body.get('question') or '').strip() or (
        'Explain this step for a beginner, and what usually goes wrong here.' if step else
        'Where am I in this bring-up, what is the next thing to do, and why?')
    if body.get('preview'):
        return {'context': ctx}
    try:
        res = ai.ask(WS, ctx, question, body.get('history'))
    except RuntimeError as e:
        raise ApiError(str(e))
    res['context'] = ctx
    return res


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


@route('POST', '/api/board/test')
def board_test(q, body):
    """Try reaching the board with the settings in the form (before saving them)."""
    cfg = {'transport': body.get('transport') or 'ssh', 'host': (body.get('host') or '').strip()}
    snap = (WS.cfg.get('board') or {}).get('snapshot') or ''
    cfg['snapshot'] = snap if os.path.isabs(snap) else os.path.join(WS.root, snap)
    return test_connection(cfg, WS.state_dir)


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


@route('POST', '/api/peripheral/fields')
def periph_fields(q, body):
    """Apply a suggested fix: set a few top-level form fields, keeping the file's comments."""
    allowed = {'compatible', 'bus', 'subsystem', 'driver_src', 'driver_url', 'driver_dir', 'config'}
    fields = {k: v for k, v in (body.get('fields') or {}).items() if k in allowed}
    WS.save_periph(body['id'], set_yaml_fields(WS.periph_text(body['id']), fields))
    return {'ok': True, 'set': fields}


@route('POST', '/api/driver/fetch')
def driver_fetch(q, body):
    """driver_src is a web link: download the .c into drivers-extra/ and point the form at it."""
    from .planner import build_context
    pid = body['id']
    form = WS.periph_form(pid)
    soc = KB.soc.get(WS.cfg.get('soc'), {})
    port = build_context(form, KB, soc, WS)['port']
    if not port.get('src_url'):
        raise ApiError('driver_src is not a web link')
    dirs = [port['dir']] + [s.get('driver_dir') for s in KB.subsystem.values() if s.get('driver_dir')]
    res = fetch_driver(port['src_url'], port['module'], dirs, os.path.join(WS.root, 'drivers-extra'))
    if not res['ok']:
        raise ApiError('could not find the driver file. Tried: ' + '; '.join(res['tried'][:6]) +
                       '. Open the link in a browser, find the .c file, and paste that file\'s link instead')
    rel = os.path.relpath(res['path'], WS.root)
    WS.save_periph(pid, set_yaml_fields(WS.periph_text(pid), {'driver_src': rel, 'driver_url': port['src_url']}))
    return {'saved': rel, 'from': res['url'], 'compatibles': res['compatibles']}


@route('DELETE', '/api/peripheral')
def delete_periph(q, body):
    return {'trashed': WS.delete_periph(q['id'])}


@route('POST', '/api/peripheral/restore')
def restore_periph(q, body):
    try:
        return {'id': WS.restore_periph(body['name'])}
    except FileExistsError as e:
        raise ApiError(str(e))


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
        pages = {'/': 'home.html', '': 'home.html', '/app': 'index.html', '/app/': 'index.html'}
        rel = pages.get(path) or path.lstrip('/').removeprefix('web/')
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
    def do_DELETE(self): self._dispatch('DELETE')


def main():
    global WS
    ap = argparse.ArgumentParser(description='Bring-up Studio')
    ap.add_argument('--workspace', default=os.path.dirname(HERE))
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--host', default='127.0.0.1')
    a = ap.parse_args()
    WS = Workspace(a.workspace)
    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        if e.errno == 98:
            raise SystemExit(f'Port {a.port} is already in use. Bring-up Studio is probably already running: '
                             f'open http://{a.host}:{a.port}\n'
                             f'Or stop it with:  pkill -f "m server.app"\n'
                             f'Or use another port:  ./run.sh --port {a.port + 1}')
        raise
    srv.daemon_threads = True
    print(f'Bring-up Studio on http://{a.host}:{a.port}  (workspace {WS.root})')
    if ai.config(WS)['enabled']:
        c = ai.config(WS)
        print(f"AI helper on: {c['label']}" + ('' if c['ready'] else ' (NOT READY: ' + ('claude CLI not found' if c['backend'] == 'claude-code' else 'set ANTHROPIC_API_KEY') + ')'))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
