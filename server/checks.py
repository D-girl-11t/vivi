"""Step checks: each returns {status: pass|fail|unknown|manual, detail: str}.

`unknown` means "can't tell yet", for example no board scan or no source tree. It never
means pass, so the guide keeps pointing at that step.
"""
from __future__ import annotations

import re


def R(status, detail=''):
    return {'status': status, 'detail': detail}


def evaluate(check: dict, ctx: dict, ws, manual_done: bool | None) -> dict:
    t = (check or {}).get('type', 'manual')
    fn = CHECKS.get(t)
    if not fn:
        return R('unknown', f'unknown check type {t}')
    try:
        res = fn(check, ctx, ws)
    except Exception as e:  # a broken check must never break the whole plan
        res = R('unknown', f'check error: {e}')
    if manual_done and res['status'] != 'pass':
        res = R('pass', 'marked done by you' + (f" (auto-check said: {res['detail']})" if res['detail'] else ''))
    elif res['status'] == 'manual' and manual_done is False:
        res = R('manual', res['detail'])
    return res


def _need_src(ws):
    return None if (ws.dts and ws.dts.ok) else R('unknown', 'source tree not configured (workspace.yaml: kernel_src, board_dts)')


def _snap(ws):
    return ws.snapshot()


# ------------------------------------------------------------- source checks
def c_source_text(ck, ctx, ws):
    if (e := _need_src(ws)):
        return e
    hits = ws.dts.search(ck['pattern'])
    return R('pass', f"found at {hits[0]['file']}:{hits[0]['line']}") if hits else R('fail', 'not found in the board include chain yet')


def c_source_status(ck, ctx, ws):
    if (e := _need_src(ws)):
        return e
    info = ws.dts.label_info(ck['label'])
    if not info['exists']:
        return R('fail', f"label &{ck['label']} not defined anywhere in the include chain. Check the SoC profile's controller naming")
    where = info['status_set_at']
    loc = f" ({where['file']}:{where['line']})" if where else ''
    if info['status'] == ck.get('expect', 'okay'):
        return R('pass', f"&{ck['label']} status = \"{info['status']}\"{loc}")
    return R('fail', f"&{ck['label']} status = \"{info['status']}\"{loc}; the board file must set \"okay\"")


def c_source_compatible(ck, ctx, ws):
    if (e := _need_src(ws)):
        return e
    if not ck.get('compatible'):
        return R('fail', 'no compatible in the peripheral form')
    hits = ws.dts.find_compatible(ck['compatible'])
    return R('pass', f"node {hits[0]['node']} at {hits[0]['file']}:{hits[0]['line']}") if hits \
        else R('fail', f"no node with compatible \"{ck['compatible']}\" yet")


def c_pins_muxed(ck, ctx, ws):
    if (e := _need_src(ws)):
        return e
    pins = ctx['pins']
    bad = [p['pad'] for p in pins if not p.get('mux_regex')]
    if bad:
        return R('fail', f"can't resolve a mux for {', '.join(bad)}; fix the pad/function in the form")
    missing = [p['pad'] for p in pins if not ws.dts.search(p['mux_regex'])]
    if missing:
        return R('fail', f"not muxed yet: {', '.join(missing)}")
    return R('pass', 'all pads muxed: ' + ', '.join(f"{p['pad']}→{p['function']}" for p in pins))


def c_pads_free(ck, ctx, ws):
    pins = ctx['pins']
    if not pins:
        return R('pass', 'no SoC pads involved')
    problems, notes = [], []
    for p in pins:
        if ctx['soc'].get('pins', {}).get('header') and not p.get('macro'):
            problems.append(f"{p['pad']}: no function matching {p['wanted']}")
    if ws.dts and ws.dts.ok:
        for p in pins:
            if not p.get('usage_regex'):
                continue
            for h in ws.dts.search(p['usage_regex']):
                own = p.get('mux_regex') and re.search(p['mux_regex'], h['text'])
                if own:
                    notes.append(f"{p['pad']} already muxed for {p['function']} in {h['node']}")
                else:
                    problems.append(f"{p['pad']} used by {h['node']} ({h['file']}:{h['line']}: {h['text']})")
    snap = _snap(ws)
    if snap:
        for line in snap.get('sections', {}).get('pinmux', '').split('\n'):
            for p in pins:
                if re.search(r'\b' + re.escape(p['pad']) + r'\b', line) and 'UNCLAIMED' not in line and \
                        ctx['dev']['label'] not in line:
                    notes.append(f"live: {line.strip()}")
    if problems:
        return R('fail', '; '.join(problems))
    return R('pass', ('pads free. ' + '; '.join(notes)) if notes else 'pads not used by anything else in the source')


# ------------------------------------------------------------- board checks
def c_kconfig(ck, ctx, ws):
    syms = [k['symbol'] for k in ctx['kconfig']]
    if not syms:
        return R('manual', 'no CONFIG_ symbol known. Add `config:` to the form, or confirm by hand')
    snap = _snap(ws)
    if not snap or not snap.get('kconfig'):
        return R('unknown', 'needs a board scan with /proc/config.gz (CONFIG_IKCONFIG_PROC=y)')
    kc = snap['kconfig']
    missing = [s for s in syms if s not in kc]
    have = ', '.join(f'{s}={kc[s]}' for s in syms if s in kc)
    if missing:
        return R('fail', f"not enabled: {', '.join(missing)}" + (f'; ok: {have}' if have else ''))
    return R('pass', have)


