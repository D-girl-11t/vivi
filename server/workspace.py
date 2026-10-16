"""Workspace = workspace.yaml + source tree index + board transport + saved progress."""
from __future__ import annotations

import json
import os
import threading

import yaml

from .board import analyze, load_snapshot, make_transport, scan
from .dts import DtsIndex
from .kernel import KernelTree

PERIPHERAL_TEMPLATE = """\
# Peripheral form: read the values off your schematic and datasheet.
# The tool works out everything else from the source tree and the board.
name: {name}
description: what it is and what you want from it
bus: i2c              # {buses}
subsystem: iio        # {subsystems}
compatible: vendor,chip   # look it up: grep -r "chip" Documentation/devicetree/bindings
connection:
  controller: 3       # SoC instance of that bus (lpi2c3 -> 3)
  reg: 0x68           # I2C address / SPI chip-select
  pins: []            # e.g. [GPIO_IO28, GPIO_IO29] or [{{pad: GPIO_IO08, function: LPSPI3_PCS0}}]
  # irq: {{pad: GPIO_IO12, type: IRQ_TYPE_EDGE_FALLING}}
# properties: |       # extra DT properties copied into the node verbatim
#   vdd-supply = <&reg_3v3>;
# config: CONFIG_FOO  # override if the driver lookup can't find it
# driver_src: drivers-extra/foo.c   # only if your kernel has no driver: the guide adds a Driver layer
goal: what "working" means to you
"""


class Workspace:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.state_dir = os.path.join(self.root, '.state')
        os.makedirs(self.state_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.live_results: dict = {}
        self.load()

    # ---------- config ----------
    @property
    def cfg_path(self):
        return os.path.join(self.root, 'workspace.yaml')

    def load(self):
        with self.lock:
            with open(self.cfg_path) as f:
                self.cfg = yaml.safe_load(f) or {}
            src = os.path.expanduser(self.cfg.get('kernel_src') or '')
            self.ktree = KernelTree(src) if src and os.path.isdir(src) else None
            self.dts = None
            if self.ktree and self.cfg.get('board_dts'):
                soc_dts_dir = self.cfg.get('dts_dir') or os.path.dirname(self.cfg['board_dts'])
                self.dts = DtsIndex(src, self.cfg['board_dts'], soc_dts_dir)
            bcfg = dict(self.cfg.get('board') or {})
            if bcfg.get('snapshot') and not os.path.isabs(bcfg['snapshot']):
                bcfg['snapshot'] = os.path.join(self.root, bcfg['snapshot'])
            self.transport = make_transport(bcfg, self.state_dir)
            self._snap = None
            self.live_results.clear()

    def save_cfg_text(self, text: str):
        yaml.safe_load(text)  # validate
        with open(self.cfg_path, 'w') as f:
            f.write(text)
        self.load()

    def set_cfg_fields(self, fields: dict):
        """Change a few settings from the setup form while keeping the file's comments."""
        self.save_cfg_text(set_yaml_fields(open(self.cfg_path).read(), fields))

    # ---------- board ----------
    @property
    def last_scan_path(self):
        return os.path.join(self.state_dir, 'last-scan.json')

    def snapshot(self) -> dict | None:
        with self.lock:
            if self._snap is None:
                if not self.transport.live:
                    self._snap = load_snapshot(self.transport.path)
                else:
                    self._snap = load_snapshot(self.last_scan_path)
                if self._snap and self._snap.get('devices') is not None:
                    # re-classify with what the kernel tree knows (also upgrades older saved scans)
                    early = self.ktree.early_init_compatibles() if self.ktree and self.ktree.ok else None
                    self._snap['findings'] = analyze(self._snap, early)
            return self._snap

    def rescan(self) -> dict:
        if not self.transport.live:
            self._snap = None
            snap = self.snapshot()
            if not snap:
                raise RuntimeError('snapshot mode: snapshot file not found. Set board.transport to ssh in workspace.yaml')
            return snap
        snap = scan(self.transport)
        early = self.ktree.early_init_compatibles() if self.ktree and self.ktree.ok else None
        snap['findings'] = analyze(snap, early)
        with self.lock:
            with open(self.last_scan_path, 'w') as f:
                json.dump(snap, f)
            self._snap = snap
            self.live_results.clear()
        return snap

    def save_snapshot(self, name: str) -> str:
        snap = self.snapshot()
        if not snap:
            raise RuntimeError('nothing to save, scan the board first')
        d = os.path.join(self.root, 'snapshots')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, safe_name(name) + '.json')
        with open(p, 'w') as f:
            json.dump(snap, f, indent=1)
        return os.path.relpath(p, self.root)

    # ---------- peripherals ----------
    @property
    def periph_dir(self):
        return os.path.join(self.root, self.cfg.get('peripherals_dir', 'peripherals'))

    def peripherals(self) -> list[dict]:
        out = []
        for f in sorted(os.listdir(self.periph_dir)) if os.path.isdir(self.periph_dir) else []:
            if f.endswith('.yaml'):
                try:
                    d = yaml.safe_load(open(os.path.join(self.periph_dir, f))) or {}
                except yaml.YAMLError as e:
                    d = {'error': str(e)}
                out.append({'id': f[:-5], 'name': d.get('name', f[:-5]), 'bus': d.get('bus'),
                            'subsystem': d.get('subsystem'), 'description': d.get('description', ''),
                            'error': d.get('error')})
        return out

    def periph_path(self, pid: str) -> str:
        return os.path.join(self.periph_dir, safe_name(pid) + '.yaml')

    def periph_text(self, pid: str) -> str:
        with open(self.periph_path(pid)) as f:
            return f.read()

    def periph_form(self, pid: str) -> dict:
        return yaml.safe_load(self.periph_text(pid)) or {}

    def save_periph(self, pid: str, text: str):
        yaml.safe_load(text)
        os.makedirs(self.periph_dir, exist_ok=True)
        with open(self.periph_path(pid), 'w') as f:
            f.write(text)

    @property
    def trash_dir(self):
        return os.path.join(self.state_dir, 'trash')

    def delete_periph(self, pid: str) -> str:
        """Move the form to .state/trash/ (with its ticked-off steps) so it can be restored."""
        import time
        src = self.periph_path(pid)
        if not os.path.isfile(src):
            raise FileNotFoundError(pid)
        os.makedirs(self.trash_dir, exist_ok=True)
        name = f"{safe_name(pid)}--{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(src, os.path.join(self.trash_dir, name + '.yaml'))
        with self.lock:
            p = self.progress()
            ticks = p.pop(pid, None)
            if ticks:
                json.dump(ticks, open(os.path.join(self.trash_dir, name + '.progress.json'), 'w'))
                json.dump(p, open(self.progress_path, 'w'), indent=1)
        return name

    def restore_periph(self, name: str) -> str:
        name = safe_name(name)
        src = os.path.join(self.trash_dir, name + '.yaml')
        pid = name.rsplit('--', 1)[0]
        if not os.path.isfile(src):
            raise FileNotFoundError(name)
        if os.path.exists(self.periph_path(pid)):
            raise FileExistsError(f'a device called {pid} already exists')
        os.makedirs(self.periph_dir, exist_ok=True)
        os.replace(src, self.periph_path(pid))
        prog = os.path.join(self.trash_dir, name + '.progress.json')
        if os.path.isfile(prog):
            with self.lock:
                p = self.progress()
                p[pid] = json.load(open(prog))
                json.dump(p, open(self.progress_path, 'w'), indent=1)
            os.unlink(prog)
        return pid

    # ---------- progress (manual ticks) ----------
    @property
    def progress_path(self):
        return os.path.join(self.state_dir, 'progress.json')

    def progress(self) -> dict:
        try:
            return json.load(open(self.progress_path))
        except (OSError, ValueError):
            return {}

    def set_manual(self, pid: str, step: str, done: bool | None):
        with self.lock:
            p = self.progress()
            p.setdefault(pid, {})
            if done is None:
                p[pid].pop(step, None)
            else:
                p[pid][step] = done
            json.dump(p, open(self.progress_path, 'w'), indent=1)

    # ---------- files ----------
    def resolve_file(self, rel: str) -> str:
        """Only allow reads inside the kernel tree or this workspace."""
        roots = [self.root] + ([self.ktree.root] if self.ktree else [])
        for r in roots:
            p = os.path.realpath(os.path.join(r, rel))
            if any(p == os.path.realpath(x) or p.startswith(os.path.realpath(x) + os.sep) for x in roots) and os.path.isfile(p):
                return p
        raise FileNotFoundError(rel)


