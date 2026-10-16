"""Planner: peripheral form + templates + SoC profile + source tree + board state -> steps.

    form (yours)  ─┐
    bus template  ─┤
    subsystem tpl ─┼─> context ─> rendered steps ─> each step's check evaluated
    soc profile   ─┤
    source tree   ─┤
    board scan    ─┘
"""
from __future__ import annotations

import copy
import os
import re

import jinja2
import yaml

from .kernel import driver_requirements

LAYER_ORDER = ['hardware', 'pinmux', 'controller', 'node', 'driver', 'kconfig', 'deploy',
               'probe', 'subsystem', 'userspace', 'app']
_EMPTY = '\x00'   # marker so empty substitutions don't leave blank lines in snippets


def _tabs(value, n=1):
    s = str(value or '').strip('\n')
    if not s.strip():
        return _EMPTY
    return '\n'.join(('\t' * n + l) if l.strip() else l for l in s.split('\n'))


def _clean_snippet(s: str) -> str:
    lines = [l.rstrip() for l in s.split('\n') if l.strip() != _EMPTY]
    return '\n'.join(lines).replace(_EMPTY, '').strip('\n')


ENV = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=False, keep_trailing_newline=False)
ENV.filters['tabs'] = _tabs


def render(tpl, ctx):
    if tpl is None:
        return None
    if isinstance(tpl, str):
        if '{{' not in tpl and '{%' not in tpl:
            return tpl
        return ENV.from_string(tpl).render(**ctx)
    if isinstance(tpl, list):
        return [render(t, ctx) for t in tpl]
    if isinstance(tpl, dict):
        return {k: render(v, ctx) for k, v in tpl.items()}
    return tpl


def truthy_expr(expr, ctx) -> bool:
    if expr is None:
        return True
    return bool(ENV.compile_expression(str(expr), undefined_to_none=True)(**ctx))


# ---------------------------------------------------------------- knowledge
class Knowledge:
    def __init__(self, root: str):
        self.root = root
        self.reload()

    def reload(self):
        def load_dir(d):
            out = {}
            p = os.path.join(self.root, d)
            for f in sorted(os.listdir(p)) if os.path.isdir(p) else []:
                if f.endswith('.yaml'):
                    with open(os.path.join(p, f)) as fh:
                        doc = yaml.safe_load(fh)
                    out[doc['id']] = doc
            return out
        with open(os.path.join(self.root, 'core.yaml')) as fh:
            self.core = yaml.safe_load(fh)
        self.soc = load_dir('soc')
        self.bus = load_dir('bus')
        self.subsystem = load_dir('subsystem')

    def catalog(self):
        pick = lambda d: [{'id': k, 'title': v.get('title'), 'summary': v.get('summary', ''),
                           'form_help': v.get('form_help', {}), 'status': v.get('status')} for k, v in d.items()]
        return {'soc': pick(self.soc), 'bus': pick(self.bus), 'subsystem': pick(self.subsystem),
                'layers': self.core['layers']}