def _okay(n):
    return n['status'] in ('okay', 'ok')


def c_dt_live(ck, ctx, ws):
    snap = _snap(ws)
    if not snap or not snap.get('dt'):
        return R('unknown', 'needs a board scan')
    if ck.get('compatible'):
        nodes = [n for n in snap['dt'] if ck['compatible'] in n['compatible']]
        if any(_okay(n) for n in nodes):
            return R('pass', 'live DT has ' + ', '.join(n['path'] for n in nodes if _okay(n)))
        if nodes:
            return R('fail', 'node is in the live DT but disabled: ' + ', '.join(n['path'] for n in nodes))
        return R('fail', 'the running board does not have this node yet. Rebuild and install the .dtb, then reboot')
    if ck.get('label'):
        path = snap.get('symbols', {}).get(ck['label'])
        if not path:
            return R('unknown', f"can't map &{ck['label']} to a live path (dtb built without -@ symbols)")
        n = next((n for n in snap['dt'] if n['path'] == path), None)
        return R('pass', f'{path} is okay') if n and _okay(n) else R('fail', f'{path} is not enabled on the board')
    return R('manual', '')


def c_probe(ck, ctx, ws):
    snap = _snap(ws)
    if not snap or not snap.get('devices'):
        return R('unknown', 'needs a board scan')
    compats = [c for c in [ctx['dev'].get('compatible')] + list(ctx['bus'].get('probe_compatibles', [])) if c]
    msgs, ok = [], True
    for c in compats:
        nodes = [n for n in snap['dt'] if c in n['compatible'] and _okay(n)]
        if not nodes:
            ok = False; msgs.append(f'"{c}": no enabled node on the board'); continue
        for n in nodes:
            devs = [d for d in snap['devices'] if d['of_node'] == n['path']]
            bound = [d for d in devs if d['driver']]
            deferred = [l for l in snap['findings'].get('deferred', []) if n['path'].split('/')[-1].split('@')[0] in l]
            if bound:
                msgs.append(f'"{c}" → {bound[0]["bus"]}/{bound[0]["name"]} bound to {bound[0]["driver"]}')
            elif deferred:
                ok = False; msgs.append(f'"{c}" probe DEFERRED: {deferred[0]}')
            elif devs:
                ok = False; msgs.append(f'"{c}" device {devs[0]["name"]} exists but no driver bound. Check dmesg and kconfig')
            else:
                ok = False; msgs.append(f'"{c}" node {n["path"]} has no device. Is the parent bus/controller probed?')
    if not compats:
        drv = (ctx.get('driver') or {}).get('name') or ctx['dev'].get('driver')
        if not drv:
            return R('manual', 'no compatible or driver name in the form')
        bound = [d for d in snap['devices'] if d['driver'] == drv]
        if bound:
            return R('pass', f"{drv} bound to " + ', '.join(f"{d['bus']}/{d['name']}" for d in bound[:4]))
        return R('fail', f'no device bound to driver {drv} (module loaded? device plugged in?)')
    dm = ctx['driver'] and ctx['driver'].get('name')
    if not ok and dm:
        lines = [l for l in snap.get('sections', {}).get('dmesg', '').split('\n') if dm in l][-3:]
        if lines:
            msgs.append('dmesg: ' + ' | '.join(lines))
    return R('pass' if ok else 'fail', '; '.join(msgs))


def c_scan(ck, ctx, ws):
    if not ck.get('pattern'):
        return R('manual', 'confirm by hand (no pattern to look for)')
    snap = _snap(ws)
    if not snap:
        return R('unknown', 'needs a board scan')
    text = snap.get('sections', {}).get(ck['section'], '')
    m = re.search(ck['pattern'], text, re.I)
    if m:
        line = next((l for l in text.split('\n') if m.group(0) in l), m.group(0))
        return R('pass', line.strip()[:200])
    return R('fail', f"nothing matching '{ck['pattern']}' in {ck['section']}" + ('' if text.strip() else ' (section empty)'))


def c_board_cmd(ck, ctx, ws):
    if not ws.transport.live:
        return R('unknown', 'needs a live board (snapshot mode)')
    key = (ctx['dev']['label'], ck['cmd'])
    if not ck.get('_live'):   # only run on the board when you press Check; otherwise show the last result
        return ws.live_results.get(key) or R('unknown', 'press Check to run this on the board')
    rc, out, err = ws.transport.run(ck['cmd'], timeout=20)
    text = out + err
    if ck.get('expect'):
        ok = re.search(ck['expect'], text, re.I)
    else:
        ok = rc == 0
    res = R('pass' if ok else 'fail', text.strip()[-400:] or f'exit {rc}')
    ws.live_results[key] = res
    return res


def c_manual(ck, ctx, ws):
    return R('manual', ck.get('hint', 'mark it done when you have tried it'))


CHECKS = {
    'source_text': c_source_text, 'source_status': c_source_status,
    'source_compatible': c_source_compatible, 'pins_muxed': c_pins_muxed, 'pads_free': c_pads_free,
    'kconfig': c_kconfig, 'dt_live': c_dt_live, 'probe': c_probe, 'scan': c_scan,
    'board_cmd': c_board_cmd, 'manual': c_manual,
}
