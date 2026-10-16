"""Lightweight device-tree *source* indexer.

This is not a full DTS compiler. It does just enough to answer the questions a
bring-up guide needs:

  * which files does my board .dts pull in, and in what order?
  * where is label X defined, and where is it overridden (&X { ... })?
  * what is the *effective* status of &X after all overrides?
  * is compatible "foo,bar" / pinctrl group / some text already present?
  * show me how other boards in the same directory use &X or "foo,bar".

Later files override earlier ones, exactly like dtc does after cpp has
concatenated the include chain.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

INCLUDE_RE = re.compile(r'^\s*(?:#include|/include/)\s+([<"])([^>"]+)[>"]', re.M)
STATUS_RE = re.compile(r'\bstatus\s*=\s*"([^"]+)"\s*;')
HEADER_RE = re.compile(
    r'^\s*((?:[A-Za-z_][\w]*\s*:\s*)*)'      # labels "a: b: "
    r'(&[A-Za-z_][\w]*|&\{[^}]*\}|/|[\w,.+@-]+)\s*$'  # &ref, /, or node-name
)


def strip_comments(text: str) -> str:
    """Blank out comments but keep offsets/newlines so line numbers stay valid."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True; out.append(c); i += 1
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r'[^\n]', ' ', text[i:j])); i = j
        elif text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            out.append(' ' * (j - i)); i = j
        else:
            out.append(c); i += 1
    return ''.join(out)


@dataclass
class Node:
    file: str
    name: str            # node name, "/" or "&label"
    labels: list[str]
    start: int           # offset of header start
    open: int            # offset of "{"
    end: int             # offset of matching "}"
    line: int
    parent: "Node | None" = None
    children: list["Node"] = field(default_factory=list)

    @property
    def ref(self) -> str | None:
        return self.name[1:] if self.name.startswith('&') and not self.name.startswith('&{') else None


@dataclass
class ParsedFile:
    path: str
    text: str            # original text
    clean: str           # comments blanked
    nodes: list[Node]    # flat, document order

    def line_of(self, off: int) -> int:
        return self.text.count('\n', 0, off) + 1

    def body(self, node: Node) -> str:
        return self.text[node.start:node.end + 1]

    def own_props(self, node: Node) -> str:
        """Text of the node body with child blocks removed (depth-1 props only)."""
        s, pos = [], node.open + 1
        for ch in node.children:
            s.append(self.clean[pos:ch.start]); pos = ch.end + 1
        s.append(self.clean[pos:node.end])
        return ''.join(s)

    def path_of(self, node: Node) -> str:
        parts = []
        n = node
        while n is not None:
            parts.append(n.name); n = n.parent
        parts.reverse()
        if parts and parts[0] == '/':
            return '/' + '/'.join(parts[1:])
        return '/'.join(parts)


def parse_file(path: str) -> ParsedFile:
    with open(path, encoding='utf-8', errors='replace') as f:
        text = f.read()
    clean = strip_comments(text)
    nodes: list[Node] = []
    stack: list[Node] = []
    seg_start = 0     # start of current statement (after last ; { or })
    i, n = 0, len(clean)
    while i < n:
        c = clean[i]
        if c == '"':
            j = i + 1
            while j < n and clean[j] != '"':
                j += 2 if clean[j] == '\\' else 1
            i = j + 1; continue
        if c == '{':
            header = clean[seg_start:i]
            # drop preprocessor lines that sit between statements
            header_lines = [l for l in header.split('\n') if not l.lstrip().startswith('#')]
            header = '\n'.join(header_lines).strip()
            m = HEADER_RE.match(header.replace('\n', ' '))
            if m and '=' not in header:
                labels = [l.strip() for l in m.group(1).split(':') if l.strip()]
                # header starts at the first line that isn't blank or a preprocessor line
                hs, off = seg_start, seg_start
                for ln in clean[seg_start:i].split('\n'):
                    if ln.strip() and not ln.lstrip().startswith('#'):
                        hs = off + len(ln) - len(ln.lstrip())
                        break
                    off += len(ln) + 1
                node = Node(path, m.group(2), labels, hs, i, -1, 0,
                            parent=stack[-1] if stack else None)
                node.line = text.count('\n', 0, hs) + 1
                if stack:
                    stack[-1].children.append(node)
                nodes.append(node)
                stack.append(node)
            else:
                # e.g. "prop = <...{...}>" is not legal DTS; treat as anonymous block
                stack.append(Node(path, '?', [], seg_start, i, -1, 0, parent=stack[-1] if stack else None))
            seg_start = i + 1
        elif c == '}':
            if stack:
                stack.pop().end = i
            seg_start = i + 1
        elif c == ';':
            seg_start = i + 1
        i += 1
    for nd in nodes:
        if nd.end < 0:
            nd.end = n - 1
    return ParsedFile(path, text, clean, nodes)


