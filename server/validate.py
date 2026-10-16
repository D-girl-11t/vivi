"""Is the kernel source + board .dts the user pointed at usable, and does it match the board?

Every check returns a plain-words message: what is wrong, why it matters, how to fix it.
Levels: error = the tool can't use it; warn = usable, but likely not what you want; ok.
"""
from __future__ import annotations

import os
import re
import subprocess

from .dts import DtsIndex
from .kernel import KernelTree


def _c(level, text, fix=None):
    return {'level': level, 'text': text, 'fix': fix}


def _release(uname: str | None) -> str | None:
    m = re.search(r'\b(\d+\.\d+(?:\.\d+)?[\w.+-]*)', uname or '')
    return m.group(1) if m else None


def _git_head(root: str) -> str | None:
    try:
        r = subprocess.run(['git', '-C', root, 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _root_compatible(dts: DtsIndex) -> list[str]:
    """The effective top-level `compatible` of the board file (the last definition wins)."""
    found = []
    for pf in dts.files:
        for nd in pf.nodes:
            if nd.parent is None and nd.name == '/':
                m = re.search(r'\bcompatible\s*=\s*([^;]+);', pf.own_props(nd))
                if m:
                    found = re.findall(r'"([^"]+)"', m.group(1))
    return found


def suggest_dts(kernel: KernelTree, soc: dict, words: list[str], limit=6) -> list[str]:
    """Board files in this SoC's dts folder whose names share words with the board model."""
    d = os.path.join(kernel.root, soc.get('dts_dir') or '')
    if not os.path.isdir(d):
        return []
    files = sorted(f for f in os.listdir(d) if f.endswith('.dts'))
    if soc.get('sibling_prefix'):
        files = [f for f in files if f.startswith(soc['sibling_prefix'])] or files
    words = [w.lower() for w in words if len(w) > 2]
    scored = sorted(files, key=lambda f: -sum(w in f.lower() for w in words))
    return [os.path.join(soc.get('dts_dir', ''), f) for f in scored[:limit]]


def validate_source(kernel_src: str | None, board_dts: str | None, soc: dict, snap: dict | None) -> dict:
    checks, info = [], {}
    live = snap if snap and not (snap.get('meta') or {}).get('demo') else None
    board_rel = _release((live or {}).get('system', {}).get('uname'))
    board_compat = ((live or {}).get('system', {}).get('compatible') or '').split()

    # ---- the kernel folder
    ks = os.path.expanduser(str(kernel_src or '').strip())
    if not ks:
        checks.append(_c('error', 'Kernel source is not set.',
                         'Point it at the folder of the kernel your board runs (a git clone, or Yocto\'s kernel-source).'))
        return {'checks': checks, 'info': info, 'ok': False}
    if re.fullmatch(r'v?\d+\.\d+[\w.+-]*', ks):
        checks.append(_c('error', f'"{ks}" is a kernel version, not a folder.',
                         'Kernel source = the folder that holds the code. Download it first (see the command below), then enter that folder.'))
        return {'checks': checks, 'info': info, 'ok': False}
    if not os.path.isabs(ks):
        checks.append(_c('error', f'"{ks}" is not a full path.', 'Use a full path such as /home/you/linux-imx (or ~/linux-imx).'))
        return {'checks': checks, 'info': info, 'ok': False}
    if not os.path.exists(ks):
        checks.append(_c('error', f'Folder not found: {ks}', 'Check the spelling, or download the source there first.'))
        return {'checks': checks, 'info': info, 'ok': False}
    if not os.path.isdir(ks):
        checks.append(_c('error', f'{ks} is a file, not a folder.', 'Enter the top folder of the kernel tree (the one with Makefile, arch/, drivers/).'))
        return {'checks': checks, 'info': info, 'ok': False}
    kt = KernelTree(ks)
    kinfo = kt.info()
    info.update(kinfo)
    missing = [d for d in ('Makefile', 'arch', 'drivers', 'include') if not os.path.exists(os.path.join(ks, d))]
    if not kinfo.get('version') or missing:
        sub = [d for d in os.listdir(ks) if os.path.isfile(os.path.join(ks, d, 'Makefile')) and os.path.isdir(os.path.join(ks, d, 'drivers'))]
        checks.append(_c('error', f'{ks} is not a Linux kernel tree' + (f' (missing: {", ".join(missing)})' if missing else '') + '.',
                         f'Did you mean {os.path.join(ks, sub[0])}?' if sub else
                         'Pick the top folder of the kernel: it contains Makefile, arch/, drivers/ and include/.'))
        return {'checks': checks, 'info': info, 'ok': False}
    checks.append(_c('ok', f"Linux {kinfo['version']}" + (f" ({kinfo['flavor']})" if kinfo.get('flavor') else '')))
    if ks.startswith(('/tmp/', '/var/tmp/')):
        checks.append(_c('warn', 'This is a temporary folder under /tmp: it can disappear on reboot.', 'Keep your kernel tree in your home folder.'))
    if not os.path.isdir(os.path.join(ks, 'Documentation', 'devicetree', 'bindings')):
        checks.append(_c('warn', 'No Documentation/devicetree/bindings in this tree, so binding lookups will be empty.'))

    # ---- does it match the running board?
    if board_rel:
        m = re.match(r'(\d+\.\d+\.\d+)', board_rel)
        if m and not kinfo['version'].startswith(m.group(1)):
            checks.append(_c('warn', f"The board runs {board_rel}, but this tree is {kinfo['version']}.",
                             'Checks will compare your board with code it is not running. Use the tree that built the board\'s kernel.'))
        g = re.search(r'-g([0-9a-f]{7,40})$', board_rel)
        head = _git_head(ks)
        if g and head:
            if head.startswith(g.group(1)):
                checks.append(_c('ok', f'Same git commit as the board\'s kernel ({g.group(1)}).'))
            else:
                checks.append(_c('warn', f'The board\'s kernel was built from commit {g.group(1)}, this tree is at {head[:12]}.',
                                 'Small differences are usually fine for device trees, but modules built here may not load on the board.'))

    # ---- the board .dts inside it
    bd = str(board_dts or '').strip()
    words = re.split(r'[^A-Za-z0-9]+', ' '.join(board_compat) + ' ' + ((live or {}).get('system', {}).get('model') or ''))
    if not bd:
        sug = suggest_dts(kt, soc, words)
        checks.append(_c('error', 'Board device-tree file is not set.',
                         ('Candidates in this tree: ' + ', '.join(sug)) if sug else 'Enter the path of your board\'s .dts inside the kernel tree.'))
        return {'checks': checks, 'info': info, 'ok': False}
    if os.path.isabs(bd):
        if not bd.startswith(ks.rstrip('/') + '/'):
            checks.append(_c('error', 'The board file must be inside the kernel tree.',
                             'Use a path relative to the kernel folder, e.g. arch/arm64/boot/dts/<vendor>/<board>.dts'))
            return {'checks': checks, 'info': info, 'ok': False}
        bd = os.path.relpath(bd, ks)
    full = os.path.join(ks, bd)
    if not os.path.isfile(full):
        sug = suggest_dts(kt, soc, words + re.split(r'[^A-Za-z0-9]+', os.path.basename(bd)))
        checks.append(_c('error', f'Board file not found in this tree: {bd}',
                         ('Similar files here: ' + ', '.join(sug)) if sug else 'Check the path; on the board, `fw_printenv fdtfile` names the .dtb that boots.'))
        return {'checks': checks, 'info': info, 'ok': False}
    if bd.endswith('.dtsi'):
        checks.append(_c('warn', f'{os.path.basename(bd)} is an include file (.dtsi), not a board file.',
                         'A .dtsi describes a chip or module; pick the board\'s .dts that includes it.'))
    elif bd.endswith(('.dtso', '.dtbo')):
        checks.append(_c('warn', f'{os.path.basename(bd)} is an overlay, not a base board file.'))
    elif not bd.endswith('.dts'):
        checks.append(_c('error', f'{os.path.basename(bd)} is not a device-tree source file (.dts).'))
        return {'checks': checks, 'info': info, 'ok': False}

    soc_dir = soc.get('dts_dir') or os.path.dirname(bd)
    try:
        ix = DtsIndex(ks, bd, soc_dir)
    except Exception as e:   # unreadable or badly broken file
        checks.append(_c('error', f'Could not read {bd}: {e}'))
        return {'checks': checks, 'info': info, 'ok': False}
    miss = [i['file'] for i in ix.include_tree if i.get('missing')]
    if miss:
        checks.append(_c('error', f'{bd} includes files that are not in this tree: {", ".join(miss[:5])}',
                         'This .dts probably belongs to a different kernel (vendor BSP vs mainline), or files were not copied over.'))
    else:
        checks.append(_c('ok', f'{os.path.basename(bd)}: {len(ix.include_tree)} files in its include chain, all found.'))

    # ---- is this .dts for the board that is connected?
    dts_compat = _root_compatible(ix)
    info['dts_compatible'] = dts_compat
    if board_compat and dts_compat:
        if dts_compat[0] == board_compat[0]:
            checks.append(_c('ok', f'Matches the connected board ({board_compat[0]}).'))
        elif set(dts_compat) & set(board_compat):
            checks.append(_c('warn', f'This .dts is "{dts_compat[0]}", the board is running "{board_compat[0]}". Same family, different variant.',
                             'Fine if you are preparing a new variant; otherwise pick the file that matches the board.'))
        else:
            sug = suggest_dts(kt, soc, words)
            checks.append(_c('error', f'This .dts is for "{dts_compat[0]}", but the connected board is "{board_compat[0]}".',
                             ('Files that look closer: ' + ', '.join(sug)) if sug else 'Pick the .dts for the board you have.'))
    ok = not any(c['level'] == 'error' for c in checks)
    return {'checks': checks, 'info': info, 'ok': ok}


# ---------------------------------------------------------------- auto-detect from the connected board
_SKIP = {'node_modules', '.cache', '.git', 'Trash', '.local', 'snap', '.npm', '.cargo', '.rustup', 'proc', 'sys'}


def _is_kernel_tree(d: str) -> bool:
    return all(os.path.exists(os.path.join(d, x)) for x in ('Makefile', 'arch', 'drivers', 'include', 'Kconfig'))


def find_kernel_trees(roots: list[str], max_depth=5, limit=40) -> list[str]:
    """Kernel trees under the given folders (does not descend into a tree once found)."""
    found = []
    for root in roots:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        for dp, dns, _ in os.walk(root):
            depth = dp[len(root):].count(os.sep)
            if _is_kernel_tree(dp):
                found.append(dp)
                dns[:] = []
                continue
            # Yocto keeps the kernel at tmp*/work-shared/<machine>/kernel-source: allow that deep
            deep_ok = 'work-shared' in dp or 'tmp' in os.path.basename(dp)
            if depth >= max_depth + (3 if deep_ok else 0):
                dns[:] = []
                continue
            dns[:] = [d for d in dns if d not in _SKIP and not d.startswith('.')]
            if len(found) >= limit:
                break
    return list(dict.fromkeys(found))


def _dts_root_compat(path: str) -> list[str]:
    try:
        text = open(path, errors='replace').read()
    except OSError:
        return []
    from .dts import strip_comments
    text = strip_comments(text)
    m = re.search(r'^/\s*\{(.*?)^\};', text, re.S | re.M)
    if not m:
        return []
    body = re.sub(r'\{[^{}]*\}', '', re.sub(r'\{[^{}]*\}', '', m.group(1)))   # drop child nodes (two levels)
    c = re.search(r'\bcompatible\s*=\s*([^;]+);', body)
    return re.findall(r'"([^"]+)"', c.group(1)) if c else []


def detect_from_board(snap: dict | None, soc: dict, roots: list[str]) -> dict:
    """Use the connected board's kernel release and compatible to find the source tree and board file."""
    live = snap if snap and not (snap.get('meta') or {}).get('demo') else None
    if not live:
        return {'ok': False, 'error': 'No real board scan yet. Connect the board and press Scan first: detection uses what the board reports.'}
    rel = _release(live['system'].get('uname'))
    compat = (live['system'].get('compatible') or '').split()
    g = re.search(r'-g([0-9a-f]{7,40})$', rel or '')
    ver = re.match(r'(\d+\.\d+\.\d+)', rel or '')
    out = {'board': {'release': rel, 'compatible': compat, 'model': live['system'].get('model')}, 'candidates': []}
    for t in find_kernel_trees(roots):
        info = KernelTree(t).info()
        head = _git_head(t) if g else None
        score, why = 0, []
        if g and head and head.startswith(g.group(1)):
            score += 100; why.append(f'same git commit {g.group(1)}')
        if ver and (info.get('version') or '').startswith(ver.group(1)):
            score += 20; why.append(f"same version {ver.group(1)}")
        if 'Trash' in t or t.startswith(('/tmp/', '/var/tmp/')):
            score -= 50; why.append('in Trash or /tmp')
        dts_hits = []
        d = os.path.join(t, soc.get('dts_dir') or '')
        if compat and os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith('.dts'):
                    rc = _dts_root_compat(os.path.join(d, f))
                    if rc and rc[0] == compat[0]:
                        dts_hits.append(os.path.join(soc.get('dts_dir'), f))
        if dts_hits:
            score += 50; why.append(f'has {os.path.basename(dts_hits[0])} for {compat[0]}')
        out['candidates'].append({'kernel_src': t, 'version': info.get('version'), 'flavor': info.get('flavor'),
                                  'board_dts': dts_hits[0] if dts_hits else None, 'score': score, 'why': why})
    out['candidates'].sort(key=lambda c: -c['score'])
    best = out['candidates'][0] if out['candidates'] else None
    out['best'] = best if best and best['score'] > 0 else None
    out['ok'] = bool(out['best'])
    if not out['candidates']:
        out['error'] = 'No kernel source tree found in your home folder.'
    elif not out['best']:
        out['error'] = f'Found {len(out["candidates"])} kernel tree(s), but none matches the board\'s kernel {rel}.'
    elif not out['best']['board_dts']:
        out['note'] = f'Kernel tree found, but no .dts in it declares "{compat[0] if compat else "?"}", the board\'s own name. ' \
                      'The board may run a modified device tree whose source is elsewhere.'
    return out