def set_yaml_fields(text: str, fields: dict) -> str:
    """Set top-level keys, or one level down with "parent.key", in YAML text, keeping comments.

    Values are written as plain scalars (quoted when needed); None means "leave it".
    """
    import re
    for key, val in fields.items():
        if val is None:
            continue
        val = str(val).strip()
        q = val if re.fullmatch(r'[\w./@:~+,-]*', val) and not re.fullmatch(r'(yes|no|on|off|true|false|null|~|[\d.]+)', val, re.I) \
            else '"' + val.replace('\\', '\\\\').replace('"', '\\"') + '"'
        if '.' in key:
            parent, sub = key.split('.', 1)
            m = re.search(rf'^{parent}:[^\n]*\n((?:[ \t]+[^\n]*\n?)*)', text, re.M)
            if not m:
                text = text.rstrip('\n') + f'\n\n{parent}:\n  {sub}: {q}\n'
                continue
            block = m.group(1)
            if re.search(rf'^[ \t]+{sub}:', block, re.M):
                block = re.sub(rf'^([ \t]+{sub}:)[ \t]*[^#\n]*?([ \t]+#[^\n]*)?$', lambda x: f'{x.group(1)} {q}' + (x.group(2) or ''), block, count=1, flags=re.M)
            else:
                block = block.rstrip('\n') + f'\n  {sub}: {q}\n'
            text = text[:m.start(1)] + block + text[m.end(1):]
        elif re.search(rf'^{key}:', text, re.M):
            text = re.sub(rf'^({key}:)[ \t]*[^#\n]*?([ \t]+#[^\n]*)?$', lambda x: f'{x.group(1)} {q}' + (x.group(2) or ''), text, count=1, flags=re.M)
        else:
            text = text.rstrip('\n') + f'\n{key}: {q}\n'
    return text


def safe_name(s: str) -> str:
    import re
    return re.sub(r'[^A-Za-z0-9_.-]', '-', s).strip('.-') or 'unnamed'