# ---------------------------------------------------------------- pins
class PinResolver:
    def __init__(self, soc: dict, kernel_root: str | None):
        self.soc = soc
        self.pins_cfg = soc.get('pins', {})
        self.header = ''
        h = self.pins_cfg.get('header')
        if h and kernel_root and os.path.isfile(os.path.join(kernel_root, h)):
            with open(os.path.join(kernel_root, h), errors='replace') as f:
                self.header = f.read()

    def resolve(self, item, role: str, prefix: str | None, want_gpio: bool) -> dict:
        if isinstance(item, str):
            item = {'pad': item}
        pad = str(item['pad']).strip()
        wanted = item.get('function')
        p = {'pad': pad, 'function': None, 'macro': None, 'options': [], 'gpio': None,
             'wanted': wanted or (prefix + '*' if prefix and not want_gpio else 'GPIO'),
             'flags': item.get('flags'),
             'padconf': item.get('padconf') or self.soc.get('padconf', {}).get(role) or
             self.soc.get('padconf', {}).get('default', '')}
        gpio_rx = re.compile(self.pins_cfg.get('gpio_function_regex', r'^$'))

        if self.pins_cfg.get('macro_regex'):           # header-based SoCs (i.MX)
            rx = re.compile(self.pins_cfg['macro_regex'].replace('{pad}', re.escape(pad)))
            opts = [(m.group(1), m.group(2)) for m in rx.finditer(self.header)]
            p['options'] = [f for _, f in opts]
            choice = None
            if wanted:
                choice = next((o for o in opts if o[1] == wanted), None)
            elif want_gpio:
                choice = next((o for o in opts if gpio_rx.match(o[1])), None)
            elif prefix:
                cands = [o for o in opts if o[1].startswith(prefix)]
                choice = cands[0] if cands else None
            if choice:
                p['macro'], p['function'] = choice
                p['mux_regex'] = re.escape(choice[0]) + r'\b'
            # GPIO numbering is derivable from the pad's GPIO function even if muxed otherwise
            g = next((gpio_rx.match(f) for _, f in opts if gpio_rx.match(f)), None)
            if g:
                bank, line = g.group(1), int(g.group(2))
                p['gpio'] = {'bank': bank, 'line': line,
                             'controller': self.pins_cfg['gpio_controller'].format(bank=bank)}
            p['usage_regex'] = self.pins_cfg.get('usage_regex', '').replace('{pad}', re.escape(pad))
        else:                                          # port/pin + AF SoCs (STM32)
            m = re.match(self.pins_cfg.get('pad_regex', r'^$'), pad)
            if m:
                port, num = m.group(1), int(m.group(2))
                af = item.get('af')
                p.update({'port': port, 'num': num, 'af': None if want_gpio else af,
                          'function': 'GPIO' if want_gpio else (f'AF{af}' if af is not None else None)})
                p['macro'] = f"STM32_PINMUX('{port}', {num}, {'GPIO' if want_gpio or af is None else 'AF' + str(af)})"
                if not want_gpio and af is None:
                    p['macro'] = None
                p['gpio'] = {'bank': port.lower(), 'line': num,
                             'controller': self.pins_cfg['gpio_controller'].format(port_lower=port.lower(), port=port)}
                p['mux_regex'] = re.escape(p['macro']).replace(r'\ ', r'\s*') if p['macro'] else None
                p['usage_regex'] = self.pins_cfg['usage_regex'].format(port=port, num=num)
        return p


# ---------------------------------------------------------------- planner
def sanitize(name: str) -> str:
    s = re.sub(r'[^a-z0-9_]', '_', name.lower()).strip('_')
    return s if s and not s[0].isdigit() else 'dev_' + s


