"""Build a board .dtb from the kernel source, and bring a .dts in from a web link."""
from __future__ import annotations

import os
import re
import subprocess
import urllib.error
import urllib.request

from .fetch import candidates


def arch_dts_dir(soc: dict) -> str:
    return os.path.join('arch', soc.get('arch', 'arm64'), 'boot', 'dts')


def build_dtb(kernel_root: str, board_dts: str, soc: dict) -> dict:
    """make <vendor>/<board>.dtb with labels kept (-@). Returns the output path or a readable error."""
    base = arch_dts_dir(soc)
    if not board_dts.startswith(base + '/'):
        raise ValueError(f'{board_dts} is not under {base}/')
    if not os.path.isfile(os.path.join(kernel_root, '.config')):
        raise RuntimeError('This kernel tree is not configured yet (no .config). Run once in it: '
                           'ssh <board> "zcat /proc/config.gz" > .config && make ARCH=arm64 olddefconfig')
    target = os.path.splitext(os.path.relpath(board_dts, base))[0] + '.dtb'
    cross = soc.get('cross_compile', 'aarch64-linux-gnu-')
    cmd = ['make', f"ARCH={soc.get('arch', 'arm64')}", f'CROSS_COMPILE={cross}', 'DTC_FLAGS=-@', target]
    try:
        r = subprocess.run(cmd, cwd=kernel_root, capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError('the build took more than 15 minutes and was stopped')
    log = (r.stdout + r.stderr).strip()
    errors = [l for l in log.splitlines() if re.search(r'\bError\b|error:|FATAL|No rule to make target', l)]
    if r.returncode != 0:
        raise RuntimeError('Build failed: ' + ('\n'.join(errors[:8]) or log[-1200:]))
    out = os.path.join(kernel_root, base, target)
    warnings = [l for l in log.splitlines() if 'Warning' in l]
    return {'path': out, 'rel': os.path.relpath(out, kernel_root), 'size': os.path.getsize(out),
            'cmd': ' '.join(cmd), 'warnings': warnings[-20:]}


def fetch_dts(url: str, name: str, kernel_root: str, soc: dict) -> dict:
    """Download a .dts into this SoC's dts folder under a new name, and register it in the Makefile."""
    if not re.fullmatch(r'[\w.+-]+', name or ''):
        raise ValueError('give it a simple file name, like imx93-11x11-frdm-mything')
    name = re.sub(r'\.dts$', '', name)
    d = os.path.join(kernel_root, soc.get('dts_dir') or '')
    if not os.path.isdir(d):
        raise RuntimeError(f"the SoC's device-tree folder is not in this tree: {soc.get('dts_dir')}")
    dest = os.path.join(d, name + '.dts')
    if os.path.exists(dest):
        raise ValueError(f'{name}.dts already exists in {soc.get("dts_dir")}. Pick another name')
    tried, text = [], None
    for u in candidates(url, name, []):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'bringup-studio'}), timeout=30) as r:
                data = r.read(3_000_001)
        except (urllib.error.URLError, OSError) as e:
            tried.append(f'{u} ({getattr(e, "code", None) or getattr(e, "reason", e)})'); continue
        t = data.decode('utf-8', errors='replace')
        if '/dts-v1/' in t or re.search(r'^\s*/\s*\{', t, re.M) or re.search(r'^\s*&\w+\s*\{', t, re.M):
            text = t; break
        tried.append(f'{u} (does not look like device-tree source: an HTML page?)')
    if text is None:
        raise RuntimeError('Could not download a .dts. Tried: ' + '; '.join(tried[:4]) +
                           '. Use the link to the raw file (on GitHub: the "Raw" button).')
    kind = 'overlay' if '/plugin/' in text else ('board' if '/dts-v1/' in text else 'fragment')
    with open(dest, 'w') as f:
        f.write(text)
    mk = os.path.join(d, 'Makefile')
    line = (soc.get('dtb_makefile_line') or 'dtb-y += {name}.dtb').format(name=name)
    if os.path.isfile(mk) and kind == 'board':
        with open(mk, 'a') as f:
            f.write(f'\n{line}\n')
    return {'path': os.path.join(soc.get('dts_dir'), name + '.dts'), 'kind': kind, 'bytes': len(text),
            'includes': re.findall(r'#include\s+[<"]([^>"]+)[>"]', text)}
