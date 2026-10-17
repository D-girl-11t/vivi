"""Part finder: from what you bought to the compatible string Linux uses.

Two halves:
  find_in_kernel   offline, rule-based. Pulls part numbers out of whatever was typed
                   ("ICS-43434 breakout", "Sony IMX519"), searches the kernel tree for them,
                   falls back to the same chip family (ics43434 -> ics43432), and reads the
                   binding/Kconfig text that mentions them. Every hit says where it came from.
  ask_web          optional, needs the AI helper. Claude searches the web to say which chips
                   are on a product ("Arducam 16MP autofocus camera"). Its suggestions are then
                   checked against the kernel tree with find_in_kernel, never taken on trust.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from . import ai

# words that look like part numbers but aren't
_NOISE = {'i2c', 'i2s', 'spi', 'usb', 'mipi', 'csi', 'csi2', 'uart', 'gpio', 'pwm', 'adc', 'dac', 'hdmi', 'rgb',
          '1080p', '720p', '4k', 'v1', 'v2', 'v3', 'mp', '16mp', '12mp', '8mp', '5mp', '2mp', '3v3', '5v', '1v8'}


def part_tokens(text: str) -> list[str]:
    """'Sony IMX519 16MP (ICS-43434)' -> ['imx519', 'ics43434']: words with letters AND digits."""
    out = []
    for w in re.findall(r'[A-Za-z0-9][A-Za-z0-9_.+-]*[A-Za-z0-9]', text):
        if ',' in w:
            continue
        t = re.sub(r'[-_.]', '', w.lower())
        if len(t) >= 4 and re.search(r'[a-z]', t) and re.search(r'\d', t) and t not in _NOISE and t not in out:
            out.append(t)
    return out


def vendor_names(kt) -> dict[str, str]:
    """'invensense' -> 'invensense', 'sony corporation' -> 'sony' (from vendor-prefixes.yaml descriptions)."""
    if hasattr(kt, '_vendor_names'):
        return kt._vendor_names
    names = {}
    try:
        text = open(os.path.join(kt.root, 'Documentation/devicetree/bindings/vendor-prefixes.yaml'), errors='replace').read()
        for pre, desc in re.findall(r'^\s+"\^([A-Za-z0-9-]+),\.\*":\s*\n\s+description:\s*(.+)$', text, re.M):
            names[pre.lower()] = pre
            names[desc.strip().lower()] = pre
            names[desc.split()[0].strip(',.').lower()] = pre
    except OSError:
        pass
    kt._vendor_names = names
    return names


def _board_config(ws, sym: str | None):
    snap = ws.snapshot() if ws else None
    if not (sym and snap and snap.get('kconfig') is not None):
        return None
    return snap['kconfig'].get(sym, 'not set')


def _describe(ws, kt, hit: dict) -> dict:
    """Add driver/config/board state and bus/subsystem to one compatible hit."""
    hit.update(kt.guess_kind(hit.get('binding'), hit.get('driver')))
    drv = kt.driver(hit['compatible']) if hit.get('driver') else None
    if drv:
        hit['driver_line'] = drv['line']
        hit['config'] = drv['config']
        hit['board_config'] = _board_config(ws, drv['config'])
    vendors = kt.vendor_prefixes()
    pre = hit['compatible'].split(',')[0] if ',' in hit['compatible'] else None
    hit['vendor_known'] = (pre in vendors) if (pre and vendors) else None
    return hit


def _mentions(kt, token: str, limit: int = 8) -> list[dict]:
    """Binding docs and Kconfig help that name the part in prose ("ICS-43434", "IMX 519")."""
    letters, digits = re.match(r'([a-z]*)(.*)', token).groups()
    rx = re.escape(letters) + r'[ _-]?' + re.escape(digits) if letters and digits else re.escape(token)
    out = []
    for where, args in (('binding', ['--include=*.yaml', 'Documentation/devicetree/bindings']),
                        ('kconfig', ['--include=Kconfig*', 'drivers', 'sound'])):
        dirs = [a for a in args if not a.startswith('--') and os.path.isdir(os.path.join(kt.root, a))]
        if not dirs:
            continue
        try:
            r = subprocess.run(['grep', '-rIniE', *[a for a in args if a.startswith('--')], rx, *dirs],
                               cwd=kt.root, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in r.stdout.splitlines()[:40]:
            f, ln, txt = (line.split(':', 2) + ['', ''])[:3]
            if any(m['file'] == f for m in out) or re.match(r'\s*(imply|select|depends|default)\b', txt):
                continue
            m = {'where': where, 'file': f, 'line': int(ln or 1), 'text': txt.strip()[:160]}
            if where == 'binding':     # the compatibles this binding accepts
                try:
                    btxt = open(os.path.join(kt.root, f), errors='replace').read()
                    m['compatibles'] = sorted(set(re.findall(r'(?:- |const: |enum: \[)"?([a-z0-9-]+,[A-Za-z0-9._+-]+)', btxt)))[:8]
                except OSError:
                    pass
            elif where == 'kconfig':    # which config option this help text belongs to
                try:
                    ktxt = open(os.path.join(kt.root, f), errors='replace').read().split('\n')
                    sym = next((re.match(r'\s*(?:menu)?config\s+(\w+)', ktxt[i]).group(1) for i in range(int(ln) - 1, -1, -1)
                                if re.match(r'\s*(?:menu)?config\s+\w+', ktxt[i])), None)
                    m['config'] = 'CONFIG_' + sym if sym else None
                except (OSError, ValueError):
                    pass
            out.append(m)
            if len(out) >= limit:
                return out
    return out


def find_in_kernel(ws, text: str) -> dict:
    kt = ws.ktree if ws else None
    if not (kt and kt.ok):
        return {'error': 'Kernel source is not set up, so there is nothing to search. Set it on the home page (Setup → Kernel source).'}
    text = text.strip()
    if not text:
        return {'tokens': [], 'parts': []}
    # a full compatible string typed in: check it directly
    exact = re.fullmatch(r'\s*([a-z0-9-]+,[A-Za-z0-9._+-]+)\s*', text)
    tokens = [exact.group(1)] if exact else part_tokens(text)
    if not tokens:   # nothing that looks like a part number: maybe it's a vendor or plain word
        tokens = [w.lower() for w in re.findall(r'[A-Za-z0-9]{4,}', text) if w.lower() not in _NOISE][:3]
    lowered = text.lower()
    vnames = vendor_names(kt)
    vendor_hint = next((p for n, p in sorted(vnames.items(), key=lambda x: -len(x[0])) if len(n) > 3 and re.search(r'\b' + re.escape(n) + r'\b', lowered)), None)
    parts = []
    for tok in tokens[:4]:
        hits = kt.search_compatibles(tok.split(',')[-1], limit=10) if not exact else \
            [h for h in kt.search_compatibles(tok.split(',')[-1], limit=20) if h['compatible'] == tok] or \
            [{'compatible': tok, 'binding': None, 'driver': None}]
        kind, family = 'exact', None
        if not hits or not any(h.get('driver') or h.get('binding') for h in hits):
            # same family: drop trailing characters until something matches (ics43434 -> ics4343 -> ics43432)
            for n in range(len(tok) - 1, max(3, len(re.match(r'[a-z]*', tok).group(0)) + 1), -1):
                # a sibling has a part number of the same length (ics43432 for ics43434), not a shorter
                # unrelated one (imx51, the old SoC, is not a relative of the imx519 sensor)
                core = lambda h: re.match(r'[a-z0-9]*', h['compatible'].split(',')[-1].lower()).group(0)
                fam = [h for h in kt.search_compatibles(tok[:n], limit=30)
                       if len(core(h)) >= len(tok) and core(h).startswith(tok[:n]) and (h.get('driver') or h.get('binding'))][:8]
                if fam:
                    hits, kind, family = fam, 'family', tok[:n]
                    break
        if vendor_hint:     # put the maker the user named first
            hits.sort(key=lambda h: not h['compatible'].startswith(vendor_hint + ','))
        parts.append({'token': tok, 'kind': kind if hits else 'none', 'family': family,
                      'hits': [_describe(ws, kt, dict(h)) for h in hits[:8]],
                      'mentions': _mentions(kt, tok.split(',')[-1])})
    return {'tokens': tokens, 'vendor_hint': vendor_hint, 'parts': parts}


WEB_SYSTEM = """You identify the chips on electronics products for embedded Linux bring-up.
Use web search to find the product page, datasheet or the vendor's Linux driver. Then answer with
ONLY a JSON object, no prose, in this shape:
{"product": "<what it is>",
 "chips": [{"part": "<chip part number as the maker writes it, e.g. IMX519>",
            "maker": "<chip manufacturer>",
            "role": "<what it does on this product, e.g. image sensor, autofocus motor driver>",
            "bus": "<i2c|spi|i2s|mipi-csi2|usb|uart|gpio|pwm|other>",
            "compatible_guess": "<vendor,part as a Linux device-tree compatible, or empty if unsure>",
            "linux_driver": "<where a Linux driver for it lives if you found one (mainline path or a URL), else empty>",
            "sources": ["<url>", "..."]}],
 "notes": "<one or two sentences: anything the user must check, e.g. module revisions with a different chip>"}
