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

    def info(self) -> dict:
        """Kernel version (from the top Makefile) and where the tree came from (from git, if any)."""
        out = {'version': None, 'origin': None, 'flavor': None}
        try:
            mk = _read(os.path.join(self.root, 'Makefile'))[:2000]
            v = {k: (re.search(rf'^{k}\s*=\s*(\S*)', mk, re.M) or [None, ''])[1] for k in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL', 'EXTRAVERSION')}
            if v['VERSION']:
                out['version'] = f"{v['VERSION']}.{v['PATCHLEVEL']}.{v['SUBLEVEL'] or 0}{v['EXTRAVERSION']}"
        except OSError:
            pass
        try:
            m = re.search(r'url\s*=\s*(\S+)', _read(os.path.join(self.root, '.git', 'config')))
            out['origin'] = m.group(1) if m else None
        except OSError:
            pass
        o = (out['origin'] or '').lower()
        out['flavor'] = next((name for key, name in [('nxp-imx', 'NXP linux-imx'), ('linux-imx', 'NXP linux-imx'),
                                                      ('torvalds', 'mainline'), ('stable', 'mainline stable'),
                                                      ('raspberrypi', 'Raspberry Pi'), ('stm32', 'ST'),
                                                      ('qualcomm', 'Qualcomm'), ('linaro', 'Linaro')] if key in o), None)
        if not out['flavor'] and 'work-shared' in self.root:
            out['flavor'] = 'Yocto kernel-source'
        return out

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

    # ---------- finding the right compatible string ----------
    def search_compatibles(self, q: str, limit: int = 12) -> list[dict]:
        """Part name (e.g. "imx219", "bme280") -> compatible strings this tree knows, with where they appear."""
        q = re.sub(r'[^A-Za-z0-9._,+-]', '', q)
        if len(q) < 2:
            return []
        core = r'([A-Za-z0-9-]+,)?[A-Za-z0-9._+-]*' + re.escape(q.split(',')[-1]) + r'[A-Za-z0-9._+-]*'
        hits: dict[str, dict] = {}
        for where, rx, args in (('binding', r'(- |const: )"?' + core, ['--include=*.yaml', 'Documentation/devicetree/bindings']),
                                ('driver', r'\.compatible\s*=\s*"' + core + '"', ['--include=*.c', 'drivers', 'sound'])):
            dirs = [a for a in args if not a.startswith('--') and os.path.isdir(os.path.join(self.root, a))]
            if not dirs:
                continue
            try:
                res = subprocess.run(['grep', '-rIoiE', *[a for a in args if a.startswith('--')], rx, *dirs],
                                     cwd=self.root, capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                continue
            for line in res.stdout.splitlines():
                f, _, c = line.partition(':')
                c = re.sub(r'^(- |const: |\.compatible\s*=\s*)', '', c).strip('"')
                if where == 'binding' and ',' not in c and c not in hits:
                    continue      # bare words in YAML lists are usually property names, not compatibles
                h = hits.setdefault(c, {'compatible': c, 'binding': None, 'driver': None})
                h[where] = h[where] or f
        out = sorted(hits.values(), key=lambda h: (not h['driver'], not h['binding'], len(h['compatible'])))
        return out[:limit]

    @staticmethod
    def guess_kind(binding_file: str | None, driver_file: str | None) -> dict:
        """Suggest bus and subsystem from where the binding/driver lives in the tree."""
        p = (binding_file or '') + ' ' + (driver_file or '')
        sub = next((s for k, s in [('/sound/', 'alsa'), ('sound/', 'alsa'), ('/media/', 'v4l2'), ('/iio/', 'iio'),
                                   ('/input/', 'input'), ('/hwmon/', 'hwmon'), ('/counter/', 'counter'),
                                   ('/serial/', 'tty')] if k in p), None)
        return {'subsystem': sub}

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


# ---------- what a driver asks the device tree for ----------
_REQ_PATTERNS = [
    # (kind, regex); group 1 = name. Works for in-tree and out-of-tree drivers alike.
    ('property', r'(?:of|device|fwnode)_property_read_\w+\s*\([^,]+,\s*"([^"]+)"'),
    ('property', r'(?:of|device|fwnode)_property_(?:present|count_\w+)\s*\([^,]+,\s*"([^"]+)"'),
    ('clock', r'(?:devm_)?clk_get(?:_optional)?(?:_enabled)?\s*\([^,]+,\s*"([^"]+)"'),
    ('clock', r'(?:devm_)?clk_get(?:_optional)?(?:_enabled)?\s*\([^,]+,\s*(NULL)\s*\)'),
    ('clock', r'devm_v4l2_sensor_clk_get(?:_legacy)?\s*\([^,]+,\s*"([^"]+)"'),     # V4L2 sensor helper (6.17+)
    ('clock', r'devm_v4l2_sensor_clk_get(?:_legacy)?\s*\([^,]+,\s*(NULL)\s*[,)]'),
    ('gpio', r'(?:devm_)?gpiod_get(?:_optional)?(?:_index)?\s*\([^,]+,\s*"([^"]+)"'),
    ('supply', r'(?:devm_)?regulator_get(?:_optional)?\s*\([^,]+,\s*"([^"]+)"'),
]


def driver_requirements(text: str) -> dict:
    """Read a driver's C source and list what it looks up from the device tree.

    This is the most honest answer to "which properties does my node need?":
    it is what the code actually asks for, so it also works for drivers that
    have no binding document (out-of-tree vendor drivers).
    """
    defines = dict(re.findall(r'^#define\s+(\w+)\s+\(?\s*(\d+)(?:U?LL?)?\s*\)?\s*$', text, re.M))
    num = lambda tok: int(defines.get(tok.strip(), tok.strip())) if re.fullmatch(r'\d+', defines.get(tok.strip(), tok.strip())) else None
    req = {'compatibles': re.findall(r'\.compatible\s*=\s*"([^"]+)"', text),
           'property': [], 'clock': [], 'gpio': [], 'supply': [], 'optional': [],
           'endpoint': False, 'irq': False, 'lanes': [], 'bus': None}
    for kind, rx in _REQ_PATTERNS:
        for m in re.finditer(rx, text):
            name = '(unnamed, the first in `clocks`)' if m.group(1) == 'NULL' else m.group(1)
            if name not in req[kind]:
                req[kind].append(name)
            if '_optional' in m.group(0) and name not in req['optional']:
                req['optional'].append(name)
    # regulator_bulk_get with a static name table: static const char * const foo_supply_name[] = {"VANA", ...};
    for m in re.finditer(r'(?:supply_names?|supplies)\s*\[\s*\w*\s*\]\s*=\s*\{([^}]*)\}', text):
        for s in re.findall(r'"([^"]+)"', m.group(1)):
            if s not in req['supply']:
                req['supply'].append(s)
    req['endpoint'] = bool(re.search(r'v4l2_fwnode_endpoint_(?:alloc_)?parse|of_graph_get_|fwnode_graph_get_next_endpoint', text))
    req['link_frequencies'] = 'link_frequencies' in text or 'link-frequencies' in text
    # link-frequency values the driver accepts (s64 foo_link_freq_menu[] = { 408000000, MACRO })
    freqs = []
    for m in re.finditer(r'link_freq\w*\s*\[\s*\]\s*=\s*\{([^}]*)\}', text):
        for tok in re.findall(r'[\w]+', m.group(1)):
            v = num(tok)
            if v and v >= 1_000_000 and v not in freqs:       # skip array indices and enum values
                freqs.append(v)
    req['link_freq_values'] = freqs
    # input clock the driver insists on (if (rate != FOO_XCLK_FREQ) return -EINVAL)
    m = re.search(r'(?:xclk|inclk|extclk|mclk)\w*\)?\s*!=\s*(\w+)', text, re.I)
    req['clock_hz'] = num(m.group(1)) if m else None
    req['irq'] = bool(re.search(r'client->irq|platform_get_irq|of_irq_get|spi->irq', text))
    req['lanes'] = sorted(set(re.findall(r'num_data_lanes\s*!=\s*(\d)', text)))
    if re.search(r'module_i2c_driver|i2c_add_driver|struct i2c_driver', text):
        req['bus'] = 'i2c'
    elif re.search(r'module_spi_driver|struct spi_driver', text):
        req['bus'] = 'spi'
    elif re.search(r'module_platform_driver|struct platform_driver', text):
        req['bus'] = 'platform'
    # Kconfig `select`s the code needs, recognised from the helper APIs it calls
    req['selects'] = [sym for rx, sym in [
        (r'\bcci_(?:read|write)|devm_cci_regmap_init', 'V4L2_CCI_I2C'),
        (r'devm_regmap_init_i2c|regmap_init_i2c', 'REGMAP_I2C'),
        (r'devm_regmap_init_spi|regmap_init_spi', 'REGMAP_SPI'),
        (r'v4l2_fwnode_', 'V4L2_FWNODE'),
        (r'media_entity_pads_init', 'MEDIA_CONTROLLER'),
        (r'v4l2_subdev_state|V4L2_SUBDEV_FL_HAS_DEVNODE', 'VIDEO_V4L2_SUBDEV_API'),
    ] if re.search(rx, text)]
    if 'V4L2_CCI_I2C' in req['selects']:          # CCI already pulls in regmap
        req['selects'].remove('REGMAP_I2C')
    # API hints that usually break when moving a driver between kernel versions
    req['port_hints'] = [msg for rx, msg in [
        (r'\.probe_new\s*=', '`.probe_new` was folded back into `.probe` (6.3+): rename it'),
        (r'probe\s*\(\s*struct i2c_client\s*\*\s*\w+\s*,\s*const struct i2c_device_id', 'I2C probe() lost its i2c_device_id argument (6.3+)'),
        (r'static\s+int\s+\w+_remove\s*\(\s*struct i2c_client', 'I2C remove() must return void (6.1+)'),
        (r'struct v4l2_subdev_pad_config', 'v4l2_subdev_pad_config was replaced by v4l2_subdev_state (5.14+)'),
        (r'v4l2_subdev_get_try_(?:format|crop|compose)', 'v4l2_subdev_get_try_*() became v4l2_subdev_state_get_*() (6.8+)'),
        (r'v4l2_subdev_state_get_(?:format|crop)', 'uses v4l2_subdev_state_get_*() (6.8+ API): on an older kernel, swap back to v4l2_subdev_get_try_*()'),
        (r'\.g_frame_interval|\.s_frame_interval', 'g/s_frame_interval moved from video ops to pad ops (6.8+)'),
    ] if re.search(rx, text)]
    return req
