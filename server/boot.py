"""Boot files on the board: which .dtb boots, compare any two, install a new one, switch, reboot.

Safety rules (they are the whole point of this module):
  * never overwrite a file without first copying it to a dated backup next to it;
  * new uploads always get a new name;
  * every switch returns the exact U-Boot commands to get back from the serial console.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import time

BOOT_DIRS = ['/run/media/boot-mmcblk0p1', '/run/media/boot-mmcblk1p1', '/boot', '/mnt/boot']
LIVE = '(live: what the board booted)'


# ---------------------------------------------------------------- listing
def boot_info(t) -> dict:
    dirs = ' '.join(shlex.quote(d) for d in BOOT_DIRS)
    script = f"""
for d in {dirs} /run/media/boot-*; do
  [ -d "$d" ] && ls "$d" 2>/dev/null | grep -q '\\.dtb' && {{ echo "DIR $d"; break; }}
done
[ -n "$d" ] && cd "$d" && for f in *.dtb*; do [ -f "$f" ] && echo "F $(stat -c '%s %Y' "$f") $f"; done
command -v fw_setenv >/dev/null && echo "FWSET yes"
(fw_printenv -n fdtfile 2>/dev/null || fw_printenv fdtfile 2>/dev/null | cut -d= -f2) | sed 's/^/FDT /'
mount | grep " $d " | sed 's/^/MNT /'
"""
    rc, out, err = t.run(script, timeout=30)
    info = {'dir': None, 'files': [], 'fw_setenv': False, 'fdtfile': None, 'mount': None}
    for line in out.splitlines():
        tag, _, rest = line.partition(' ')
        if tag == 'DIR':
            info['dir'] = rest
        elif tag == 'F':
            size, mtime, name = rest.split(' ', 2)
            info['files'].append({'name': name, 'size': int(size), 'mtime': int(mtime),
                                  'kind': 'dtb' if name.endswith('.dtb') else ('dtbo' if name.endswith('.dtbo') else 'backup')})
        elif tag == 'FWSET':
            info['fw_setenv'] = True
        elif tag == 'FDT' and rest.strip():
            info['fdtfile'] = os.path.basename(rest.strip())
        elif tag == 'MNT':
            info['mount'] = rest
    if not info['dir']:
        info['error'] = 'No boot partition with .dtb files found (looked in ' + ', '.join(BOOT_DIRS) + ').'
    elif info['mount'] and ' ro,' in info['mount'] + ',':
        info['warning'] = 'The boot partition is mounted read-only: installing or switching will fail.'
    return info


def fetch(t, info: dict, name: str) -> bytes:
    path = '/sys/firmware/fdt' if name == LIVE else f"{info['dir']}/{name}"
    if name != LIVE and name not in {f['name'] for f in info['files']}:
        raise ValueError(f'{name} is not in {info["dir"]}')
    return t.read_bytes(path)


# ---------------------------------------------------------------- decompile + compare
def decompile(blob: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix='.dtb') as f:
        f.write(blob); f.flush()
        r = subprocess.run(['dtc', '-q', '-I', 'dtb', '-O', 'dts', '-s', f.name], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError('dtc could not read it: ' + (r.stderr.strip()[:300] or 'not a device-tree blob?'))
    return r.stdout


def parse_nodes(dts: str) -> tuple[dict, dict, bool]:
    """dtc output -> ({node path: {prop: raw value}}, {phandle: node path}, has_labels)."""
    nodes, stack, phandles = {}, [], {}
    for line in dts.split('\n'):
        s = line.strip()
        m = re.match(r'^([^\s{}=;]+) \{$', s)
        if m:
            stack.append(m.group(1)); nodes['/'.join(stack).replace('//', '/') or '/'] = {}
            continue
        if s == '};':
            stack and stack.pop(); continue
        if not stack or not s.endswith(';'):
            continue
        cur = '/'.join(stack).replace('//', '/') or '/'
        k, _, v = s[:-1].partition(' = ')
        if k == 'phandle':
            phandles[v.strip('<> ')] = cur
            continue
        nodes[cur][k] = v
    has_labels = '/__symbols__' in nodes
    for path in list(nodes):
        if path.startswith(('/__symbols__', '/__fixups__', '/__local_fixups__')):
            del nodes[path]
    return nodes, phandles, has_labels


def same_value(va: str | None, vb: str | None, pa: dict, pb: dict) -> bool:
    """Equal, or equal once phandle numbers (renumbered by every build) are resolved to the nodes they name."""
    if va == vb:
        return True
    if va is None or vb is None:
        return False
    ta, tb = re.split(r'(0x[0-9a-f]+)', va), re.split(r'(0x[0-9a-f]+)', vb)
    if len(ta) != len(tb):
        return False
    for x, y in zip(ta, tb):
        if x == y:
            continue
        if x in pa and y in pb and pa[x] == pb[y]:
            continue
        return False
    return True


def show(v: str | None, ph: dict) -> str | None:
    """Value with phandle numbers annotated by the node they point to, where unambiguous enough to help."""
    if v is None:
        return None
    return re.sub(r'0x[0-9a-f]+', lambda m: f"{m.group(0)}(→{'/'.join(ph[m.group(0)].split('/')[-2:])})" if m.group(0) in ph else m.group(0), v) \
        if len(v) < 300 else v


# What U-Boot writes into the tree while booting. Differences here are normal, not your change.
_UBOOT_PROPS = {'serial-number', 'bootargs', 'u-boot,version', 'local-mac-address', 'mac-address', 'kaslr-seed',
                'rng-seed', 'linux,initrd-start', 'linux,initrd-end', 'linux,usable-memory-range', 'stdout-path'}


def uboot_fixup(path: str, prop: str | None = None) -> bool:
    if path.startswith(('/chosen', '/memory')) or re.search(r'/memory@', path):
        return True
    if path == '/firmware' or 'optee' in path:      # secure-world firmware (OP-TEE) registers itself at boot
        return True
    if prop in _UBOOT_PROPS:
        return True
    return bool(prop == 'temperature' and '/thermal-zones/' in path)   # trip points set from the chip's grade


def compare(blob_a: bytes, blob_b: bytes, limit: int = 200) -> dict:
    a, pa, la = parse_nodes(decompile(blob_a))
    b, pb, lb = parse_nodes(decompile(blob_b))
    removed = [p for p in a if p not in b]
    added = [p for p in b if p not in a]
    changed = []
    for p in a:
        if p not in b:
            continue
        props = [{'prop': k, 'a': show(a[p].get(k), pa), 'b': show(b[p].get(k), pb), 'uboot': uboot_fixup(p, k)}
                 for k in sorted(set(a[p]) | set(b[p])) if not same_value(a[p].get(k), b[p].get(k), pa, pb)]
        if props:
            changed.append({'path': p, 'props': props, 'uboot': all(x['uboot'] for x in props)})
    # nodes switched on/off are what usually matters most: list them first
    status = [c for c in changed if any(x['prop'] == 'status' for x in c['props'])]
    changed = status + [c for c in changed if c not in status and not c['uboot']] + [c for c in changed if c['uboot']]
    removed = [{'path': p, 'uboot': uboot_fixup(p)} for p in removed]
    added = [{'path': p, 'uboot': uboot_fixup(p)} for p in added]
    root = lambda n: {'model': (n.get('/', {}).get('model') or '').strip('"'),
                      'compatible': re.findall(r'[\w,.+-]+', (n.get('/', {}).get('compatible') or '').replace('\\0', ' '))}
    return {'a': dict(root(a), labels=la, nodes=len(a)), 'b': dict(root(b), labels=lb, nodes=len(b)),
            'removed': removed[:limit], 'added': added[:limit], 'changed': changed[:limit],
            'counts': {'removed': len(removed), 'added': len(added), 'changed': len(changed),
                       'real': sum(not x['uboot'] for x in removed + added + changed)}}


def closest_to_live(t, info: dict) -> list[dict]:
    """Rank .dtb files by how close they are to what the board booted (U-Boot edits a few nodes on the way)."""
    live, pl, _ = parse_nodes(decompile(t.read_bytes('/sys/firmware/fdt')))
    out = []
    for f in info['files']:
        try:
            n, pn, _ = parse_nodes(decompile(fetch(t, info, f['name'])))
        except Exception:
            continue
        diff = sum(1 for p in set(n) | set(live)
                   if p not in n or p not in live or
                   any(not same_value(n[p].get(k), live[p].get(k), pn, pl) for k in set(n[p]) | set(live[p])))
        out.append({'name': f['name'], 'diff': diff})
    return sorted(out, key=lambda x: x['diff'])


# ---------------------------------------------------------------- changing the board
def _exists(t, path) -> bool:
    return t.run(f'test -e {shlex.quote(path)} && echo yes', timeout=15)[1].strip() == 'yes'


def install(t, info: dict, blob: bytes, name: str) -> dict:
    """Upload a .dtb under a NEW name (never overwrites)."""
    if not re.fullmatch(r'[\w.+-]+\.dtb', name):
        raise ValueError('use a simple file name ending in .dtb')
    path = f"{info['dir']}/{name}"
    if _exists(t, path):
        raise ValueError(f'{name} already exists on the board. Pick another name')
    t.write_bytes(path, blob)
    rc, out, _ = t.run(f'stat -c %s {shlex.quote(path)}', timeout=15)
    if out.strip() != str(len(blob)):
        raise RuntimeError(f'upload looks incomplete ({out.strip()} of {len(blob)} bytes)')
    return {'path': path, 'size': len(blob)}


def switch(t, info: dict, target: str, use: str) -> dict:
    """Make U-Boot boot `use` instead of `target` (the file it loads today).

    With fw_setenv we just repoint fdtfile. Without it we back up `target` and copy `use` over it.
    """
    names = {f['name'] for f in info['files']}
    if use not in names or target not in names:
        raise ValueError('pick files that exist in the boot partition')
    d = info['dir']
    stamp = time.strftime('%Y%m%d-%H%M%S')
    if info.get('fw_setenv'):
        prev = info.get('fdtfile') or target
        rc, out, err = t.run(f'fw_setenv fdtfile {shlex.quote(use)}', timeout=20)
        if rc:
            raise RuntimeError('fw_setenv failed: ' + (err or out).strip())
        return {'method': 'fw_setenv', 'boots': use, 'previous': prev,
                'undo_linux': f'fw_setenv fdtfile {prev}',
                'rescue': ['Stop autoboot in the serial console (press a key)', f'setenv fdtfile {prev}', 'saveenv', 'boot']}
    if use == target:
        raise ValueError('that file already boots')
    backup = f'{target}.bs-{stamp}'
    cmd = (f'cd {shlex.quote(d)} && cp -p {shlex.quote(target)} {shlex.quote(backup)} && '
           f'cp {shlex.quote(use)} {shlex.quote(target)} && sync && cmp {shlex.quote(use)} {shlex.quote(target)}')
    rc, out, err = t.run(cmd, timeout=60)
    if rc:
        raise RuntimeError('switch failed, nothing should have changed: ' + (err or out).strip())
    return {'method': 'swap', 'boots': use, 'as': target, 'backup': backup,
            'undo_linux': f'cd {d} && cp {backup} {target} && sync',
            'rescue': ['Stop autoboot in the serial console (press a key)', f'setenv fdtfile {backup}', 'boot',
                       f'then, back in Linux: cd {d} && cp {backup} {target} && sync']}


def history_add(state_dir: str, entry: dict):
    p = os.path.join(state_dir, 'boot-history.json')
    try:
        h = json.load(open(p))
    except (OSError, ValueError):
        h = []
    h.append(dict(entry, time=time.strftime('%Y-%m-%d %H:%M:%S')))
    json.dump(h[-50:], open(p, 'w'), indent=1)