def build_context(form: dict, kb: Knowledge, soc: dict, ws) -> dict:
    dts, ktree = ws.dts, ws.ktree
    bus = kb.bus.get(form.get('bus'), {})
    conn = form.get('connection') or {}
    compat = form.get('compatible') or ''

    dev = copy.deepcopy(form)
    dev.setdefault('name', 'device')
    dev['label'] = sanitize(form.get('label') or dev['name'])   # DT label: underscores ok
    dev['slug'] = dev['label'].replace('_', '-')                  # DT node names: hyphens
    dev['node_name'] = form.get('node_name') or (compat.split(',')[-1] if compat else dev['slug'])
    dev['connection'] = conn
    dev['properties'] = (form.get('properties') or '').strip('\n')
    dev['card_name'] = form.get('card_name') or re.sub(r'[^A-Za-z0-9]', '', dev['name'])[:15] or 'card'
    reg = conn.get('reg')
    dev['reg_hex'] = ('0x%x' % int(str(reg), 0)) if reg is not None and str(reg).strip() else '0x?'

    # ---- controller
    ccfg = (soc.get('controllers') or {}).get(form.get('bus'), {})
    n = conn.get('controller')
    ctrl = {'n': n, 'label': None}
    if ccfg.get('label') and n is not None and form.get('bus') != 'gpio':
        ctrl['label'] = ccfg['label'].format(n=n)
    prefix = ccfg['function_prefix'].format(n=n) if ccfg.get('function_prefix') and n is not None else None

    # ---- pins
    pr = PinResolver(soc, ktree.root if ktree else None)
    role = bus.get('pin_role', 'default')
    want_gpio = form.get('bus') == 'gpio'
    pins = [pr.resolve(it, role, prefix, want_gpio) for it in (conn.get('pins') or [])]
    irq = None
    if conn.get('irq'):
        it = conn['irq'] if isinstance(conn['irq'], dict) else {'pad': conn['irq']}
        irq = pr.resolve(it, 'gpio', None, True)
        irq['type'] = it.get('type', 'IRQ_TYPE_LEVEL_LOW')
        pins.append(irq)
    gpio_ctrls = sorted({p['gpio']['controller'] for p in pins if p.get('gpio')})
    if want_gpio and gpio_ctrls:
        ctrl['label'] = gpio_ctrls[0]

    if dts and dts.ok and ctrl['label']:
        info = dts.label_info(ctrl['label'])
        ctrl.update({k: info[k] for k in ('exists', 'status', 'status_set_at', 'blocks')})
        for pf, nd, kind in dts.blocks_for(ctrl['label']):
            m = re.search(r'compatible\s*=\s*"([^"]+)"', pf.own_props(nd))
            if kind == 'define' and m:
                ctrl['compatible'] = m.group(1)

    # ---- kernel knowledge
    binding = ktree.binding(compat) if (ktree and ktree.ok and compat) else None
    driver = ktree.driver(compat, form.get('bus', '')) if (ktree and ktree.ok and compat) else None
    sub = kb.subsystem.get(form.get('subsystem') or 'other', {})
    port = driver_port_info(form, driver, compat, sub, ws)
    if not driver and port['src_ok']:
        driver = {'name': port['module'], 'config': port['config'], 'file': None, 'external': True}
    if not driver and form.get('driver'):
        driver = {'name': form['driver'], 'config': None}

    kconfig, seen = [], set()

    def add_k(sym, via, info=None):
        if not sym or sym in seen:
            return
        seen.add(sym)
        e = {'symbol': sym, 'via': via}
        if info:
            e.update({'file': info.get('file'), 'prompt': (info.get('kconfig') or {}).get('prompt'),
                      'depends': (info.get('kconfig') or {}).get('depends')})
        kconfig.append(e)

    extra = form.get('config') or []
    for sym in ([extra] if isinstance(extra, str) else extra):
        add_k(sym, 'set in form')
    if port['active'] and port['config']:
        add_k(port['config'], 'the driver you are adding')
    if driver and driver.get('config'):
        add_k(driver['config'], f'"{compat}"', driver)
    if ktree and ktree.ok:
        for c in ([ctrl.get('compatible')] if ctrl.get('compatible') else []) + list(bus.get('kconfig_compatibles', [])):
            d = ktree.driver(c)
            if d:
                add_k(d.get('config'), f'"{c}"', d)

    # ---- files / deploy
    files = {'board': dts.rel(dts.board_dts) if dts else None}
    if dts:
        arch_dts = os.path.join('arch', soc.get('arch', 'arm64'), 'boot', 'dts')
        rel = os.path.relpath(dts.board_dts, os.path.join(dts.kernel_root, arch_dts))
        files['dtb_target'] = os.path.splitext(rel)[0] + '.dtb'
        files['dtb_out'] = os.path.join(arch_dts, files['dtb_target'])

    # what the driver code reads from the DT (in-tree file, or the one you brought in)
    drv_text = None
    if driver and driver.get('file') and ktree:
        with open(os.path.join(ktree.root, driver['file']), errors='replace') as f:
            drv_text = f.read()
    elif port['src_ok']:
        with open(port['src_path'], errors='replace') as f:
            drv_text = f.read()
    driver_req = driver_requirements(drv_text) if drv_text else None

    # values a template may need, taken from the form first and the driver code second
    def pick(*vals):
        return next((v for v in vals if v not in (None, '', [], {})), None)
    req = driver_req or {}
    dev['supplies'] = pick(conn.get('supplies'), form.get('supplies')) or {}
    dev['supplies_list'] = list(dev['supplies']) or req.get('supply') or \
        [p[:-len('-supply')] for p in (binding or {}).get('required', []) if p.endswith('-supply')]
    dev['gpios'] = pick(conn.get('gpios'), form.get('gpios')) or {}
    dev['clock_hz'] = pick(conn.get('clock_hz'), req.get('clock_hz'))
    lanes = int(pick(conn.get('lanes'), (req.get('lanes') or [None])[0], 2))
    dev['lanes'], dev['lane_list'] = lanes, ' '.join(str(i) for i in range(1, lanes + 1))
    dev['link_freq'] = pick(conn.get('link_frequency'), (req.get('link_freq_values') or [None])[0])

    has_label = (lambda l: bool(l) and dts.label_info(l)['exists']) if (dts and dts.ok) else (lambda l: False)
    ctx = {'dev': dev, 'bus': bus, 'ctrl': ctrl, 'pins': pins, 'irq': irq, 'gpio_ctrls': gpio_ctrls,
           'soc': soc, 'binding': binding, 'driver': driver, 'kconfig': kconfig, 'files': files,
           'board': ws.cfg.get('board', {}), 'anchor': {}, 'port': port, 'driver_req': driver_req,
           'has_label': has_label}
    ctx['cam'] = camera_style(soc, has_label)
    ctx['cam'] = render(ctx['cam'], ctx)
    # bus templates can ask for extra source searches, e.g. "who else uses the CSI input?"
    ctx['found'] = {}
    for key, rx in (bus.get('context_searches') or {}).items():
        rx = render(rx, ctx)
        ctx['found'][key] = dts.owners(rx, exclude_label=dev['label']) if (dts and dts.ok and rx) else []
    sn = soc.get('snippets', {})
    ctx['snip'] = {
        'pinctrl_group': render(sn.get('pinctrl_group', ''), ctx) if pins else '',
        'pinctrl_props': render(sn.get('pinctrl_props', ''), ctx) if pins else '',
        'controller_extra': render((sn.get('controller_extra') or {}).get(form.get('bus'), ''), ctx),
    }
    ctx['deploy'] = render(soc.get('deploy', {}), ctx)
    return ctx


