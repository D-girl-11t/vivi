"""Download a driver's .c file from a web link the user pasted into `driver_src`.

People usually paste whatever link they found: a GitHub branch page, a file page, or a
raw file. We turn that into raw-file URLs to try, download the first one that looks like
a kernel driver into the workspace, and point the form at the local copy.
"""
from __future__ import annotations

import os
import re
import urllib.error
import urllib.request

MAX_BYTES = 2_000_000


def candidates(url: str, module: str, dirs: list[str]) -> list[str]:
    url = url.strip().rstrip('/')
    m = re.match(r'https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$', url)
    if m:                                    # a file page on GitHub
        return [f'https://raw.githubusercontent.com/{m[1]}/{m[2]}/{m[3]}']
    m = re.match(r'https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)(?:/(.*))?$', url)
    if m:                                    # a branch (or folder) page: guess where the driver lives
        base = f'https://raw.githubusercontent.com/{m[1]}/{m[2]}/{m[3]}'
        if m[4] and m[4].endswith('.c'):
            return [f'{base}/{m[4]}']
        folders = ([m[4]] if m[4] else []) + dirs
        return [f'{base}/{d.strip("/")}/{module}.c' for d in dict.fromkeys(folders)]
    m = re.match(r'https?://gitlab\.com/(.+?)/-/blob/(.+)$', url)
    if m:
        return [f'https://gitlab.com/{m[1]}/-/raw/{m[2]}']
    return [url]                             # raw link, or another host: try as is


def fetch_driver(url: str, module: str, dirs: list[str], dest_dir: str) -> dict:
    tried = []
    for u in candidates(url, module, dirs):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'bringup-studio'}), timeout=30) as r:
                data = r.read(MAX_BYTES + 1)
        except (urllib.error.URLError, OSError) as e:
            tried.append(f'{u} ({getattr(e, "code", None) or getattr(e, "reason", e)})')
            continue
        text = data.decode('utf-8', errors='replace')
        if len(data) > MAX_BYTES or not re.search(r'#include\s*<linux/', text):
            tried.append(f'{u} (not a kernel C file)')
            continue
        os.makedirs(dest_dir, exist_ok=True)
        name = os.path.basename(u.split('?')[0]) if u.endswith('.c') else f'{module}.c'
        path = os.path.join(dest_dir, name)
        with open(path, 'w') as f:
            f.write(text)
        return {'ok': True, 'url': u, 'path': path,
                'compatibles': re.findall(r'\.compatible\s*=\s*"([^"]+)"', text)}
    return {'ok': False, 'tried': tried}
