"""Answer "compatible string -> binding doc -> driver file -> CONFIG_ symbol".

Everything is looked up in the kernel source tree you point at, so the answers
match *your* kernel (mainline or vendor BSP), not someone's memory of it.
"""
from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache


def _read(path: str) -> str:
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read()

import yaml


class KernelTree:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._binding_files: list[str] | None = None

    @property
    def ok(self) -> bool:
        return os.path.isdir(os.path.join(self.root, 'drivers'))

    def rel(self, p: str) -> str:
        return os.path.relpath(p, self.root)

    # ---------- bindings ----------
    def _bindings(self) -> list[str]:
        if self._binding_files is None:
            base = os.path.join(self.root, 'Documentation/devicetree/bindings')
            out = []
            for dp, _, fs in os.walk(base):
                out += [os.path.join(dp, f) for f in fs if f.endswith(('.yaml', '.txt'))]
            # schema'd YAML first, legacy .txt only as fallback
            self._binding_files = sorted(out, key=lambda f: not f.endswith('.yaml'))
        return self._binding_files

    @lru_cache(maxsize=256)
    def binding(self, compat: str) -> dict | None:
        token = re.compile(r'(?<![\w,.-])' + re.escape(compat) + r'(?![\w,.-])')
        for path in self._bindings():
            try:
                with open(path, encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except OSError:
                continue
            if compat not in text or not token.search(text):
                continue
            info = {'file': self.rel(path), 'format': 'yaml' if path.endswith('.yaml') else 'txt'}
            if path.endswith('.yaml'):
                try:
                    doc = yaml.safe_load(text) or {}
                except yaml.YAMLError:
                    doc = {}
                props = doc.get('properties', {}) or {}
                # only accept if the schema declares it, not if it merely appears in an example
                if compat not in str(props.get('compatible', '')) + str(doc.get('select', '')):
                    continue
                info.update({
                    'title': doc.get('title', ''),
                    'description': (doc.get('description') or '').strip()[:1200],
                    'required': doc.get('required', []) or [],
                    'properties': {k: self._prop_summary(v) for k, v in props.items()},
                    'refs': [a.get('$ref') for a in (doc.get('allOf') or []) if isinstance(a, dict) and a.get('$ref')],
                    'example': (doc.get('examples') or [''])[0],
                })
            else:
                info.update({'title': os.path.basename(path), 'description': text[:1500],
                             'required': [], 'properties': {}, 'example': ''})
            return info
        return None

    @staticmethod
    def _prop_summary(v) -> str:
        if not isinstance(v, dict):
            return ''
        d = v.get('description')
        if isinstance(d, str):
            return d.strip().split('\n')[0][:160]
        for k in ('const', 'enum', '$ref', 'maxItems'):
            if k in v:
                return f'{k}: {v[k]}'
        return ''

    # ---------- driver + Kconfig ----------
    @lru_cache(maxsize=256)
    def driver(self, compat: str, bus_hint: str = '') -> dict | None:
        roots = [d for d in ('drivers', 'sound') if os.path.isdir(os.path.join(self.root, d))]
        if not roots:
            return None
        try:
            res = subprocess.run(['grep', '-rlF', '--include=*.c', f'"{compat}"', *roots],
                                 cwd=self.root, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        files = [f for f in res.stdout.split('\n') if f]
        # chips with several bus front-ends (foo-i2c.c / foo-spi.c): prefer the one for our bus
        if bus_hint:
            files.sort(key=lambda f: bus_hint not in os.path.basename(f))
        # prefer a file that has it in an of_device_id table
        for f in files:
            src = _read(os.path.join(self.root, f))
            m = re.search(r'\.compatible\s*=\s*"' + re.escape(compat) + '"', src)
            if not m:
                continue
            name = re.search(r'\.name\s*=\s*"([^"]+)"', src[m.end():]) or \
                re.search(r'#define\s+DRV_NAME\s+"([^"]+)"', src)
            sym = self.config_for(f)
            return {
                'file': f,
                'line': src.count('\n', 0, m.start()) + 1,
                'name': name.group(1) if name else os.path.splitext(os.path.basename(f))[0],
                'config': sym,
                'kconfig': self.kconfig_entry(sym, os.path.dirname(f)) if sym else None,
            }
        return None

    def config_for(self, c_file: str) -> str | None:
        """drivers/x/foo.c -> CONFIG_FOO by reading drivers/x/Makefile."""
        d, obj = os.path.dirname(c_file), os.path.splitext(os.path.basename(c_file))[0] + '.o'
        mk = os.path.join(self.root, d, 'Makefile')
        if not os.path.isfile(mk):
            return None
        text = _read(mk).replace('\\\n', ' ')
        for _ in range(3):  # follow composite objects: foo-y += bar.o ; obj-$(CONFIG_X) += foo.o
            m = re.search(r'obj-\$\((CONFIG_\w+)\)\s*[+:]?=[^\n]*\b' + re.escape(obj), text)
            if m:
                return m.group(1)
            m = re.search(r'^\s*([\w-]+)-(?:y|objs|\$\(CONFIG_\w+\))\s*[+:]?=[^\n]*\b' + re.escape(obj), text, re.M)
            if not m:
                return None
            obj = m.group(1) + '.o'
        return None

    def kconfig_entry(self, sym: str, near_dir: str) -> dict | None:
        name = sym.removeprefix('CONFIG_')
        d = os.path.join(self.root, near_dir)
        while True:
            kc = os.path.join(d, 'Kconfig')
            if os.path.isfile(kc):
                text = _read(kc)
                m = re.search(r'^(?:menu)?config\s+' + re.escape(name) + r'\s*\n((?:[ \t]+.*\n|\s*\n)*)', text, re.M)
                if m:
                    body = m.group(1)
                    prompt = re.search(r'^\s*(?:tristate|bool)\s+"([^"]+)"', body, re.M)
                    deps = re.findall(r'^\s*depends on\s+(.+)$', body, re.M)
                    sel = re.findall(r'^\s*select\s+(\w+)', body, re.M)
                    return {'file': self.rel(kc), 'line': text.count('\n', 0, m.start()) + 1,
                            'prompt': prompt.group(1) if prompt else '',
                            'depends': deps, 'selects': sel}
            if os.path.abspath(d) == self.root or not d.startswith(self.root):
                return None
            d = os.path.dirname(d)