def _resolve_path(p: str, *bases: str) -> str:
    p = os.path.expanduser(str(p))
    if os.path.isabs(p):
        return p
    for b in bases:
        if b and os.path.exists(os.path.join(b, p)):
            return os.path.join(b, p)
    return os.path.join(bases[0], p) if bases and bases[0] else p


def driver_port_info(form: dict, driver: dict | None, compat: str, sub: dict, ws) -> dict:
    """Is the driver in the kernel tree? If not, where will it come from and where should it go?"""
    ktree = ws.ktree
    src = form.get('driver_src')
    src_url = src if src and re.match(r'https?://', str(src)) else None
    src_path = _resolve_path(src, ws.root, ktree.root if ktree else None) if src and not src_url else None
    src_ok = bool(src_path and os.path.isfile(src_path))
    module = os.path.splitext(os.path.basename(src_path))[0] if src_path else \
        (compat.split(',')[-1] if src_url and compat else None) or \
        (driver or {}).get('name') or (compat.split(',')[-1] if compat else 'mydriver')
    module = re.sub(r'[^A-Za-z0-9_]', '_', module)
    if driver and driver.get('file'):
        state = 'ok' if driver.get('config') else 'not_built'
    elif not (ktree and ktree.ok and compat):
        state = 'unknown'
    else:
        state = 'missing'
    cfg = form.get('config') if isinstance(form.get('config'), str) else None
    if not cfg and driver and driver.get('config'):
        cfg = driver['config']
    if not cfg:
        cfg = 'CONFIG_' + sub.get('kconfig_prefix', '') + module.upper()
    ddir = form.get('driver_dir') or (os.path.dirname(driver['file']) if driver and driver.get('file') else None) \
        or sub.get('driver_dir') or 'drivers/misc'
    src_text = ''
    if src_ok:
        with open(src_path, errors='replace') as f:
            src_text = f.read()
    return {
        'state': state, 'active': bool(src) or state in ('missing', 'not_built'),
        'src': src, 'src_url': src_url, 'src_path': src_path, 'src_ok': src_ok,
        'src_compat_ok': (not compat) or (f'"{compat}"' in src_text),
        'module': module, 'config': cfg, 'symbol': cfg.removeprefix('CONFIG_'),
        'dir': ddir, 'dest': f'{ddir}/{module}.c', 'object': f'{ddir}/{module}.o',
    }


