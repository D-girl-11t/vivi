"""Optional AI helper: ask Claude about the current step, with the tool's evidence attached.

Off unless workspace.yaml has `ai: {enabled: true}`. Two backends:

  claude-code  runs the Claude Code CLI you already use (`claude -p`), so it rides on
               your existing login: no API key to manage. Tools are disabled, so it
               can only read the evidence we send and answer; it can't touch files.
  api          calls the Claude API directly with ANTHROPIC_API_KEY from the environment
               (never written to disk).

Every answer comes back with the exact context that was sent, so nothing leaves unseen.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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
    cli = shutil.which('claude')
    backend = a.get('backend') or ('claude-code' if cli else 'api')
    ready = bool(cli) if backend == 'claude-code' else bool(os.environ.get('ANTHROPIC_API_KEY'))
    model = a.get('model') or ('' if backend == 'claude-code' else DEFAULT_MODEL)
    return {'enabled': bool(a.get('enabled')), 'backend': backend, 'model': model,
            'label': (model or 'your Claude Code default model') + (' via Claude Code' if backend == 'claude-code' else ' via API'),
            'ready': ready, 'has_key': ready, 'cli': cli}


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


def _prompt(context: str, question: str, history: list | None) -> list[dict]:
    msgs = [{'role': 'user', 'content': f"{context}\n\nQUESTION: {(history or [{}])[0].get('q') or question}"}]
    for i, turn in enumerate(history or []):
        msgs.append({'role': 'assistant', 'content': turn.get('a', '')})
        nxt = history[i + 1]['q'] if i + 1 < len(history) else question
        msgs.append({'role': 'user', 'content': nxt})
    return msgs


def ask(ws, context: str, question: str, history: list | None = None) -> dict:
    cfg = config(ws)
    if not cfg['enabled']:
        raise RuntimeError('AI helper is off. Set `ai: {enabled: true}` in workspace.yaml (⚙).')
    msgs = _prompt(context, question, history)
    if cfg['backend'] == 'claude-code':
        return _ask_claude_code(ws, cfg, msgs)
    return _ask_api(cfg, msgs)


def _ask_claude_code(ws, cfg, msgs) -> dict:
    if not cfg['cli']:
        raise RuntimeError('`claude` (Claude Code CLI) not found on PATH. Install it, or set ai.backend: api')
    # one prompt: earlier turns are replayed as a transcript
    text = msgs[0]['content'] if len(msgs) == 1 else msgs[0]['content'] + '\n\n' + '\n\n'.join(
        f"{'YOUR EARLIER ANSWER' if m['role'] == 'assistant' else 'FOLLOW-UP QUESTION'}:\n{m['content']}" for m in msgs[1:])
    cmd = [cfg['cli'], '-p', '--output-format', 'json', '--tools', '', '--no-session-persistence',
           '--strict-mcp-config', '--system-prompt', SYSTEM]
    if cfg['model']:
        cmd += ['--model', cfg['model']]
    try:
        r = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=300, cwd=ws.state_dir)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Claude Code did not answer within 5 minutes')
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f'Claude Code failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[:400]}'
                           + (' (not logged in? run `claude` once in a terminal)' if 'login' in (r.stderr + r.stdout).lower() else ''))
    if out.get('is_error'):
        raise RuntimeError(f"Claude Code error: {str(out.get('result'))[:400]}")
    models = list((out.get('modelUsage') or {}).keys())
    return {'answer': out.get('result', ''), 'model': (models[0] if models else 'claude') + ' via Claude Code',
            'cost_usd': out.get('total_cost_usd'), 'seconds': round((out.get('duration_ms') or 0) / 1000, 1)}


def _ask_api(cfg, msgs) -> dict:
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        raise RuntimeError('ANTHROPIC_API_KEY is not set. Start the tool with:  ANTHROPIC_API_KEY=sk-... ./run.sh')
    body = {'model': cfg['model'], 'max_tokens': 1500, 'system': SYSTEM, 'messages': msgs}
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