class DtsIndex:
    """Index of a board .dts and everything it includes."""

    def __init__(self, kernel_root: str, board_dts: str, dts_dir: str):
        self.kernel_root = os.path.abspath(kernel_root)
        self.board_dts = os.path.join(self.kernel_root, board_dts) if not os.path.isabs(board_dts) else board_dts
        self.dts_dir = os.path.join(self.kernel_root, dts_dir) if not os.path.isabs(dts_dir) else dts_dir
        self.include_dirs = [
            self.dts_dir,
            os.path.join(self.kernel_root, 'include'),
            os.path.join(self.kernel_root, 'scripts/dtc/include-prefixes'),
            os.path.join(self.kernel_root, 'arch/arm64/boot/dts'),
            os.path.join(self.kernel_root, 'arch/arm/boot/dts'),
        ]
        self.files: list[ParsedFile] = []       # include order, DTS/DTSI only
        self.headers: list[str] = []            # .h files pulled in
        self.include_tree: list[dict] = []      # [{file, depth}]
        self._cache: dict[str, ParsedFile] = {}
        self.reload()

    # ---------- loading ----------
    def reload(self):
        self.files, self.headers, self.include_tree = [], [], []
        self._cache.clear()
        if os.path.isfile(self.board_dts):
            self._walk(self.board_dts, 0, set())

    def _resolve(self, cur: str, kind: str, name: str) -> str | None:
        cands = []
        if kind == '"':
            cands.append(os.path.join(os.path.dirname(cur), name))
        cands += [os.path.join(d, name) for d in self.include_dirs]
        for c in cands:
            if os.path.isfile(c):
                return os.path.normpath(c)
        return None

    def _walk(self, path: str, depth: int, seen: set):
        if path in seen:
            return
        seen.add(path)
        self.include_tree.append({'file': self.rel(path), 'depth': depth})
        if path.endswith('.h'):
            self.headers.append(path)
            return
        pf = self._parse(path)
        # includes are processed at their position: children first, then the rest of
        # this file. Approximate by handling all includes before this file's nodes,
        # which matches the common "includes at top" style.
        for m in INCLUDE_RE.finditer(pf.clean):
            inc = self._resolve(path, m.group(1), m.group(2))
            if inc:
                self._walk(inc, depth + 1, seen)
            else:
                self.include_tree.append({'file': m.group(2), 'depth': depth + 1, 'missing': True})
        self.files.append(pf)

    def _parse(self, path: str) -> ParsedFile:
        if path not in self._cache:
            self._cache[path] = parse_file(path)
        return self._cache[path]

    def rel(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.kernel_root)
        except ValueError:
            return path

    @property
    def ok(self) -> bool:
        return bool(self.files)

    # ---------- queries ----------
    def blocks_for(self, label: str) -> list[tuple[ParsedFile, Node, str]]:
        """All blocks that define or override &label, in effective order."""
        out = []
        for pf in self.files:
            for nd in pf.nodes:
                if label in nd.labels:
                    out.append((pf, nd, 'define'))
                elif nd.ref == label:
                    out.append((pf, nd, 'override'))
        return out

    def label_info(self, label: str) -> dict:
        blocks = self.blocks_for(label)
        status, where = None, None
        for pf, nd, _ in blocks:
            m = STATUS_RE.search(pf.own_props(nd))
            if m:
                status, where = m.group(1), {'file': self.rel(pf.path), 'line': pf.line_of(nd.open + m.start())}
        return {
            'label': label,
            'exists': bool(blocks),
            'status': status or ('okay' if blocks else None),
            'status_set_at': where,
            'blocks': [{'file': self.rel(pf.path), 'line': nd.line, 'kind': k,
                        'path': pf.path_of(nd) if k == 'define' else '&' + label}
                       for pf, nd, k in blocks],
        }

    def board_block(self, ref: str) -> dict | None:
        """The `&ref {` (or `/ {`) block in the board file itself, if any."""
        pf = self.files[-1] if self.files else None
        if not pf:
            return None
        for nd in pf.nodes:
            if nd.parent is None and (nd.name == ref or (ref.startswith('&') and nd.ref == ref[1:])):
                return {'file': self.rel(pf.path), 'line': nd.line,
                        'end_line': pf.line_of(nd.end), 'text': pf.body(nd)}
        return None

    def search(self, pattern: str, flags=0) -> list[dict]:
        rx = re.compile(pattern, flags)
        hits = []
        for pf in self.files:
            for m in rx.finditer(pf.clean):
                ln = pf.line_of(m.start())
                hits.append({'file': self.rel(pf.path), 'line': ln,
                             'text': pf.text.split('\n')[ln - 1].strip(),
                             'node': self._enclosing_name(pf, m.start())})
        return hits

    def find_compatible(self, compat: str) -> list[dict]:
        return self.search(r'compatible\s*=[^;]*"' + re.escape(compat) + '"')

    def node_body_for_compatible(self, compat: str) -> dict | None:
        """Comment-free body (children included) of the node that carries this compatible."""
        rx = re.compile(r'compatible\s*=[^;]*"' + re.escape(compat) + '"')
        for pf in self.files:
            for m in rx.finditer(pf.clean):
                nd = None
                for n in pf.nodes:
                    if n.open <= m.start() <= n.end and (nd is None or n.open > nd.open):
                        nd = n
                if nd:
                    return {'file': self.rel(pf.path), 'line': nd.line, 'text': pf.clean[nd.start:nd.end + 1]}
        return None

    def owners(self, pattern: str, exclude_label: str = '') -> list[dict]:
        """Devices (nearest ancestor node with a `compatible`) whose body matches pattern."""
        rx, out, seen = re.compile(pattern), [], set()
        for pf in self.files:
            for m in rx.finditer(pf.clean):
                chain = sorted((n for n in pf.nodes if n.open <= m.start() <= n.end), key=lambda n: n.open)
                if any(exclude_label and exclude_label in n.labels for n in chain):
                    continue
                dev = next((n for n in reversed(chain) if re.search(r'\bcompatible\s*=', pf.own_props(n))), None) \
                    or (chain[-1] if chain else None)
                if not dev or (pf.path, dev.start) in seen:
                    continue
                seen.add((pf.path, dev.start))
                st = STATUS_RE.search(pf.own_props(dev))
                label = dev.labels[0] if dev.labels else dev.ref
                if label and not st:
                    st_eff = self.label_info(label)['status']
                else:
                    st_eff = st.group(1) if st else 'okay'
                inner = next((n for n in reversed(chain) if n.labels), None)
                out.append({'label': label, 'name': dev.name, 'file': self.rel(pf.path),
                            'line': pf.line_of(m.start()), 'status': st_eff,
                            'match_label': inner.labels[0] if inner is not None and inner is not dev else None})
        return out

    def _enclosing_name(self, pf: ParsedFile, off: int) -> str | None:
        best = None
        for nd in pf.nodes:
            if nd.open <= off <= nd.end and (best is None or nd.open > best.open):
                best = nd
        if not best:
            return None
        return (best.labels[0] + ': ' if best.labels else '') + best.name

    # ---------- learning from other boards ----------
    def _sibling_files(self, glob_prefix: str) -> list[str]:
        if not os.path.isdir(self.dts_dir):
            return []
        return sorted(os.path.join(self.dts_dir, f) for f in os.listdir(self.dts_dir)
                      if f.startswith(glob_prefix) and f.endswith(('.dts', '.dtsi', '.dtso')))

    def examples_for_label(self, label: str, glob_prefix: str, limit: int = 6) -> list[dict]:
        """`&label { ... status = "okay" }` blocks from other boards of the same SoC."""
        out = []
        for path in self._sibling_files(glob_prefix):
            if path == self.board_dts:
                continue
            pf = self._parse(path)
            for nd in pf.nodes:
                if nd.ref == label and nd.parent is None:
                    m = STATUS_RE.search(pf.own_props(nd))
                    if m and m.group(1) != 'okay':
                        continue
                    out.append({'file': self.rel(path), 'line': nd.line, 'text': pf.body(nd)})
                    break
            if len(out) >= limit:
                break
        return out

    def examples_for_compatible(self, compat: str, roots: list[str], limit: int = 6) -> list[dict]:
        out, rx = [], re.compile(r'compatible\s*=[^;]*"' + re.escape(compat) + '"')
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _, files in os.walk(root):
                for f in sorted(files):
                    if not f.endswith(('.dts', '.dtsi', '.dtso')):
                        continue
                    p = os.path.join(dirpath, f)
                    try:
                        with open(p, encoding='utf-8', errors='replace') as fh:
                            if compat not in fh.read():
                                continue
                    except OSError:
                        continue
                    pf = self._parse(p)
                    m = rx.search(pf.clean)
                    if not m:
                        continue
                    nd = None
                    for cand in pf.nodes:
                        if cand.open <= m.start() <= cand.end and (nd is None or cand.open > nd.open):
                            nd = cand
                    if nd:
                        out.append({'file': self.rel(p), 'line': nd.line, 'text': pf.body(nd)})
                    if len(out) >= limit:
                        return out
        return out

    # ---------- overview ----------
    def overview(self) -> list[dict]:
        """Every labelled hardware block in the include chain with its effective status.

        This answers "what does the SoC have, and which parts did my board switch on?"
        """
        compat_rx = re.compile(r'compatible\s*=\s*"([^"]+)"')
        seen, out = set(), []
        for pf in self.files:
            for nd in pf.nodes:
                if not nd.labels or '@' not in nd.name:
                    continue
                m = compat_rx.search(pf.own_props(nd))
                if not m:
                    continue
                label = nd.labels[0]
                if label in seen:
                    continue
                seen.add(label)
                info = self.label_info(label)
                out.append({'label': label, 'name': nd.name, 'compatible': m.group(1),
                            'status': info['status'], 'file': self.rel(pf.path), 'line': nd.line,
                            'set_at': info['status_set_at']})
        return sorted(out, key=lambda x: (x['status'] != 'okay', x['label']))
