"""Talk to the board and take a snapshot of what Linux currently sees.

Transports:
  ssh       - run commands over ssh (uses a ControlMaster socket so repeated
              calls are fast). Needs key-based login: ssh-copy-id root@board
  local     - run on this machine (use when the tool runs on the board itself)
  snapshot  - offline: read a saved scan JSON, cannot run live commands
"""
from __future__ import annotations

import json
import re
import os
import shlex
import subprocess
import time

# POSIX sh (busybox-safe). Each section starts with "===S name===".
SCAN_SCRIPT = r'''
B=/sys/firmware/devicetree/base
S() { echo "===S $1==="; }
S uname; uname -a
S model; [ -f $B/model ] && tr -d '\000' < $B/model; echo
S compatible; [ -f $B/compatible ] && tr '\000' ' ' < $B/compatible; echo
S dt
find $B -name compatible -type f 2>/dev/null | while read -r f; do
  d=${f%/compatible}; st=okay
  [ -f "$d/status" ] && st=$(tr -d '\000' < "$d/status")
  printf '%s\t%s\t%s\n' "${d#$B}" "$(tr '\000' '|' < "$f")" "$st"
done
S symbols
[ -d $B/__symbols__ ] && for f in $B/__symbols__/*; do printf '%s\t%s\n' "${f##*/}" "$(tr -d '\000' < "$f")"; done
S devices
for d in /sys/bus/*/devices/*; do
  [ -e "$d" ] || continue
  bus=${d#/sys/bus/}; bus=${bus%%/*}; drv=; of=
  [ -e "$d/driver" ] && drv=$(basename "$(readlink "$d/driver")")
  [ -e "$d/of_node" ] && of=$(readlink -f "$d/of_node")
  printf '%s\t%s\t%s\t%s\n' "$bus" "${d##*/}" "$drv" "${of#$B}"
done
S deferred; cat /sys/kernel/debug/devices_deferred 2>/dev/null
S kconfig; (zcat /proc/config.gz 2>/dev/null || cat /boot/config-$(uname -r) 2>/dev/null) | grep -E '^CONFIG_[A-Za-z0-9_]+=[ym]'
S modules; cut -d' ' -f1 /proc/modules 2>/dev/null
S dmesg; dmesg 2>/dev/null | grep -iE 'error|fail|defer|unable|invalid|not found|timeout|missing|-EPROBE|probe|unknown symbol|disagrees about version|module format' | tail -n 300
S alsa_cards; cat /proc/asound/cards 2>/dev/null
S alsa_pcm; cat /proc/asound/pcm 2>/dev/null
S v4l2; for d in /sys/class/video4linux/*; do [ -e "$d" ] && printf '%s\t%s\n' "${d##*/}" "$(cat "$d/name" 2>/dev/null)"; done
S iio; for d in /sys/bus/iio/devices/iio:device*; do [ -e "$d" ] && printf '%s\t%s\t%s\n' "${d##*/}" "$(cat "$d/name" 2>/dev/null)" "$(ls "$d" | grep -E '^(in|out)_' | tr '\n' ' ')"; done
S input; cat /proc/bus/input/devices 2>/dev/null
S counter; for d in /sys/bus/counter/devices/counter*; do [ -e "$d" ] && printf '%s\t%s\n' "${d##*/}" "$(cat "$d/name" 2>/dev/null)"; done
S hwmon; for d in /sys/class/hwmon/hwmon*; do [ -e "$d" ] && printf '%s\t%s\n' "${d##*/}" "$(cat "$d/name" 2>/dev/null)"; done
S i2c; for d in /sys/bus/i2c/devices/*; do [ -e "$d" ] && printf '%s\t%s\n' "${d##*/}" "$(cat "$d/name" 2>/dev/null)"; done
S spi; for d in /sys/bus/spi/devices/*; do [ -e "$d" ] && printf '%s\t%s\n' "${d##*/}" "$(cat "$d/modalias" 2>/dev/null)"; done
S usb; for d in /sys/bus/usb/devices/*; do [ -f "$d/idVendor" ] && printf '%s\t%s:%s\t%s\t%s\n' "${d##*/}" "$(cat "$d/idVendor")" "$(cat "$d/idProduct")" "$(cat "$d/manufacturer" 2>/dev/null)" "$(cat "$d/product" 2>/dev/null)"; done
S net; for d in /sys/class/net/*; do [ -e "$d" ] && printf '%s\t%s\n' "${d##*/}" "$(cat "$d/operstate" 2>/dev/null)"; done
S pwm; for d in /sys/class/pwm/pwmchip*; do [ -e "$d" ] && printf '%s\t%s\t%s\n' "${d##*/}" "$(cat "$d/npwm")" "$(basename "$(readlink -f "$d/device")")"; done
S remoteproc; for d in /sys/class/remoteproc/*; do [ -e "$d" ] && printf '%s\t%s\t%s\n' "${d##*/}" "$(cat "$d/name" 2>/dev/null)" "$(cat "$d/state" 2>/dev/null)"; done
S pinmux; cat /sys/kernel/debug/pinctrl/*/pinmux-pins 2>/dev/null
S gpio; cat /sys/kernel/debug/gpio 2>/dev/null
S end
'''


