#!/usr/bin/env python3
"""Build an OFFLINE demo snapshot by compiling a board .dts with cpp + dtc.

The live-DT part (nodes, compatibles, status) is what the kernel would really see.
Everything a real board adds (bound drivers, kconfig, dmesg, ALSA...) is
NOT available, so the snapshot is marked demo and those sections stay empty or
are labelled "(demo)". Use a real scan (transport: ssh) for actual bring-up.

usage: tools/make_demo_snapshot.py <kernel_src> <board.dts> <out.json>
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server.board import analyze  # noqa: E402
from server.dts import parse_file  # noqa: E402


def compile_flat(ksrc, dts):
    inc = [os.path.dirname(dts), os.path.join(ksrc, 'include'), os.path.join(ksrc, 'scripts/dtc/include-prefixes')]
    with tempfile.TemporaryDirectory() as td:
        pre, dtb, flat = (os.path.join(td, n) for n in ('pre.dts', 'out.dtb', 'flat.dts'))
        subprocess.run(['cpp', '-nostdinc', '-undef', '-D__DTS__', '-x', 'assembler-with-cpp',
                        *sum((['-I', i] for i in inc), []), '-o', pre, dts], check=True)
        subprocess.run(['dtc', '-q', '-@', '-I', 'dts', '-O', 'dtb', '-o', dtb, pre], check=True)
        subprocess.run(['dtc', '-q', '-I', 'dtb', '-O', 'dts', '-o', flat, dtb], check=True)
        return parse_file(flat), open(flat).read()


def main(ksrc, dts, out):
    import re
    pf, text = compile_flat(ksrc, os.path.join(ksrc, dts) if not os.path.isabs(dts) else dts)
    dt, symbols = [], {}
    for nd in pf.nodes:
        props = pf.own_props(nd)
        path = pf.path_of(nd)
        if nd.name == '__symbols__':
            symbols = dict(re.findall(r'([\w-]+)\s*=\s*"([^"]+)"', props))
        m = re.search(r'\bcompatible\s*=\s*([^;]+);', props)
        if not m or path.startswith('/__'):
            continue
        st = re.search(r'\bstatus\s*=\s*"([^"]+)"', props)
        dt.append({'path': path, 'compatible': re.findall(r'"([^"]+)"', m.group(1)),
                   'status': st.group(1) if st else 'okay'})
    model = re.search(r'model\s*=\s*"([^"]+)"', text)
    devices = [{'bus': 'platform', 'name': n['path'].split('/')[-1], 'driver': '(demo)', 'of_node': n['path']}
               for n in dt if n['status'] in ('okay', 'ok')]
    snap = {
        'meta': {'time': 'demo', 'source': f'compiled from {os.path.basename(dts)}', 'demo': True},
        'system': {'uname': 'Linux (demo snapshot, no real board)', 'model': model.group(1) if model else '',
                   'compatible': ''},
        'dt': sorted(dt, key=lambda x: x['path']), 'symbols': symbols, 'devices': devices, 'kconfig': {},
        'sections': {'dmesg': '', 'deferred': '', 'usb': '', 'alsa_cards': '', 'v4l2': '', 'input': '',
                     'pinmux': ''},
    }
    snap['findings'] = analyze(snap)
    with open(out, 'w') as f:
        json.dump(snap, f, indent=1)
    print(f'wrote {out}: {len(dt)} nodes, {len(symbols)} symbols')


if __name__ == '__main__':
    main(*sys.argv[1:4])