List every chip Linux needs a driver for (sensor, focus motor, codec, PMIC...), most important first.
Never invent a part number: if the sources don't say, leave it out and say so in notes."""


def ask_web(ws, product: str) -> dict:
    cfg = ai.config(ws)
    if not cfg['enabled']:
        raise RuntimeError('The AI helper is off. Turn it on in ⚙ with `ai: {enabled: true}`.')
    if cfg['backend'] != 'claude-code' or not cfg['cli']:
        raise RuntimeError('Web lookup needs the Claude Code backend (ai.backend: claude-code) and the `claude` CLI.')
    cmd = [cfg['cli'], '-p', '--output-format', 'json', '--no-session-persistence', '--strict-mcp-config',
           '--tools', 'WebSearch', 'WebFetch', '--allowedTools', 'WebSearch', 'WebFetch', '--system-prompt', WEB_SYSTEM]
    if cfg['model']:
        cmd += ['--model', cfg['model']]
    try:
        r = subprocess.run(cmd, input=f'Product: {product}', capture_output=True, text=True, timeout=300, cwd=ws.state_dir)
    except subprocess.TimeoutExpired:
        raise RuntimeError('the web lookup took more than 5 minutes')
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f'Claude Code failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[:300]}')
    if out.get('is_error'):
        raise RuntimeError(f"Claude Code error: {str(out.get('result'))[:300]}")
    text = out.get('result', '')
    m = re.search(r'\{.*\}', text, re.S)
    try:
        found = json.loads(m.group(0)) if m else None
    except ValueError:
        found = None
    if not isinstance(found, dict):
        return {'raw': text, 'chips': [], 'cost_usd': out.get('total_cost_usd')}
    # check every suggestion against the kernel tree: the web says what the chip is, the tree says what Linux calls it
    for c in found.get('chips') or []:
        q = (c.get('compatible_guess') or '').strip() or c.get('part', '')
        c['kernel'] = find_in_kernel(ws, q) if q else None
        if c['kernel'] and not c['kernel'].get('error') and c.get('compatible_guess') and \
                not any(h.get('driver') or h.get('binding') for p in c['kernel']['parts'] for h in p['hits']):
            c['kernel'] = find_in_kernel(ws, c.get('part', ''))     # guess unknown to this tree: search the part number
    found['cost_usd'] = out.get('total_cost_usd')
    return found