def camera_style(soc: dict, has_label) -> dict:
    """SoC vendors change their camera DT layout between BSP releases; use whichever this tree has."""
    styles = (soc.get('camera') or {}).get('styles') or []
    for st in styles:
        if has_label(st.get('detect')):
            return dict(st, found=True)
    return dict(styles[0], found=False) if styles else {'found': False}


def step_templates(form: dict, kb: Knowledge) -> list[dict]:
    bus = kb.bus.get(form.get('bus'), {})
    sub = kb.subsystem.get(form.get('subsystem') or 'other', kb.subsystem.get('other', {}))
    out = list(kb.core.get('before_bus', []))
    for s in bus.get('steps', []):
        if 'use' in s:
            base = copy.deepcopy(kb.core['common'][s['use']])
            base.update({k: v for k, v in s.items() if k != 'use'})
            out.append(base)
        else:
            out.append(s)
    out += kb.core.get('driver_port', [])
    out += kb.core.get('after_bus', [])
    out += [dict(s, id='sub_' + s['id']) for s in sub.get('steps', [])]
    return out


def anchor_info(dts, anchor: str | None) -> dict:
    if not dts or not dts.ok or not anchor:
        return {}
    blk = dts.board_block(anchor)
    if blk:
        return {'exists': True, 'line': blk['line'], 'end_line': blk['end_line'], 'file': blk['file']}
    # new top-level block: suggest the end of the board file
    pf = dts.files[-1]
    return {'exists': False, 'line': pf.text.count('\n') + 1, 'file': dts.rel(pf.path)}


def form_problems(form: dict, kb: Knowledge, ws, ctx: dict) -> list[dict]:
    """Contradictions between the form, the kernel tree and the driver code.

    Each problem may carry `fix`: form fields that would resolve it, so the UI can offer a button.
    """
    out = []
    bus, compat, req = form.get('bus'), form.get('compatible') or '', ctx.get('driver_req') or {}
    if bus not in kb.bus:
        out.append({'text': f"unknown bus '{bus}'. Known: {', '.join(kb.bus)}"})
    if form.get('subsystem') and form['subsystem'] not in kb.subsystem:
        out.append({'text': f"unknown subsystem '{form['subsystem']}'. Known: {', '.join(kb.subsystem)}"})
    vendors = ws.ktree.vendor_prefixes() if ws.ktree and ws.ktree.ok else set()
    if ',' in compat and vendors and compat.split(',')[0] not in vendors:
        out.append({'text': f"vendor prefix '{compat.split(',')[0]}' is not registered in vendor-prefixes.yaml, "
                            "so check the chip maker's prefix (e.g. ovti = OmniVision, sony, bosch, ti, st)"})
    decl = req.get('compatibles') or []
    if decl and compat and compat not in decl:
        best = next((c for c in decl if c.split(',')[-1] == compat.split(',')[-1]), decl[0])
        out.append({'text': f'the driver declares "{best}", but the form says "{compat}". They must match exactly, '
                            'or the driver never binds', 'fix': {'compatible': best}})
    want = None
    if req.get('bus') == 'i2c' and req.get('video_sensor'):
        want = 'csi2' if 'csi2' in kb.bus else None
    elif req.get('bus') in ('i2c', 'spi') and req['bus'] in kb.bus and bus not in (req['bus'], 'csi2'):
        want = req['bus']
    if want and bus != want:
        why = 'an I2C-controlled image sensor (V4L2 subdevice), so its video goes over MIPI CSI-2' if want == 'csi2' else f'an {want.upper()} driver'
        out.append({'text': f"the driver code is {why}, but the form says bus: {bus}", 'fix': {'bus': want}})
    port = ctx.get('port') or {}
    if port.get('src_url'):
        out.append({'text': 'driver_src is a web link: the tool needs the .c file on disk to read it', 'action': 'fetch_driver'})
    return out