class Transport:
    live = True

    def run(self, cmd: str, timeout: int = 30, stdin: str | None = None) -> tuple[int, str, str]:
        raise NotImplementedError

    def popen(self, cmd: str) -> subprocess.Popen:
        raise NotImplementedError

    def describe(self) -> str:
        return self.__class__.__name__


class LocalTransport(Transport):
    def run(self, cmd, timeout=30, stdin=None):
        p = subprocess.run(['sh', '-c', cmd], input=stdin, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    def popen(self, cmd):
        return subprocess.Popen(['sh', '-c', cmd], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def describe(self):
        return 'local'


class SSHTransport(Transport):
    def __init__(self, host: str, opts: list[str] | None, control_dir: str):
        self.host = host
        # The control socket path must fit in 108 bytes (Unix socket limit), and while creating it ssh
        # appends a 17-char temp suffix to "cm-<40 hex>". Workspace paths are rarely short enough, so
        # always use a short private dir.
        import tempfile
        control_dir = os.path.join(tempfile.gettempdir(), f'bs-ssh-{os.getuid()}')
        os.makedirs(control_dir, mode=0o700, exist_ok=True)
        self.base = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                     '-o', 'ControlMaster=auto', '-o', f'ControlPath={control_dir}/cm-%C',
                     '-o', 'ControlPersist=120', *(opts or []), host]

    def run(self, cmd, timeout=30, stdin=None):
        try:
            p = subprocess.run(self.base + [cmd], input=stdin, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, '', f'timeout after {timeout}s'
        return p.returncode, p.stdout, p.stderr

    def popen(self, cmd):
        return subprocess.Popen(self.base + [cmd], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def describe(self):
        return f'ssh {self.host}'


class SnapshotTransport(Transport):
    live = False

    def __init__(self, path: str):
        self.path = path

    def run(self, cmd, timeout=30, stdin=None):
        return 1, '', 'offline snapshot mode: no board connected'

    def describe(self):
        return f'snapshot {os.path.basename(self.path)}'


def make_transport(cfg: dict, state_dir: str) -> Transport:
    kind = cfg.get('transport', 'snapshot')
    if kind == 'ssh':
        return SSHTransport(cfg['host'], cfg.get('ssh_opts'), os.path.join(state_dir, 'ssh'))
    if kind == 'local':
        return LocalTransport()
    return SnapshotTransport(cfg.get('snapshot', ''))


# ---------------------------------------------------------------- parsing
def _rows(text: str, n: int) -> list[list[str]]:
    out = []
    for line in text.split('\n'):
        if not line.strip():
            continue
        parts = line.split('\t')
        out.append((parts + [''] * n)[:n])
    return out


def parse_scan(raw: str) -> dict:
    sections: dict[str, str] = {}
    cur = None
    buf: list[str] = []
    for line in raw.split('\n'):
        if line.startswith('===S ') and line.endswith('==='):
            if cur:
                sections[cur] = '\n'.join(buf).strip('\n')
            cur, buf = line[5:-3], []
        else:
            buf.append(line)
    if cur and cur != 'end':
        sections[cur] = '\n'.join(buf)

    dt = [{'path': p, 'compatible': [c for c in comp.split('|') if c], 'status': st}
          for p, comp, st in _rows(sections.get('dt', ''), 3)]
    devices = [{'bus': b, 'name': n, 'driver': d, 'of_node': o}
               for b, n, d, o in _rows(sections.get('devices', ''), 4)]
    kconfig = {}
    for line in sections.get('kconfig', '').split('\n'):
        if '=' in line:
            k, v = line.split('=', 1)
            kconfig[k] = v
    snap = {
        'system': {k: sections.get(k, '').strip() for k in ('uname', 'model', 'compatible')},
        'dt': sorted(dt, key=lambda x: x['path']),
        'symbols': dict(_rows(sections.get('symbols', ''), 2)),
        'devices': devices,
        'kconfig': kconfig,
        'sections': {k: v for k, v in sections.items() if k not in ('dt', 'devices', 'kconfig', 'symbols')},
    }
    snap['findings'] = analyze(snap)
    return snap


# Enabled nodes that never get a driver, by design. Listing them as "not bound" would bury real problems.
_NO_DRIVER_BY_DESIGN = [
    (lambda n, devs: any(d['bus'] == 'cpu' for d in devs), 'CPU core: run by the kernel core, never bound to a driver'),
    (lambda n, devs: 'simple-bus' in n['compatible'], 'bus container: only groups child nodes, has no driver of its own'),
    (lambda n, devs: 'syscon' in n['compatible'], 'syscon: a shared register block other drivers use via regmap'),
]


# used when there is no kernel tree to read *_OF_DECLARE from: the ones on nearly every arm64 board
_EARLY_FALLBACK = {'arm,armv8-timer', 'arm,armv7-timer', 'nxp,sysctr-timer', 'arm,gic-v3', 'arm,cortex-a15-gic',
                   'arm,gic-400', 'fsl,imx93-anatop', 'fsl,imx8mp-anatop'}


def analyze(snap: dict, early_init: set | None = None) -> dict:
    """The classic bring-up failure: a node is enabled but nothing bound to it.

    early_init: compatibles the kernel sets up with *_OF_DECLARE (timers, irqchips, early
    clocks), which never show a bound driver. Comes from the kernel tree when we have one.
    """
    by_of: dict[str, list[dict]] = {}
    for d in snap['devices']:
        if d['of_node']:
            by_of.setdefault(d['of_node'], []).append(d)
    dmesg = snap['sections'].get('dmesg', '').split('\n')
    unbound, expected, no_device, bound = [], [], [], 0
    for n in snap['dt']:
        if n['status'] not in ('okay', 'ok'):
            continue
        devs = by_of.get(n['path'], [])
        if not devs:
            no_device.append(n['path'])
        elif not any(d['driver'] for d in devs):
            entry = {'path': n['path'], 'compatible': n['compatible'],
                     'devices': [d['bus'] + '/' + d['name'] for d in devs]}
            why = next((msg for test, msg in _NO_DRIVER_BY_DESIGN if test(n, devs)), None)
            if not why and any(c in (early_init or _EARLY_FALLBACK) for c in n['compatible']):
                why = 'set up early by the kernel (an *_OF_DECLARE timer, clock or irqchip), not by a normal driver'
            if why:
                expected.append(dict(entry, why=why))
                continue
            # dmesg lines about this device: by its device name (2-0034) or the chip part (imx519)
            names = {d['name'] for d in devs}
            parts = {c.split(',')[-1] for c in n['compatible'] if len(c.split(',')[-1]) > 3}
            # its own messages first ("adp5585 2-0034: ...", "imx519: ..."), then others that mention it
            own = [l.strip() for l in dmesg if any(re.search(rf'\b{re.escape(k)}\b:', l) for k in names | parts)]
            other = [l.strip() for l in dmesg if l.strip() not in own and any(k in l for k in names | parts)]
            entry['dmesg'] = own[-3:] + other[-1:]
            unbound.append(entry)
        else:
            bound += 1
    deferred = [l for l in snap['sections'].get('deferred', '').split('\n') if l.strip()]
    return {'bound': bound, 'unbound': unbound, 'expected_unbound': expected, 'no_device': no_device,
            'deferred': deferred,
            'enabled': sum(1 for n in snap['dt'] if n['status'] in ('okay', 'ok')),
            'disabled': sum(1 for n in snap['dt'] if n['status'] not in ('okay', 'ok'))}


def scan(t: Transport) -> dict:
    rc, out, err = t.run('sh -s', timeout=90, stdin=SCAN_SCRIPT) if not isinstance(t, LocalTransport) \
        else t.run(SCAN_SCRIPT, timeout=90)
    if '===S uname===' not in out:
        raise RuntimeError(err.strip() or f'scan failed (rc={rc})')
    snap = parse_scan(out)
    snap['meta'] = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'source': t.describe(), 'demo': False}
    return snap


def load_snapshot(path: str) -> dict | None:
    if path and os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return None


def sysfs_stream_cmd(paths: list[str], hz: float) -> str:
    """Shell loop that prints one tab-separated sample line per tick."""
    period = max(0.02, 1.0 / max(hz, 0.1))
    cats = ' '.join(f'"$(cat {shlex.quote(p)} 2>/dev/null)"' for p in paths)
    fmt = '\\t'.join(['%s'] * len(paths))
    return f"while true; do printf '{fmt}\\n' {cats}; sleep {period:.3f}; done"


# ---------------------------------------------------------------- connection test
_SSH_HINTS = [
    # (pattern in ssh's error, what it means, what to do)
    (r'Permission denied', 'The board answered, but rejected your key.',
     'Copy your key once with ssh-copy-id {host} (it asks for the board password one last time). '
     'No key yet? Create one first: ssh-keygen -t ed25519'),
    (r'Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED', 'Your laptop does not trust this board yet, or the board was reflashed and has a new identity.',
     'Run ssh {host} once in a terminal and answer "yes". If it was reflashed: ssh-keygen -R {hostname}, then try again'),
    (r'Could not resolve hostname', 'That name is not known on your network.', 'Use the IP address from "ip addr" on the board\'s serial console'),
    (r'Connection refused', 'The board is reachable, but no SSH server is listening on it.',
     'On the board: check the SSH service is running (systemctl status sshd, or dropbear on small images)'),
    (r'timed out|No route to host|Network is unreachable|timeout after', 'Nothing answered at that address.',
     'Check the IP with "ip addr" on the board (it may have changed after a reboot), that the Ethernet cable or Wi-Fi is connected, '
     'and that your laptop is on the same network. A direct cable needs a fixed IP on both ends'),
]


def test_connection(cfg: dict, state_dir: str) -> dict:
    """Try the configured transport once and explain any failure in plain words."""
    kind = cfg.get('transport', 'snapshot')
    probe = "uname -r; cat /proc/device-tree/model 2>/dev/null | tr -d '\\0'; echo; id -u; " \
            "test -r /proc/config.gz && echo cfg; test -d /sys/kernel/debug/pinctrl && echo dbg; true"
    if kind == 'snapshot':
        p = cfg.get('snapshot') or ''
        return {'ok': os.path.isfile(p), 'summary': f'snapshot file {"found" if os.path.isfile(p) else "NOT found"}: {p}'}
    if kind == 'ssh' and not cfg.get('host'):
        return {'ok': False, 'summary': 'No SSH target set', 'fix': 'Fill in the SSH target, like root@192.168.1.50'}
    t = make_transport(cfg, state_dir)
    rc, out, err = t.run(probe, timeout=12)
    if rc != 0:
        res = {'ok': False, 'summary': 'Could not run a command on the board', 'detail': (err or out).strip()[-400:]}
        host = cfg.get('host', '')
        for rx, what, fix in _SSH_HINTS:
            if re.search(rx, err or '', re.I):
                res.update(summary=what, fix=fix.format(host=host, hostname=host.split('@')[-1]))
                break
        return res
    lines = out.split('\n')
    flags = set(l.strip() for l in lines[3:])
    notes = []
    if lines[2:3] and lines[2].strip() != '0':
        notes.append('You are not logged in as root, so some board information (debugfs, dmesg) may be hidden. Use root@…')
    if 'cfg' not in flags:
        notes.append('/proc/config.gz is missing, so kernel-config checks will show "?" (needs CONFIG_IKCONFIG_PROC=y)')
    if 'dbg' not in flags:
        notes.append('debugfs is not mounted, so there is no pin-ownership or deferred-probe info. On the board: mount -t debugfs none /sys/kernel/debug')
    return {'ok': True, 'summary': f'Connected: {lines[1].strip() or "board"}, kernel {lines[0].strip()}', 'notes': notes}
