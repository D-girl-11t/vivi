"""Optional AI helper: ask Claude about the current step, with the tool's evidence attached.

Off unless workspace.yaml has `ai: {enabled: true}`. The API key is read from the
ANTHROPIC_API_KEY environment variable and is never written to disk. Every answer
comes back with the exact context that was sent, so nothing leaves the machine unseen.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

API_URL = 'https://api.anthropic.com/v1/messages'
DEFAULT_MODEL = 'claude-sonnet-5-5'

SYSTEM = """You are a patient embedded-Linux bring-up mentor inside "Bring-up Studio".
The user is learning. They are bringing up one peripheral on a real board, step by step.
You get the current step plus evidence the tool collected: the peripheral form, the generated
snippet, the check result, what the driver code asks for, the board's dmesg and probe state,
and the relevant block of their device-tree source.

Rules:
- Ground every claim in the evidence given. If something is not in the evidence, say what
  command or file would show it, instead of guessing.
- Never invent DT properties, Kconfig symbols or pin names. If you suggest one, say where it
  comes from (binding, driver source, SoC header) so the user can verify it.
- Explain *why*, briefly, then give the concrete next action. Prefer short numbered steps.
- For driver porting, point at the exact API change and the minimal fix.
- Keep it under about 350 words unless the user asks for more. Use Markdown with fenced code."""


def config(ws) -> dict:
    a = ws.cfg.get('ai') or {}
    return {'enabled': bool(a.get('enabled')), 'model': a.get('model') or DEFAULT_MODEL,
            'has_key': bool(os.environ.get('ANTHROPIC_API_KEY'))}


def _clip(s, n):
    s = s or ''
    return s if len(s) <= n else s[:n] + f'\n...[{len(s) - n} more chars cut]'


def build_context(ws, form_text: str, plan: dict, step: dict | None) -> str:
    sm = plan.get('summary', {})
    parts = [f"SoC profile: {ws.cfg.get('soc')}",
             f"Peripheral form (YAML):\n```yaml\n{_clip(form_text, 3000)}\n```"]
    drv = sm.get('driver') or {}
    parts.append('Driver: ' + (f"{drv.get('name')} ({drv.get('file') or 'external'}, {drv.get('config')})" if drv else 'none found'))
    if sm.get('port') and sm['port'].get('active'):
        p = sm['port']
        parts.append(f"Driver porting state: {p['state']}; source {p.get('src')}; target {p.get('dest')} ({p.get('config')})")
    if sm.get('driver_req'):
        r = {k: v for k, v in sm['driver_req'].items() if v}
        parts.append('What the driver code looks up:\n' + json.dumps(r, indent=1))
    if sm.get('binding'):
        b = sm['binding']
        parts.append(f"Binding {b.get('file')}: required {b.get('required')}")
    parts.append('All steps and their check results:\n' + '\n'.join(
        f"- [{s['result']['status']}] ({s['layer']}) {s['title']}: {s['result']['detail'][:200]}" for s in plan.get('steps', [])))
    if step:
        parts.append(f"CURRENT STEP: {step['title']} (layer {step['layer']})\nWhy (tool text):\n{_clip(step.get('why'), 1500)}\n"
                     f"Do (tool text):\n{_clip(step.get('do'), 1500)}\n"
                     + (f"Suggested change:\n```\n{_clip(step.get('snippet'), 2500)}\n```\n" if step.get('snippet') else '')
                     + f"Check result: {step['result']['status']}: {step['result']['detail']}")
        if ws.dts and ws.dts.ok and step.get('anchor') and step['anchor'].get('ref'):
            blk = ws.dts.board_block(step['anchor']['ref'])
            if blk:
                parts.append(f"Current board-file block {step['anchor']['ref']} ({blk['file']}:{blk['line']}):\n```dts\n{_clip(blk['text'], 3000)}\n```")
    snap = ws.snapshot()
    if snap:
        f = snap.get('findings', {})
        meta = snap.get('meta', {})
        parts.append(f"Board scan ({'DEMO data, not a real board' if meta.get('demo') else meta.get('time')}): "
                     f"{snap.get('system', {}).get('uname', '')}; unbound: {[u['path'] for u in f.get('unbound', [])][:15]}; "
                     f"deferred: {f.get('deferred', [])[:10]}")
        m = re.search(r'^compatible:\s*(\S+)', form_text, re.M)
        compat_part = m.group(1).split(',')[-1] if m else None
        keys = [k for k in (drv.get('name'), sm.get('label'), compat_part) if k and len(k) > 2]
        dm = [l for l in snap.get('sections', {}).get('dmesg', '').split('\n') if any(k.lower() in l.lower() for k in keys)]
        if dm:
            parts.append('Matching dmesg lines:\n```\n' + '\n'.join(dm[-40:]) + '\n```')
    return '\n\n'.join(parts)


def ask(ws, context: str, question: str) -> dict:
    cfg = config(ws)
    if not cfg['enabled']:
        raise RuntimeError('AI helper is off. Set `ai: {enabled: true}` in workspace.yaml (⚙).')
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        raise RuntimeError('ANTHROPIC_API_KEY is not set. Start the tool with:  ANTHROPIC_API_KEY=sk-... ./run.sh')
    body = {'model': cfg['model'], 'max_tokens': 1500, 'system': SYSTEM,
            'messages': [{'role': 'user', 'content': f"{context}\n\nQUESTION: {question}"}]}
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method='POST', headers={
        'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors='replace')[:500]
        raise RuntimeError(f'Claude API error {e.code}: {detail}')
    except urllib.error.URLError as e:
        raise RuntimeError(f'cannot reach the Claude API: {e.reason}')
    text = ''.join(b.get('text', '') for b in out.get('content', []) if b.get('type') == 'text')
    return {'answer': text, 'model': out.get('model', cfg['model']), 'usage': out.get('usage', {})}
