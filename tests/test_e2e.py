"""End-to-end: generated snippets must compile with dtc, and source checks must turn green.

    BS_KERNEL=/path/to/linux python3 -m unittest tests.test_e2e -v

Applies every peripheral's snippets to a temporary copy of the board .dts (the
original is never touched), compiles it with cpp + dtc, then re-plans against the
copy and expects every source-side step to pass.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from server.dts import parse_file
from server.planner import Knowledge, plan
from server.workspace import Workspace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KERNEL = os.environ.get('BS_KERNEL')
SOURCE_LAYERS = {'pinmux', 'controller', 'node'}


def insert_snippet(text: str, anchor_ref: str, snippet: str) -> str:
    """Put a snippet where the guide says: inside the anchor block, or as a new top-level block."""
    wrapper = snippet.lstrip().startswith(('&', '/ {')) and snippet.lstrip().split()[0] == anchor_ref
    if wrapper or not anchor_ref:
        return text.rstrip('\n') + '\n\n' + snippet + '\n'
    tmp = tempfile.NamedTemporaryFile('w', suffix='.dts', delete=False)
    tmp.write(text); tmp.close()
    try:
        pf = parse_file(tmp.name)
    finally:
        os.unlink(tmp.name)
    for nd in pf.nodes:
        if nd.parent is None and (nd.name == anchor_ref or (anchor_ref.startswith('&') and nd.ref == anchor_ref[1:])):
            body = '\n'.join('\t' + l if l else l for l in snippet.split('\n'))
            return text[:nd.end] + '\n' + body + '\n' + text[nd.end:]
    # anchor block doesn't exist yet: create it
    return text.rstrip('\n') + f'\n\n{anchor_ref} {{\n' + '\n'.join('\t' + l for l in snippet.split('\n')) + '\n};\n'


def compile_dts(ksrc, dts):
    inc = [os.path.dirname(dts), os.path.join(ksrc, 'include'), os.path.join(ksrc, 'scripts/dtc/include-prefixes')]
    with tempfile.TemporaryDirectory() as td:
        pre = os.path.join(td, 'pre.dts')
        subprocess.run(['cpp', '-nostdinc', '-undef', '-D__DTS__', '-x', 'assembler-with-cpp',
                        *sum((['-I', i] for i in inc), []), '-o', pre, dts], check=True, capture_output=True)
        r = subprocess.run(['dtc', '-I', 'dts', '-O', 'dtb', '-o', os.devnull, pre], capture_output=True, text=True)
        return r.returncode, r.stderr


@unittest.skipUnless(KERNEL and os.path.isdir(KERNEL or ''), 'set BS_KERNEL to a kernel tree')
class E2E(unittest.TestCase):
    def setUp(self):
        self.ws_dir = tempfile.mkdtemp()
        for d in ('peripherals', 'snapshots'):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(self.ws_dir, d))
        self.board_rel = 'arch/arm64/boot/dts/freescale/imx93-11x11-frdm.dts'
        self.copy_rel = self.board_rel.replace('.dts', '-bstest.dts')
        shutil.copy(os.path.join(KERNEL, self.board_rel), os.path.join(KERNEL, self.copy_rel))
        self.kb = Knowledge(os.path.join(ROOT, 'knowledge'))
        self.write_ws(self.copy_rel)

    def tearDown(self):
        os.unlink(os.path.join(KERNEL, self.copy_rel))
        shutil.rmtree(self.ws_dir)

    def write_ws(self, board):
        with open(os.path.join(self.ws_dir, 'workspace.yaml'), 'w') as f:
            f.write(f'soc: imx93\nkernel_src: {KERNEL}\nboard_dts: {board}\n'
                    f'board: {{transport: snapshot, snapshot: snapshots/demo-imx93-frdm.json}}\n')
        return Workspace(self.ws_dir)

    def test_snippets_compile_and_checks_pass(self):
        ws = Workspace(self.ws_dir)
        path = os.path.join(KERNEL, self.copy_rel)
        rc, err = compile_dts(KERNEL, path)
        self.assertEqual(rc, 0, f'pristine board must compile first: {err}')
        norm = lambda e: {re.sub(r'^\S+:\d+\.\d+-\d+\.\d+: ', '', w) for w in e.strip().split('\n') if w}
        baseline_warnings = norm(err)

        for pid in ('i2s-mic', 'motor-encoder', 'usb-camera'):
            p = plan(ws.periph_form(pid), self.kb, ws, {})
            for st in p['steps']:
                if st['layer'] in SOURCE_LAYERS and st['snippet'] and st['result']['status'] != 'pass':
                    text = open(path).read()
                    open(path, 'w').write(insert_snippet(text, (st.get('anchor') or {}).get('ref'), st['snippet']))

        rc, err = compile_dts(KERNEL, path)
        new_warnings = sorted(norm(err) - baseline_warnings)
        self.assertEqual(rc, 0, f'dtc failed on generated snippets:\n{err}\n----\n{open(path).read()[-3000:]}')
        print('\nnew dtc warnings:', new_warnings or 'none')

        ws = self.write_ws(self.copy_rel)
        for pid in ('i2s-mic', 'motor-encoder', 'usb-camera'):
            p = plan(ws.periph_form(pid), self.kb, ws, {})
            for st in p['steps']:
                if st['layer'] in SOURCE_LAYERS | {'hardware'} and st['check'].get('type') not in ('manual', 'scan'):
                    self.assertEqual(st['result']['status'], 'pass', f"{pid}/{st['id']}: {st['result']}")


if __name__ == '__main__':
    unittest.main()
