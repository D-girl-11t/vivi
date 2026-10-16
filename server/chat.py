"""Chat assistant: a conversation with Claude that can look things up by itself.

With the claude-code backend each chat is one Claude Code session (resumed turn by turn), with
read-only tools only: Read, Grep and Glob, limited to your kernel tree, this project (forms,
knowledge, the last board scan in .state/last-scan.json). It cannot write files, run commands or
reach the board; what it looks up shows in the chat as it happens.

With the api backend it falls back to plain question/answer (no lookups).

The page starts a turn with POST /api/chat and long-polls /api/chat/poll for its events.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid

from . import ai

TOOLS = ['Read', 'Grep', 'Glob']

AGENT_SYSTEM = ai.SYSTEM + """

You are the chat assistant of Bring-up Studio and you can look things up yourself with
read-only tools (Read, Grep, Glob). Use them to check facts instead of guessing: binding docs and
driver sources in the kernel tree, the board's device-tree source, the last board scan (dmesg,
probe state, kconfig), the peripheral forms and the tool's knowledge templates. Say briefly what
you looked at. You cannot change files, run commands or reach the board: when a command on the
board would answer the question, give it to the user to run (Serial tab or ssh).

Where things are:
{places}"""

CHATS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _places(ws) -> tuple[str, list[str]]:
    rows, dirs = [], [ws.root]
    if ws.ktree:
        rows.append(f'- kernel source tree: {ws.ktree.root}')
        dirs.append(ws.ktree.root)
    if ws.dts and ws.dts.ok:
        rows.append(f'- board device-tree file: {ws.dts.board_dts}')
    board = _board_files(ws)
    if board:
        rows.append(f'- last board scan, one readable file per part (dmesg, findings, kconfig, ...): {board}/')
    rows += [
             f'- peripheral forms: {ws.periph_dir}',
             f'- knowledge templates (bus/subsystem/soc): {os.path.join(ws.root, "knowledge")}',
             f'- workspace settings: {os.path.join(ws.root, "workspace.yaml")}',
             f'- boot history (dtb switches): {os.path.join(ws.state_dir, "boot-history.json")}']
    return '\n'.join(rows), dirs


def _board_files(ws) -> str | None:
    """The last scan split into plain files the assistant can grep (the scan itself is one long JSON line)."""
    snap = ws.snapshot()
    if not snap:
        return None
    d = os.path.join(ws.state_dir, 'agent-board')
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))
    for name, text in (snap.get('sections') or {}).items():
        if isinstance(text, str):
            open(os.path.join(d, f'{name}.txt'), 'w').write(text)
    for key in ('meta', 'system', 'findings', 'devices'):
        if snap.get(key) is not None:
            json.dump(snap[key], open(os.path.join(d, f'{key}.json'), 'w'), indent=1)
    if snap.get('kconfig'):
        open(os.path.join(d, 'kconfig.txt'), 'w').write(''.join(f'{k}={v}\n' for k, v in sorted(snap['kconfig'].items())))
    return d


def _short(name: str, inp: dict) -> str:
    """One readable line for a tool call, e.g.  Grep "imx519" in drivers/media"""
    if name == 'Read':
        return f"Read {inp.get('file_path', '')}" + (f" (from line {inp['offset']})" if inp.get('offset') else '')
    if name == 'Grep':
        return f"Grep \"{inp.get('pattern', '')}\"" + (f" in {inp['path']}" if inp.get('path') else '') + (f" ({inp['glob']})" if inp.get('glob') else '')
    if name == 'Glob':
        return f"Glob {inp.get('pattern', '')}" + (f" in {inp['path']}" if inp.get('path') else '')
    return f'{name} {json.dumps(inp)[:120]}'


def new_chat() -> dict:
    c = {'id': uuid.uuid4().hex[:12], 'session': str(uuid.uuid4()), 'started': False, 'history': [],
         'ctx_key': None, 'turns': [], 'busy': False, 'proc': None, 'cond': threading.Condition()}
    with _LOCK:
        CHATS[c['id']] = c
    return c


def get(chat_id: str | None) -> dict | None:
    return CHATS.get(chat_id or '')


def _emit(c, turn, ev):
    with c['cond']:
        turn['events'].append(ev)
        c['cond'].notify_all()


def start_turn(ws, c: dict, message: str, context: str | None, ctx_key: str | None) -> int:
    if c['busy']:
        raise RuntimeError('still answering the last message: wait, or press Stop')
    cfg = ai.config(ws)
    if not cfg['enabled']:
        raise RuntimeError('AI helper is off. Set `ai: {enabled: true}` in workspace.yaml (⚙).')
    # the evidence goes in with the first message, and again whenever the user moves to another device/step
    text = message
    if context and ctx_key != c['ctx_key']:
        text = f'{context}\n\nQUESTION: {message}' if not c['ctx_key'] else \
            f'[The user is now looking at a different device/step. Updated evidence:]\n{context}\n\nQUESTION: {message}'
        c['ctx_key'] = ctx_key
    turn = {'n': len(c['turns']), 'q': message, 'events': [], 'done': False, 'answer': ''}
    c['turns'].append(turn)
    c['busy'] = True
    target = _run_claude_code if cfg['backend'] == 'claude-code' else _run_api
    threading.Thread(target=target, args=(ws, cfg, c, turn, text), daemon=True).start()
    return turn['n']


def _finish(c, turn, **ev):
    turn['done'] = True
    c['busy'] = False
    c['proc'] = None
    _emit(c, turn, dict(ev, type=ev.get('type', 'done')))


def _run_claude_code(ws, cfg, c, turn, text):
    if not cfg['cli']:
        return _finish(c, turn, type='error', text='`claude` (Claude Code CLI) not found on PATH. Install it, or set ai.backend: api')
    places, dirs = _places(ws)
    cmd = [cfg['cli'], '-p', '--output-format', 'stream-json', '--verbose', '--strict-mcp-config',
           '--tools', *TOOLS, '--allowedTools', *TOOLS, '--add-dir', *dirs,
           '--system-prompt', AGENT_SYSTEM.format(places=places)]
    cmd += ['--resume', c['session']] if c['started'] else ['--session-id', c['session']]
    if cfg['model']:
        cmd += ['--model', cfg['model']]
    t0 = time.time()
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, cwd=ws.state_dir)
    except OSError as e:
        return _finish(c, turn, type='error', text=f'could not start Claude Code: {e}')
    c['proc'] = p
    p.stdin.write(text)
    p.stdin.close()
    result = None
    for line in p.stdout:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get('type') == 'assistant':
            for b in m.get('message', {}).get('content', []):
                if b.get('type') == 'tool_use':
                    _emit(c, turn, {'type': 'tool', 'text': _short(b.get('name', ''), b.get('input') or {})})
                elif b.get('type') == 'text' and b.get('text', '').strip():
                    _emit(c, turn, {'type': 'text', 'text': b['text']})
        elif m.get('type') == 'result':
            result = m
    p.wait()
    c['started'] = True
    if result is None or result.get('is_error'):
        err = (result or {}).get('result') or p.stderr.read().strip()[:400] or f'exit {p.returncode}'
        if p.returncode in (-9, -15):
            err = 'stopped'
        elif 'login' in err.lower():
            err += ' (not logged in? run `claude` once in a terminal)'
        return _finish(c, turn, type='error', text=err)
    turn['answer'] = result.get('result', '')
    models = list((result.get('modelUsage') or {}).keys())
    _finish(c, turn, answer=turn['answer'], model=(models[0] if models else 'claude') + ' via Claude Code',
            cost_usd=result.get('total_cost_usd'), seconds=round(time.time() - t0, 1))


def _run_api(ws, cfg, c, turn, text):
    try:
        msgs = []
        for h in c['history']:
            msgs += [{'role': 'user', 'content': h['q']}, {'role': 'assistant', 'content': h['a']}]
        msgs.append({'role': 'user', 'content': text})
        r = ai._ask_api(cfg, msgs)
    except RuntimeError as e:
        return _finish(c, turn, type='error', text=str(e))
    c['history'].append({'q': text, 'a': r['answer']})
    turn['answer'] = r['answer']
    _finish(c, turn, answer=r['answer'], model=r.get('model'))


def stop(c):
    p = c.get('proc')
    if p and p.poll() is None:
        p.terminate()


def poll(c: dict, turn_n: int, since: int, wait: float = 20.0) -> dict:
    turn = c['turns'][turn_n]
    with c['cond']:
        if len(turn['events']) <= since and not turn['done']:
            c['cond'].wait(wait)
        return {'events': turn['events'][since:], 'next': len(turn['events']), 'done': turn['done']}