def plan(form: dict, kb: Knowledge, ws, manual: dict, live_step: str | None = None) -> dict:
    from .checks import evaluate
    soc = kb.soc.get(ws.cfg.get('soc'), {})
    ctx = build_context(form, kb, soc, ws)
    steps = []
    problems = form_problems(form, kb, ws, ctx)

    for tpl in step_templates(form, kb):
        try:
            if not truthy_expr(tpl.get('when'), ctx):
                continue
            anchor_s = render(tpl.get('anchor'), ctx)
            sctx = dict(ctx, anchor=anchor_info(ws.dts, anchor_s))
            command = render(tpl.get('command'), sctx)
            sctx['step_command'] = command
            st = {
                'id': tpl['id'], 'layer': tpl.get('layer', 'app'),
                'title': render(tpl.get('title', tpl['id']), sctx),
                'why': render(tpl.get('why', ''), sctx),
                'do': render(tpl.get('do', ''), sctx),
                'file': ctx['files'].get('board') if tpl.get('file') == 'board' else render(tpl.get('file'), sctx),
                'anchor': dict(sctx['anchor'], ref=anchor_s) if anchor_s else None,
                'snippet': _clean_snippet(render(tpl.get('snippet'), sctx) or ''),
                'command': command,
                'examples': render(tpl.get('examples'), sctx) or None,
                'check': render(tpl.get('check') or {'type': 'manual'}, sctx),
                'actions': [a for a in str(render(tpl.get('actions') or '', sctx)).split() if a],
            }
            if st['examples'] and not any(st['examples'].values()):
                st['examples'] = None
        except jinja2.TemplateError as e:
            st = {'id': tpl.get('id'), 'layer': tpl.get('layer', 'app'), 'title': f"template error in step {tpl.get('id')}",
                  'why': str(e), 'do': '', 'snippet': '', 'check': {'type': 'manual'}}
        if live_step == st['id'] and isinstance(st.get('check'), dict):
            st['check']['_live'] = True
        st['result'] = evaluate(st['check'], ctx, ws, manual.get(st['id']))
        if isinstance(st.get('check'), dict):
            st['check'].pop('_live', None)
        steps.append(st)

    steps.sort(key=lambda s: LAYER_ORDER.index(s['layer']) if s['layer'] in LAYER_ORDER else 99)
    current = next((s['id'] for s in steps if s['result']['status'] != 'pass'), None)
    return {
        'name': form.get('name'), 'steps': steps, 'current': current, 'problems': problems,
        'summary': {
            'label': ctx['dev']['label'], 'controller': ctx['ctrl'], 'pins': ctx['pins'],
            'binding': ctx['binding'], 'driver': ctx['driver'], 'kconfig': ctx['kconfig'],
            'driver_req': ctx['driver_req'], 'port': ctx['port'],
            'live': (kb.subsystem.get(form.get('subsystem') or '', {}) or {}).get('live'),
        },
    }
