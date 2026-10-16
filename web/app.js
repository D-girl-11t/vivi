// Bring-up Studio frontend: plain JS, no build step.
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ICON = { pass: '✓', fail: '✗', unknown: '?', manual: '✋' };

const S = { status: null, board: null, source: null, kb: null, periphs: [], pid: null, plan: null, open: new Set(), live: null };

async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.body && typeof o.body !== 'string') { o.body = JSON.stringify(o.body); o.headers = { 'Content-Type': 'application/json' }; }
  const r = await fetch(path, o);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg, ms = 2200) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), ms); }
async function guard(fn) { try { return await fn(); } catch (e) { toast('⚠ ' + e.message, 5000); console.error(e); } }

// ------------------------------------------------------------ tiny markdown
function md(src) {
  if (!src) return '';
  const lines = String(src).split('\n'); let out = '', inCode = false, code = [], inList = false, para = [];
  const inline = s => esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/&lt;br&gt;/g, '<br>');
  const flushP = () => { if (para.length) { out += '<p>' + inline(para.join(' ')) + '</p>'; para = []; } };
  const flushL = () => { if (inList) { out += '</ul>'; inList = false; } };
  for (const l of lines) {
    if (l.trim().startsWith('```')) {
      if (inCode) { out += '<pre>' + esc(code.join('\n')) + '</pre>'; code = []; inCode = false; }
      else { flushP(); flushL(); inCode = true; }
      continue;
    }
    if (inCode) { code.push(l); continue; }
    const li = l.match(/^\s*[*-]\s+(.*)/);
    if (li) { flushP(); if (!inList) { out += '<ul>'; inList = true; } out += '<li>' + inline(li[1]) + '</li>'; continue; }
    if (!l.trim()) { flushP(); flushL(); continue; }
    flushL(); para.push(l.trim());
  }
  flushP(); flushL(); if (inCode) out += '<pre>' + esc(code.join('\n')) + '</pre>';
  return out;
}

// ------------------------------------------------------------ DTS highlighting
function hl(line) {
  let s = esc(line);
  if (/^\s*(#include|#define|\/include\/|#if|#endif)/.test(line)) return `<span class="t-pp">${s}</span>`;
  s = s.replace(/(&quot;[^&]*?&quot;)/g, '<span class="t-str">$1</span>')
    .replace(/(\/\*.*?\*\/|\/\/.*$)/g, '<span class="t-com">$1</span>')
    .replace(/^(\s*)([A-Za-z_][\w]*)(:\s)/, '$1<span class="t-lab">$2</span>$3')
    .replace(/(&amp;[A-Za-z_][\w]*)/g, '<span class="t-ref">$1</span>')
    .replace(/^(\s*)([#\w,.-]+)(\s*=)/, '$1<span class="t-prop">$2</span>$3')
    .replace(/\b(0x[0-9a-fA-F]+)\b/g, '<span class="t-num">$1</span>');
  return s;
}

// ------------------------------------------------------------ tabs
$$('.tabs').forEach(nav => nav.addEventListener('click', e => {
  const b = e.target.closest('button[data-tab]'); if (!b) return;
  switchTab(nav.dataset.for, b.dataset.tab);
}));
function switchTab(panel, tab) {
  if (panel === 'left' && tab === 'boot' && !S.boot) guard(loadBoot);
  $$(`.tabs[data-for="${panel}"] button`).forEach(x => x.classList.toggle('active', x.dataset.tab === tab));
  $$(`#${panel} > .tabbody`).forEach(x => x.classList.toggle('hidden', x.id !== `${panel}-${tab}`));
}

// ------------------------------------------------------------ top bar
async function loadStatus() {
  S.status = await api('/api/status');
  const st = S.status, chips = [];
  const sc = st.board.scanned, dot = c => `<span class="cdot ${c}"></span>`;
  chips.push(`<span class="chip" title="SoC profile: ${esc(st.soc.title || '?')}${st.soc.status === 'starter' ? ' (starter profile)' : ''}">${esc(st.soc.title || 'SoC ?')}</span>`);
  const bstate = st.board.transport === 'disconnected' ? 'warn' : sc ? (sc.demo ? 'warn' : 'ok') : 'bad';
  const btext = st.board.transport === 'disconnected' ? 'disconnected' : sc ? (sc.demo ? 'demo data' : (st.board.model || 'board') + ' · ' + esc((sc.time || '').slice(11, 16))) : 'not scanned';
  chips.push(`<span class="chip" title="Board: ${esc(st.board.transport)}${st.board.model ? ' · ' + esc(st.board.model) : ''}${sc ? ' · scanned ' + esc(sc.time) : ''}">${dot(bstate)}Board: ${esc(btext)}</span>`);
  chips.push(`<span class="chip" title="Kernel source: ${esc(st.source.kernel || 'not set')}\nBoard file: ${esc(st.source.board_dts || 'not set')}">${dot(st.source.ok ? 'ok' : 'bad')}Source: ${esc((st.source.board_dts || 'not set').split('/').pop())}</span>`);
  $('#chips').innerHTML = chips.join('');
  $('#btn-scan').disabled = false;
}

// ------------------------------------------------------------ left: board
async function loadBoard() {
  S.board = await api('/api/board');
  const b = S.board, el = $('#left-board');
  if (b.empty) { el.innerHTML = '<p class="muted">No board scan yet. Set <code>board.transport: ssh</code> in ⚙ settings, then press <b>Rescan board</b>.</p>'; return; }
  const f = b.findings, sec = b.sections || {};
  let h = '';
  if (b.meta?.demo) h += `<div class="demo-banner">DEMO snapshot: the device tree was compiled from ${esc(b.meta.source.replace('compiled from ', ''))}, but drivers, kconfig and dmesg are <b>not real</b>. Connect your board over ssh for actual bring-up.</div>`;
  h += `<div class="muted small">${esc(b.system?.uname || '')}</div>`;
  h += `<div class="cards">
    <div class="card" data-show="enabled"><b>${f.enabled}</b>enabled nodes</div>
    <div class="card" data-show="bound"><b>${f.bound}</b>bound to a driver</div>
    <div class="card ${f.unbound.length ? 'bad' : ''}" data-show="unbound"><b>${f.unbound.length}</b>enabled, NOT bound</div>
    <div class="card ${f.deferred.length ? 'warn' : ''}" data-show="deferred"><b>${f.deferred.length}</b>probe deferred</div>
  </div>`;
  h += `<details open><summary>Device tree (live) <span class="count">${b.dt.length}</span></summary>
    <input class="filter" id="dt-filter" placeholder="filter: path, compatible, okay/disabled">
    <div id="dt-list"></div></details>`;
  const sections = [['alsa_cards', 'ALSA sound cards'], ['v4l2', 'V4L2 video'], ['input', 'Input devices'], ['iio', 'IIO sensors'],
    ['counter', 'Counters'], ['hwmon', 'hwmon'], ['i2c', 'I2C devices'], ['spi', 'SPI devices'], ['usb', 'USB devices'],
    ['net', 'Network'], ['pwm', 'PWM'], ['remoteproc', 'Remoteproc (M33)'], ['modules', 'Loaded modules'],
    ['dmesg', 'dmesg (errors/probe)'], ['pinmux', 'Pin ownership (debugfs)'], ['gpio', 'GPIO lines (debugfs)']];
  for (const [k, t] of sections) {
    const txt = sec[k] || ''; const n = txt.trim() ? txt.trim().split('\n').length : 0;
    h += `<div class="row" data-raw="${k}"><span class="dot ${n ? 'okay' : 'disabled'}"></span>${esc(t)} <span class="muted">${n}</span></div>`;
  }
  h += `<div class="row" data-kconfig="1"><span class="dot okay"></span>Kernel config <span class="muted">${b.kconfig_count}</span></div>`;
  h += `<div class="actions"><button id="btn-save-snap">Save snapshot…</button></div>`;
  el.innerHTML = h;
  renderDtList('');
  $('#dt-filter').addEventListener('input', e => renderDtList(e.target.value));
  el.onclick = onBoardClick;
}
function renderDtList(q) {
  const b = S.board, unb = new Set(b.findings.unbound.map(u => u.path)); q = q.toLowerCase();
  const rows = b.dt.filter(n => !q || (n.path + ' ' + n.compatible.join(' ') + ' ' + n.status).toLowerCase().includes(q)).slice(0, 400);
  $('#dt-list').innerHTML = rows.map(n => `<div class="row" data-node="${esc(n.path)}" title="${esc(n.compatible.join(', '))}">
    <span class="dot ${unb.has(n.path) ? 'bad' : (n.status === 'okay' ? 'okay' : 'disabled')}"></span>${esc(n.path)}</div>`).join('');
}
function onBoardClick(e) {
  const t = e.target.closest('[data-show],[data-raw],[data-node],[data-kconfig],#btn-save-snap'); if (!t) return;
  const b = S.board;
  if (t.id === 'btn-save-snap') return guard(async () => { const name = prompt('Snapshot name', 'frdm-' + new Date().toISOString().slice(0, 10)); if (name) { const r = await api('/api/snapshot/save', { method: 'POST', body: { name } }); toast('saved ' + r.path); } });
  if (t.dataset.raw) return showRaw(t.textContent.trim(), b.sections[t.dataset.raw] || '(empty: not present on this board, or needs root/debugfs)');
  if (t.dataset.kconfig) return guard(async () => { const q = prompt('Filter CONFIG_ symbols (e.g. SND_SOC, ROTARY, UVC)', ''); if (q === null) return; const r = await api('/api/board/kconfig?q=' + encodeURIComponent(q)); showRaw('Kernel config: ' + q, Object.entries(r).map(([k, v]) => `${k}=${v}`).join('\n') || 'no match'); });
  if (t.dataset.node) {
    const n = b.dt.find(x => x.path === t.dataset.node);
    const devs = b.devices.filter(d => d.of_node === n.path);
    const label = Object.entries(b.symbols || {}).filter(([, p]) => p === n.path).map(([l]) => '&' + l);
    return showRaw(n.path, `labels:      ${label.join(' ') || '-'}\ncompatible:  ${n.compatible.join(', ')}\nstatus:      ${n.status}\n\n` +
      (devs.length ? devs.map(d => `device ${d.bus}/${d.name} → driver: ${d.driver || '(NONE, not bound)'}`).join('\n') : 'no Linux device created for this node'));
  }
  const show = t.dataset.show, f = b.findings;
  if (show === 'unbound') return showRaw('Enabled but no driver bound',
    (f.unbound.map(u => `${u.path}\n   compatible: ${u.compatible.join(', ')}\n   devices: ${u.devices.join(', ')}` +
      (u.dmesg?.length ? '\n   dmesg:\n' + u.dmesg.map(l => '     ' + l).join('\n') : '\n   dmesg: (nothing mentions it: driver probably not built or not loaded; check Kernel config)')).join('\n\n') || 'none 🎉') +
    ((f.expected_unbound || []).length ? `\n\n──── ${f.expected_unbound.length} more with no driver BY DESIGN (not a problem) ────\n` +
      f.expected_unbound.map(u => `${u.path}\n   ${u.why}`).join('\n') : ''),
    'These nodes are status=okay and Linux created a device, but no driver claimed it.\nUsual causes: driver not built (kconfig), module failed to load (version mismatch), or probe failed (dmesg shows why).');
  if (show === 'deferred') return showRaw('Deferred probes', f.deferred.join('\n') || 'none (or debugfs not mounted)', 'A deferred probe means the driver is waiting for a resource (clock, regulator, GPIO, codec…) that never appeared.');
  if (show === 'enabled' || show === 'bound') { $('#dt-filter').value = 'okay'; renderDtList('okay'); }
}

// ------------------------------------------------------------ left: source
async function loadSource() {
  S.source = await api('/api/source');
  const el = $('#left-source'), s = S.source;
  if (!s.ok) { el.innerHTML = '<p class="muted">Source tree not configured. Set <code>kernel_src</code> and <code>board_dts</code> in ⚙.</p>'; return; }
  let h = `<details open><summary>Include chain <span class="count">${s.include_tree.length}</span></summary>`;
  h += s.include_tree.map(f => `<div class="row" data-file="${esc(f.file)}" style="padding-left:${4 + f.depth * 14}px">${f.missing ? '⚠ ' : ''}${esc(f.file.split('/').pop())}</div>`).join('');
  h += `</details><details open><summary>SoC blocks: what the board enabled <span class="count">${s.blocks.filter(b => b.status === 'okay').length}/${s.blocks.length}</span></summary>
    <input class="filter" id="blk-filter" placeholder="filter: lpi2c, sai, disabled…"><div id="blk-list"></div></details>`;
  el.innerHTML = h;
  const render = q => { q = q.toLowerCase(); $('#blk-list').innerHTML = s.blocks.filter(b => !q || (b.label + b.compatible + b.status).toLowerCase().includes(q))
    .map(b => `<div class="row" data-file="${esc((b.set_at || b).file)}" data-line="${(b.set_at || b).line}" title="${esc(b.compatible)} · status set in ${esc((b.set_at || b).file)}">
      <span class="dot ${b.status === 'okay' ? 'okay' : 'disabled'}"></span>&amp;${esc(b.label)} <span class="muted">${esc(b.compatible)}</span></div>`).join(''); };
  render(''); $('#blk-filter').addEventListener('input', e => render(e.target.value));
  el.onclick = e => { const r = e.target.closest('[data-file]'); if (r) openFile(r.dataset.file, +r.dataset.line || 1); };
}

// ------------------------------------------------------------ left: pins
let pinTimer;
$('#pin-q').addEventListener('input', e => { clearTimeout(pinTimer); pinTimer = setTimeout(() => guard(async () => {
  const q = e.target.value.trim(); if (q.length < 2) return;
  const r = await api('/api/pins?q=' + encodeURIComponent(q));
  if (r.note) { $('#pin-results').textContent = r.note; return; }
  $('#pin-results').innerHTML = Object.entries(r.pads).map(([pad, fns]) => `<details ${Object.keys(r.pads).length < 4 ? 'open' : ''}><summary><b>${esc(pad)}</b> ${r.used[pad] ? '<span class="pill" style="border-color:var(--unknown)">used</span>' : ''}</summary>
    ${(r.used[pad] || []).map(u => `<div class="small" style="color:var(--unknown)">in use: ${esc(u)}</div>`).join('')}
    ${fns.map(f => `<div class="row">${esc(f)}</div>`).join('')}</details>`).join('') || 'no match';
}), 250); });

// ------------------------------------------------------------ left: boot files on the board
const LIVE = '(live: what the board booted)';
async function loadBoot(detect = true) {
  const el = $('#left-boot');
  el.innerHTML = `<p class="muted small">${detect ? 'reading the boot partition and working out which file boots (compares each one with the live tree)…' : 'reading the boot partition…'}</p>`;
  try { S.boot = await api('/api/boot/files' + (detect ? '?detect=1' : '')); }
  catch (e) { el.innerHTML = `<div class="callout warn small">${esc(e.message)}</div>`; S.boot = null; return; }
  renderBoot();
}
function bootsName() { const c = S.boot?.closest; return c && c.length && (c.length < 2 || c[0].diff < c[1].diff) ? c[0].name : null; }
function renderBoot() {
  const b = S.boot, el = $('#left-boot'), boots = b.fdtfile || bootsName();
  if (b.error) { el.innerHTML = `<div class="callout warn small">${esc(b.error)}</div>`; return; }
  const fmt = t => new Date(t * 1000).toISOString().slice(0, 10);
  // the names share a long prefix (imx93-11x11-frdm…): show the part that tells them apart
  const names = b.files.map(f => f.name); let pre = names[0] || '';
  names.forEach(n => { while (pre && !n.startsWith(pre)) pre = pre.slice(0, -1); });
  const cut = pre.length > 10 ? pre.lastIndexOf('-', pre.length - 1) + 1 : 0;
  const short = n => cut > 0 ? '…' + n.slice(cut) : n;
  el.innerHTML = `<div class="small muted">${esc(b.dir)} · ${b.files.length} files${b.fw_setenv ? '' : ' · no fw_setenv: switching swaps files (with a backup)'}</div>
    ${b.warning ? `<div class="callout warn small">${esc(b.warning)}</div>` : ''}
    <div class="actions"><button class="primary" data-bact="install" title="Build the Kernel source board file and upload it under a new name">⬆ Install from my source</button>
      <button data-bact="cmp-src" title="Your Kernel source board file vs what the board booted">Source vs live</button><button data-bact="reload">↻</button></div>
    <p class="small muted">${boots ? `U-Boot loads <b>${esc(boots)}</b> ${b.fdtfile ? '(fdtfile)' : '(the closest match to the live tree)'}.` : 'Could not tell which file boots.'} Click a file for actions.</p>
    <div id="boot-list">${b.files.map(f => `<div class="row boot-row ${f.name === boots ? 'boots' : ''}" data-bfile="${esc(f.name)}" title="${esc(f.name)}">
      <span class="dot ${f.name === boots ? 'okay' : (f.kind === 'dtb' ? '' : 'disabled')}"></span><span class="bn">${esc(short(f.name))}</span>
      <span class="muted">${(f.size / 1024).toFixed(0)}K · ${fmt(f.mtime)}</span></div>`).join('')}</div>
    <div id="boot-act"></div>`;
  el.onclick = onBootClick;
}
function onBootClick(e) {
  const t = e.target.closest('[data-bact],[data-bfile]'); if (!t) return;
  const boots = S.boot.fdtfile || bootsName();
  if (t.dataset.bfile) {
    const f = t.dataset.bfile;
    $$('.boot-row').forEach(r => r.classList.toggle('sel', r === t));
    $('#boot-act').innerHTML = `<div class="boot-actions"><b>${esc(f)}</b>
      <button data-bact="cmp" data-a="${esc(LIVE)}" data-b="${esc(f)}">Compare with what booted</button>
      ${boots && f !== boots ? `<button data-bact="cmp" data-a="${esc(boots)}" data-b="${esc(f)}">Compare with ${esc(boots)}</button>` : ''}
      <button data-bact="cmp" data-a="${esc(f)}" data-b="source">Compare with my source</button>
      ${boots && f !== boots && /\.dtb(\.|$)|\.dtb$/.test(f) ? `<button class="primary" data-bact="switch" data-use="${esc(f)}">Boot this one…</button>` : ''}</div>`;
    return;
  }
  const a = t.dataset.bact;
  if (a === 'reload') return guard(() => loadBoot(true));
  if (a === 'cmp-src') return compareDtb(LIVE, 'source');
  if (a === 'cmp') return compareDtb(t.dataset.a, t.dataset.b);
  if (a === 'switch') return switchDtb(t.dataset.use);
  if (a === 'install') return installDtb();
}
const nice = n => n === LIVE ? 'what the board booted' : n === 'source' ? 'your Kernel source build' : n;
async function compareDtb(a, b) {
  setView(`<div class="bar"><b>Comparing ${esc(nice(a))} → ${esc(nice(b))}</b></div>`, '<p class="muted">fetching and decompiling…' + (a === 'source' || b === 'source' ? ' (building your source first)' : '') + '</p>');
  try {
    const r = await api('/api/boot/compare', { method: 'POST', body: { a, b } });
    const hide = x => x.uboot ? ' uboot' : '';
    const sec = (title, items, fn) => items.length ? `<h4>${title} <span class="muted">${items.length}</span></h4>${items.map(fn).join('')}` : '';
    const side = (x, n) => `<div><b>${esc(nice(n))}</b><br><span class="muted small">${esc(x.model)} · ${esc(x.compatible[0] || '')} · ${x.nodes} nodes · ${x.labels ? 'has labels' : '<span style="color:var(--unknown)">no labels (built without -@: overlays won\'t apply)</span>'}</span></div>`;
    setView(`<div class="bar"><b>${esc(nice(a))} → ${esc(nice(b))}</b></div>
      <div class="cmp-head">${side(r.a, a)}<div class="arrow">→</div>${side(r.b, b)}</div>
      <p class="small">${r.counts.real ? `<b>${r.counts.real}</b> real difference${r.counts.real > 1 ? 's' : ''}` : '<b>No real differences</b>'}
        <label class="small" style="margin-left:12px"><input type="checkbox" id="show-uboot"> show what the bootloader adds at boot (${r.counts.removed + r.counts.added + r.counts.changed - r.counts.real})</label></p>`,
      `<div class="cmp">${sec('Added nodes', r.added, x => `<div class="cmp-row add${hide(x)}">+ ${esc(x.path)}</div>`)}
        ${sec('Removed nodes', r.removed, x => `<div class="cmp-row rm${hide(x)}">− ${esc(x.path)}</div>`)}
        ${sec('Changed nodes', r.changed, c => `<div class="cmp-node${hide(c)}"><div class="cmp-path">~ ${esc(c.path)}</div>${c.props.map(p => `<div class="cmp-prop${p.uboot ? ' uboot' : ''}">
          <code>${esc(p.prop)}</code>: <span class="rm">${p.a === null ? '<i>absent</i>' : esc(p.a || '(set)')}</span> → <span class="add">${p.b === null ? '<i>absent</i>' : esc(p.b || '(set)')}</span></div>`).join('')}</div>`)}
        ${r.counts.real ? '' : '<p class="muted">The two trees describe the same hardware.</p>'}</div>`);
    const tog = () => $$('.cmp .uboot').forEach(x => x.classList.toggle('hidden', !$('#show-uboot').checked));
    $('#show-uboot').onchange = tog; tog();
  } catch (e) { setView(`<div class="bar"><b>Compare failed</b></div>`, `<div class="callout bad">${esc(e.message)}</div>`); }
}
async function installDtb() {
  const name = prompt('Upload your Kernel source build to the board under this NEW name (nothing that boots is touched):',
    (S.status.source.board_dts || 'board.dts').split('/').pop().replace(/\.dts$/, '') + '-' + new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '').replace(/(\d{8})(\d{4})/, '$1-$2') + '.dtb');
  if (!name) return;
  setView(`<div class="bar"><b>Install ${esc(name)}</b></div>`, '<p class="muted">building the .dtb from your source, then uploading…</p>');
  try {
    const r = await api('/api/boot/install', { method: 'POST', body: { name } });
    setView(`<div class="bar"><b>✓ Installed ${esc(r.name)}</b></div>`,
      `<p>Built <code>${esc(r.build.rel)}</code> (${(r.build.size / 1024).toFixed(0)} KB, labels kept) and uploaded it as <code>${esc(r.path)}</code>.
       <b>Nothing that boots has changed yet.</b></p>
       ${r.build.warnings.length ? `<details><summary class="small">${r.build.warnings.length} dtc warnings</summary><pre class="raw">${esc(r.build.warnings.join('\n'))}</pre></details>` : ''}
       <div class="actions"><button data-x="cmp">Compare it with what booted</button><button class="primary" data-x="switch">Boot it…</button></div>`);
    $('#view-body [data-x="cmp"]').onclick = () => compareDtb(LIVE, r.name);
    $('#view-body [data-x="switch"]').onclick = async () => { await loadBoot(true); switchDtb(r.name); };
    await loadBoot(false);
  } catch (e) { setView(`<div class="bar"><b>Install failed</b></div>`, `<div class="callout bad">${esc(e.message)}</div>`); }
}
async function switchDtb(use) {
  const b = S.boot, target = b.fdtfile || bootsName();
  if (!target) return toast('Could not tell which file boots: press ↻ in the Boot tab first', 6000);
  const how = b.fw_setenv ? `U-Boot's fdtfile will be set to ${use}.`
    : `U-Boot loads ${target}. It will be BACKED UP to ${target}.bs-<date> and replaced by a copy of ${use}.`;
  if (!confirm(`Boot ${use}?\n\n${how}\n\nThe change takes effect after a reboot. If the board doesn't come back, the exact U-Boot rescue commands are shown next.`)) return;
  try {
    const r = await api('/api/boot/switch', { method: 'POST', body: { target, use } });
    const rescue = `<ol class="small">${r.rescue.map(x => `<li><code>${esc(x)}</code></li>`).join('')}</ol>`;
    setView(`<div class="bar"><b>✓ Next boot uses ${esc(use)}</b></div>`,
      `<p>${r.method === 'swap' ? `Original kept as <code>${esc(r.backup)}</code>.` : `Previous fdtfile: <code>${esc(r.previous)}</code>.`}
        Undo from Linux: <code>${esc(r.undo_linux)}</code></p>
       <div class="callout"><b>If the board doesn't come back</b> (keep minicom open to watch it boot):${rescue}</div>
       <div class="actions"><button class="primary" data-x="reboot">⟲ Reboot now and check</button></div><div id="reboot-out"></div>`);
    $('#view-body [data-x="reboot"]').onclick = e => rebootAndCheck(e.target, rescue);
    await loadBoot(false);
  } catch (e) { toast('⚠ ' + e.message, 8000); }
}
async function rebootAndCheck(btn, rescue) {
  btn.disabled = true; const out = $('#reboot-out');
  await api('/api/boot/reboot', { method: 'POST' });
  const t0 = Date.now(); let up = false, wentDown = false;
  while (Date.now() - t0 < 240000) {
    await new Promise(r => setTimeout(r, 5000));
    const s = Math.round((Date.now() - t0) / 1000);
    const r = await api('/api/board/test', { method: 'POST', body: { transport: 'ssh', host: S.status.board.transport.replace(/^ssh /, '') } }).catch(() => ({ ok: false }));
    if (!r.ok) wentDown = true;
    out.innerHTML = `<p class="muted">${r.ok ? (wentDown ? 'back up' : 'still going down…') : `waiting for the board… ${s}s`}</p>` +
      (!r.ok && s >= 60 ? `<div class="callout warn small"><b>Taking longer than usual (most boards are back in 30–60 s).</b> Look at minicom:
        <ul><li><b>You see a <code>login:</code> prompt</b> → it booted, but isn't on the network. Over Wi-Fi this is common: a connection made by hand
          (wpa_supplicant/udhcpc) doesn't come back after a reboot. Log in, run <code>ip addr</code>, reconnect, and if the IP changed update Board → Change on the home page.</li>
        <li><b>Boot messages stopped, or a kernel error/panic</b> → this device tree didn't boot. Use the rescue steps below.</li>
        <li><b>Nothing at all</b> → check minicom is on the right port (<code>/dev/ttyACM*</code>) at 115200.</li></ul>${rescue}</div>` : '');
    if (r.ok && wentDown) { up = true; break; }
  }
  if (!up) { out.innerHTML = `<div class="callout bad"><b>The board did not come back within 4 minutes.</b> Check minicom; use the rescue steps:${rescue}</div>`; btn.disabled = false; return; }
  await api('/api/scan', { method: 'POST' });
  await Promise.all([loadStatus(), loadBoard()]); await loadPlan();
  out.innerHTML = `<div class="callout ok"><b>✓ The board is back and was rescanned.</b> ${esc(S.board.system?.model || '')}<br>
    Check the guide's <b>Deploy</b> and <b>Probe</b> steps on the right, or <a href="#" id="cmp-after">compare what booted with your source</a>.</div>`;
  $('#cmp-after').onclick = e => { e.preventDefault(); compareDtb(LIVE, 'source'); };
  S.boot = null;
}

// ------------------------------------------------------------ center viewer
function setView(head, body) { switchTab('center', 'view'); $('#view-head').innerHTML = head; $('#view-body').innerHTML = body; $('#center-view').scrollTop = 0; }
function showRaw(title, text, note = '') { setView(`<div class="bar"><b>${esc(title)}</b></div>${note ? `<p class="muted small">${esc(note)}</p>` : ''}`, `<pre class="raw">${esc(text)}</pre>`); }

async function openFile(path, line = 1, opts = {}) {
  return guard(async () => {
    const r = await api('/api/file?path=' + encodeURIComponent(path));
    const lines = r.text.split('\n'), end = opts.endLine || line;
    const rows = lines.map((l, i) => `<tr class="${i + 1 === line ? 'hl' : (i + 1 > line && i + 1 <= end ? 'hl2' : '')}" id="L${i + 1}"><td class="ln">${i + 1}</td><td>${hl(l)}</td></tr>`).join('');
    const snip = opts.snippet ? `<div class="snippet-overlay"><div class="small muted">${esc(opts.snippetNote || 'Proposed change')}</div>${snipBlock(opts.snippet)}</div>` : '';
    setView(`<div class="bar"><span class="path">${esc(path)}:${line}</span>
      <button data-open-editor>Open in editor</button><button data-reload>↻</button></div>${snip}`,
      `<div class="code"><table>${rows}</table></div>`);
    $('[data-open-editor]').onclick = () => guard(async () => { const x = await api('/api/open', { method: 'POST', body: { path, line } }); toast('ran: ' + x.cmd); });
    $('[data-reload]').onclick = () => openFile(path, line, opts);
    const target = $('#L' + line);
    if (target) setTimeout(() => target.scrollIntoView({ block: 'center' }), 30);
  });
}

function snipBlock(text) {
  const id = 's' + Math.random().toString(36).slice(2);
  return `<div class="snip"><pre id="${id}">${text.split('\n').map(hl).join('\n')}</pre><button class="copy" data-copy="${id}">copy</button></div>`;
}
document.addEventListener('click', e => {
  const c = e.target.closest('[data-copy]'); if (!c) return;
  const pre = document.getElementById(c.dataset.copy);
  navigator.clipboard.writeText(pre.innerText).then(() => toast('copied'), () => toast('copy failed: select the text manually'));
});

// ------------------------------------------------------------ right: guide
async function loadPeripherals(select) {
  const r = await api('/api/peripherals'); S.periphs = r.items;
  const sel = $('#periph-select');
  sel.innerHTML = S.periphs.map(p => `<option value="${esc(p.id)}">${esc(p.name)}  ·  ${esc(p.bus || '?')} → ${esc(p.subsystem || '?')}</option>`).join('') || '<option value="">no peripherals yet, press ＋</option>';
  const fromUrl = new URLSearchParams(location.search).get('p');
  S.pid = select || fromUrl || (() => { try { return localStorage.getItem('bs.pid'); } catch { return null; } })() || S.periphs[0]?.id;
  if (!S.periphs.some(p => p.id === S.pid)) S.pid = S.periphs[0]?.id;
  if (S.pid) sel.value = S.pid;
  if (S.pid) await loadPlan();
}
$('#periph-select').addEventListener('change', e => { S.pid = e.target.value; S.open.clear(); try { localStorage.setItem('bs.pid', S.pid); } catch { }
  history.replaceState(null, '', '/app?p=' + encodeURIComponent(S.pid)); guard(loadPlan); });

async function loadPlan(liveStep) {
  if (!S.pid) return;
  const q = '/api/plan?id=' + encodeURIComponent(S.pid) + (liveStep ? '&live_step=' + encodeURIComponent(liveStep) : '');
  S.plan = await api(q);
  if (S.open.size === 0 && S.plan.current) S.open.add(S.plan.current);
  renderPlan();
}

function renderPlan() {
  const p = S.plan, layers = S.kb?.layers || [];
  // ladder: worst status per layer
  const rank = { fail: 3, unknown: 2, manual: 1, pass: 0 };
  const byLayer = {};
  p.steps.forEach(s => { const cur = byLayer[s.layer]; if (!cur || rank[s.result.status] > rank[cur]) byLayer[s.layer] = s.result.status; });
  const curLayer = p.steps.find(s => s.id === p.current)?.layer;
  $('#ladder').innerHTML = layers.filter(l => byLayer[l.id]).map(l => `<span class="rung ${byLayer[l.id]} ${l.id === curLayer ? 'cur' : ''}" title="${esc(l.eli5)}" data-layer="${l.id}">${esc(l.title)}</span>`).join('');
  const sm = p.summary, done = p.steps.filter(s => s.result.status === 'pass').length;
  $('#plan-summary').innerHTML = `${done}/${p.steps.length} steps green` +
    (sm.driver ? ` · driver <code>${esc(sm.driver.name)}</code>` : '') + (sm.controller?.label ? ` · controller <code>&${esc(sm.controller.label)}</code>` : '') +
    (sm.binding ? ` · <span class="filelink" data-file="${esc(sm.binding.file)}">binding</span>` : '') +
    (sm.driver?.file ? ` · <span class="filelink" data-file="${esc(sm.driver.file)}" data-line="${sm.driver.line || 1}">driver source</span>` : '') +
    p.problems.map((x, i) => `<div class="problem">⚠ ${esc(x.text)}${x.action === 'fetch_driver' ? ` <button class="fix-btn" data-fetch="1">⬇ Download driver</button>` : ''}${x.fix ? ` <button class="fix-btn" data-fix="${i}">Apply fix: ${esc(Object.entries(x.fix).map(([k, v]) => `${k}: ${v}`).join(', '))}</button>` : ''}</div>`).join('');
  $('#steps').innerHTML = p.steps.map((s, i) => stepHtml(s, i)).join('');
  setupLive(sm.live);
}

function stepHtml(s, i) {
  const open = S.open.has(s.id), cur = s.id === S.plan.current, r = s.result;
  let b = '';
  if (open) {
    b += `<div class="step-b">`;
    if (s.why) b += `<h4>Why</h4><div class="md">${md(s.why)}</div>`;
    if (s.do) b += `<h4>Do</h4><div class="md">${md(s.do)}</div>`;
    if (s.file && s.anchor) b += `<div style="margin-top:8px"><span class="filelink" data-step-file="${esc(s.id)}">📄 ${esc(s.file.split('/').pop())}:${s.anchor.line || 1} ${s.anchor.exists ? `(${esc(s.anchor.ref)} block)` : '(add new block here)'} →</span></div>`;
    if (s.snippet) b += `<h4>Change</h4>${snipBlock(s.snippet)}`;
    if (s.command) b += `<h4>Command</h4>${snipBlock(s.command)}`;
    b += `<div class="result ${r.status}"><b>${ICON[r.status]} ${r.status}</b> ${esc(r.detail)}</div>`;
    b += `<div class="actions"><button class="primary" data-check="${esc(s.id)}">Check</button>`;
    if ((s.actions || []).includes('fetch_driver')) b += `<button class="primary" data-fetch="1">⬇ Download driver</button>`;
    if (s.examples) b += `<button data-examples="${esc(s.id)}">Examples from other boards</button>`;
    b += `<button data-ask="${esc(s.id)}" class="ai-btn" title="Ask Claude about this step, with the evidence attached">✦ Ask Claude</button>`;
    b += r.status === 'pass' && /marked done/.test(r.detail) ? `<button data-manual="${esc(s.id)}" data-done="">Undo done</button>`
      : r.status !== 'pass' ? `<button data-manual="${esc(s.id)}" data-done="1">Mark done</button>` : '';
    b += `</div></div>`;
  }
  return `<div class="step ${cur ? 'cur' : ''}" data-step="${esc(s.id)}">
    <div class="step-h" data-toggle="${esc(s.id)}"><span class="num">${i + 1}</span><span class="icon ${r.status}">${ICON[r.status]}</span>
    <span class="title">${esc(s.title)}</span><span class="layer">${esc(s.layer)}</span></div>${b}</div>`;
}

$('#right').addEventListener('click', e => {
  const t = e.target;
  const tog = t.closest('[data-toggle]');
  if (tog) { const id = tog.dataset.toggle; S.open.has(id) ? S.open.delete(id) : S.open.add(id); renderPlan(); return; }
  const rung = t.closest('[data-layer]');
  if (rung) { const s = S.plan.steps.find(x => x.layer === rung.dataset.layer); if (s) { S.open.add(s.id); renderPlan(); document.querySelector(`[data-step="${s.id}"]`)?.scrollIntoView({ block: 'start' }); } return; }
  const sf = t.closest('[data-step-file]');
  if (sf) { const s = S.plan.steps.find(x => x.id === sf.dataset.stepFile);
    openFile(s.anchor.file || s.file, s.anchor.line || 1, { endLine: s.anchor.end_line, snippet: s.snippet,
      snippetNote: s.anchor.exists ? `Add inside the ${s.anchor.ref} block (highlighted)` : `Add as a new top-level block (end of file)` }); return; }
  const fl = t.closest('.filelink[data-file]');
  if (fl) { openFile(fl.dataset.file, +fl.dataset.line || 1); return; }
  const ck = t.closest('[data-check]');
  if (ck) { guard(async () => { ck.disabled = true; ck.textContent = 'checking…'; await api('/api/source/reload', { method: 'POST' }); await loadPlan(ck.dataset.check); const s = S.plan.steps.find(x => x.id === ck.dataset.check); toast(`${ICON[s.result.status]} ${s.result.status}`); }); return; }
  const mn = t.closest('[data-manual]');
  if (mn) { guard(async () => { await api('/api/manual', { method: 'POST', body: { id: S.pid, step: mn.dataset.manual, done: mn.dataset.done ? true : null } }); await loadPlan(); }); return; }
  const fx = t.closest('[data-fix]');
  if (fx) { guard(async () => { const fix = S.plan.problems[+fx.dataset.fix].fix;
    await api('/api/peripheral/fields', { method: 'POST', body: { id: S.pid, fields: fix } });
    toast('form updated: ' + Object.entries(fix).map(([k, v]) => `${k}: ${v}`).join(', ')); await loadPeripherals(S.pid); }); return; }
  const fe = t.closest('[data-fetch]');
  if (fe) { guard(async () => { fe.disabled = true; fe.textContent = 'downloading…';
    try { const r = await api('/api/driver/fetch', { method: 'POST', body: { id: S.pid } });
      toast(`saved ${r.saved} (declares ${r.compatibles.join(', ') || 'no compatible'})`, 6000); await loadPeripherals(S.pid); }
    finally { fe.disabled = false; fe.textContent = '⬇ Download driver'; } }); return; }
  const ak = t.closest('[data-ask]');
  if (ak) { askAi(ak.dataset.ask); return; }
  const ex = t.closest('[data-examples]');
  if (ex) { guard(async () => {
    const s = S.plan.steps.find(x => x.id === ex.dataset.examples), q = new URLSearchParams(Object.entries(s.examples).filter(([, v]) => v));
    const r = await api('/api/examples?' + q);
    setView(`<div class="bar"><b>How other boards do it: ${esc(Object.values(s.examples).join(' '))}</b></div><p class="muted small">Real code from other board files in your kernel tree. Compare the properties they set with your snippet.</p>`,
      r.examples.map(x => `<div style="margin-bottom:14px"><span class="filelink" data-ex-file="${esc(x.file)}" data-line="${x.line}">${esc(x.file)}:${x.line}</span>${snipBlock(x.text)}</div>`).join('') || '<p class="muted">No other board in this tree uses it.</p>');
  }); return; }
});
$('#center').addEventListener('click', e => { const f = e.target.closest('[data-ex-file]'); if (f) openFile(f.dataset.exFile, +f.dataset.line); });

// ------------------------------------------------------------ AI helper (Claude)
// A small chat about one step, or about the whole device. Each question goes out with the
// tool's evidence (form, steps and checks, what the driver reads, dmesg, board-file block).
const AI_SUGGEST = {
  step: ['Explain this step like I am new to this', 'Why is this check failing, and how do I fix it?', 'What usually goes wrong here?', 'Show me exactly what to change'],
  device: ['Where am I and what should I do next?', 'Explain the whole path for this device', 'What is most likely to go wrong?', 'Which parts of the datasheet matter here?'],
};
function aiReady() { return S.ai?.enabled && S.ai?.ready; }
function aiOffNote() {
  if (!S.ai?.enabled) return `The AI helper is <b>off</b>. Turn it on in ⚙ with <code>ai: {enabled: true}</code>.`;
  return S.ai.backend === 'claude-code' ? 'The <code>claude</code> CLI was not found on PATH.' : '<code>ANTHROPIC_API_KEY</code> is not set.';
}
function askAi(stepId) {
  const s = stepId ? S.plan.steps.find(x => x.id === stepId) : null;
  const chat = { stepId, history: [] };
  setView(`<div class="bar"><b>✦ Ask Claude: ${esc(s ? s.title : S.plan.name + ' (whole device)')}</b>
      <span class="muted small">${esc(S.ai?.label || '')}</span></div>`,
    `${aiReady() ? '' : `<div class="callout warn">${aiOffNote()} You can still preview what would be sent.</div>`}
     <div id="ai-thread"></div>
     <div class="ai-suggest">${AI_SUGGEST[s ? 'step' : 'device'].map(q => `<button class="chip-btn" data-q="${esc(q)}">${esc(q)}</button>`).join('')}</div>
     <div class="ai-input"><textarea id="ai-q" rows="2" placeholder="Ask anything about ${s ? 'this step' : 'this device'}… (Enter to send, Shift+Enter for a new line)"></textarea>
       <button class="primary" id="ai-go" ${aiReady() ? '' : 'disabled'}>Ask</button></div>
     <div class="actions"><button id="ai-ctx">Show what gets sent</button>${s ? '' : ''}</div>
     <p class="muted small">Claude sees only the evidence shown under "what gets sent". It can't read or change your files.
       Answers can be wrong: check them against the binding, the driver source and your board.</p>
     <div id="ai-ctx-out"></div>`);
  const send = q => guard(async () => {
    q = (q || $('#ai-q').value).trim(); if (!q || !aiReady()) return;
    $('#ai-q').value = ''; const b = $('#ai-go'); b.disabled = true; b.textContent = '…';
    const th = $('#ai-thread');
    th.insertAdjacentHTML('beforeend', `<div class="ai-msg me">${esc(q)}</div><div class="ai-msg bot pending">thinking…</div>`);
    th.lastElementChild.scrollIntoView({ block: 'end' });
    try {
      const r = await api('/api/ask', { method: 'POST', body: { id: S.pid, step: stepId, question: q, history: chat.history } });
      chat.history.push({ q, a: r.answer });
      th.lastElementChild.outerHTML = `<div class="ai-msg bot md">${md(r.answer)}<div class="ai-meta">${esc(r.model || '')}${r.seconds ? ` · ${r.seconds}s` : ''}${r.cost_usd ? ` · $${r.cost_usd.toFixed(3)}` : ''}</div></div>`;
    } catch (e) { th.lastElementChild.outerHTML = `<div class="ai-msg bot err">⚠ ${esc(e.message)}</div>`; }
    finally { b.disabled = !aiReady(); b.textContent = 'Ask'; }
  });
  $('#ai-go').onclick = () => send();
  $('#ai-q').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } };
  $$('.ai-suggest [data-q]').forEach(b => b.onclick = () => send(b.dataset.q));
  $('#ai-ctx').onclick = () => guard(async () => {
    const r = await api('/api/ask', { method: 'POST', body: { id: S.pid, step: stepId, preview: true } });
    $('#ai-ctx-out').innerHTML = `<h4>Context sent with each question</h4><pre class="raw">${esc(r.context)}</pre>`;
  });
  $('#ai-q').focus();
}

// ------------------------------------------------------------ forms & settings
function editText(title, text, help, onSave) {
  setView(`<div class="bar"><b>${esc(title)}</b><button class="primary" data-save>Save</button></div>${help}`,
    `<textarea class="editor" spellcheck="false">${esc(text)}</textarea>`);
  const ta = $('textarea.editor');
  ta.addEventListener('keydown', e => { if (e.key === 'Tab') { e.preventDefault(); document.execCommand('insertText', false, '  '); } if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); $('[data-save]').click(); } });
  $('[data-save]').onclick = () => guard(async () => { await onSave(ta.value); toast('saved'); });
}
$('#btn-form').onclick = () => guard(async () => {
  if (!S.pid) return;
  const r = await api('/api/peripheral?id=' + encodeURIComponent(S.pid));
  const kb = S.kb;
  const help = `<div class="muted small"><b>bus</b>: ${kb.bus.map(b => `<code title="${esc(b.summary)}">${esc(b.id)}</code>`).join(' ')} · <b>subsystem</b>: ${kb.subsystem.map(b => `<code title="${esc(b.summary)}">${esc(b.id)}</code>`).join(' ')}
    <br>Hover a name for what it covers. Use the <b>Pins</b> tab on the left to find pad names and functions. Ctrl+S saves.</div>
    <div class="actions"><input id="lookup-c" placeholder="look up a compatible, e.g. bosch,bme280"><button id="lookup-b">Look up</button></div><div id="lookup-r" class="small"></div>`;
  editText(`peripherals/${S.pid}.yaml`, r.text, help, async txt => { await api('/api/peripheral?id=' + encodeURIComponent(S.pid), { method: 'PUT', body: { text: txt } }); await loadPeripherals(S.pid); });
  $('#lookup-b').onclick = () => guard(async () => {
    const c = $('#lookup-c').value.trim(); const x = await api('/api/lookup?compatible=' + encodeURIComponent(c));
    $('#lookup-r').innerHTML = !x.binding && !x.driver ? 'not found in this kernel tree' :
      `${x.binding ? `binding <code>${esc(x.binding.file)}</code> requires <code>${esc((x.binding.required || []).join(', '))}</code>` : 'no binding doc'} · ${x.driver ? `driver <code>${esc(x.driver.file)}</code> → <code>${esc(x.driver.config || '?')}</code>` : 'no driver'}`;
  });
});
$('#btn-new').onclick = () => { location.href = '/#add'; };
$('#btn-ask').onclick = () => { if (S.plan) askAi(null); };
$('#btn-del').onclick = () => guard(async () => {
  const id = S.pid; if (!id) return;
  if (!confirm(`Delete "${id}"?\n\nIts form (peripherals/${id}.yaml) and ticked-off steps move to .state/trash/. Your kernel source and board are not touched.`)) return;
  const r = await api('/api/peripheral?id=' + encodeURIComponent(id), { method: 'DELETE' });
  try { localStorage.removeItem('bs.pid'); } catch { }
  S.pid = null; S.open.clear(); history.replaceState(null, '', '/app');
  await loadPeripherals();
  if (!S.pid) { $('#ladder').innerHTML = ''; $('#plan-summary').innerHTML = ''; $('#steps').innerHTML = '<p class="muted" style="padding:10px">No devices yet. Press ＋ to add one.</p>'; }
  toast(`Deleted ${id}: restore it from the home page's undo, or from .state/trash/${r.trashed}.yaml`, 7000);
});
$('#btn-settings').onclick = () => guard(async () => {
  const r = await api('/api/config');
  editText('workspace.yaml', r.text, '<p class="muted small">Point <code>kernel_src</code> at the kernel tree your board runs, set <code>board.transport: ssh</code> plus <code>host</code> once key login works.</p>',
    async txt => { await api('/api/config', { method: 'PUT', body: { text: txt } }); await boot(); });
});
$('#btn-scan').onclick = () => guard(async () => {
  const b = $('#btn-scan'); b.disabled = true; b.textContent = 'scanning…';
  try { const r = await api('/api/scan', { method: 'POST' }); toast(`scan done: ${r.findings.bound} bound, ${r.findings.unbound.length} unbound`); await Promise.all([loadStatus(), loadBoard()]); await loadPlan(); }
  finally { b.disabled = false; b.textContent = '⟳ Rescan'; }
});
$('#btn-reload-src').onclick = () => guard(async () => { await api('/api/source/reload', { method: 'POST' }); await Promise.all([loadSource(), loadPlan()]); toast('source re-read'); });

// ------------------------------------------------------------ live plot
function setupLive(live) {
  const note = $('#live-note');
  if (!live) { note.textContent = 'Plot any numeric sysfs value from the board (IIO channels, counters, hwmon, gpio values).'; return; }
  if (live.kind === 'sysfs' && !$('#live-paths').value) $('#live-paths').value = live.hint || '';
  note.textContent = live.kind === 'sysfs' ? 'Suggested path for this subsystem, adjust the device number to match the Board tab.'
    : `This subsystem (${live.kind}) gets a dedicated view later (audio waveform, video preview, input events). For now, plot sysfs values here and use the userspace step's command.`;
}
$('#live-start').onclick = () => {
  stopLive();
  const paths = $('#live-paths').value.split(',').map(s => s.trim()).filter(Boolean); if (!paths.length) return;
  const es = new EventSource(`/api/live?paths=${encodeURIComponent(paths.join(','))}&hz=${$('#live-hz').value}`);
  const data = paths.map(() => []); S.live = { es, paths, data };
  es.onmessage = ev => { const v = JSON.parse(ev.data); v.forEach((x, i) => { const n = parseFloat(x); if (!isNaN(n)) { data[i].push(n); if (data[i].length > 400) data[i].shift(); } });
    $('#live-values').innerHTML = paths.map((p, i) => `<div><span style="color:${COLORS[i % COLORS.length]}">■</span> ${esc(p)} = <b>${esc(v[i])}</b></div>`).join(''); drawLive(); };
  es.addEventListener('error', ev => { if (ev.data) toast('live: ' + ev.data, 5000); stopLive(); });
};
const COLORS = ['#1a7f37', '#0a5aa8', '#d4731c', '#8250df', '#cf222e', '#b07d00'];
function stopLive() { if (S.live) { S.live.es.close(); S.live = null; } }
$('#live-stop').onclick = stopLive;
function drawLive() {
  const cv = $('#live-canvas'), dpr = devicePixelRatio || 1; cv.width = cv.clientWidth * dpr; cv.height = cv.clientHeight * dpr;
  const g = cv.getContext('2d'); g.scale(dpr, dpr); const W = cv.clientWidth, H = cv.clientHeight;
  g.clearRect(0, 0, W, H); g.strokeStyle = '#d6ead6'; g.beginPath(); for (let y = 0; y <= 4; y++) { g.moveTo(0, y * H / 4); g.lineTo(W, y * H / 4); } g.stroke();
  S.live.data.forEach((d, i) => {
    if (d.length < 2) return; const mn = Math.min(...d), mx = Math.max(...d), span = mx - mn || 1;
    g.strokeStyle = COLORS[i % COLORS.length]; g.lineWidth = 1.5; g.beginPath();
    d.forEach((v, j) => { const x = j / 399 * W, y = H - 10 - (v - mn) / span * (H - 20); j ? g.lineTo(x, y) : g.moveTo(x, y); }); g.stroke();
    g.fillStyle = COLORS[i % COLORS.length]; g.font = '11px monospace'; g.fillText(`max ${mx}  min ${mn}`, 6, 14 + i * 14);
  });
}

// ------------------------------------------------------------ boot
async function boot() {
  await guard(async () => {
    S.kb = await api('/api/knowledge');
    await loadStatus();
    S.ai = S.status.ai;
    await Promise.all([loadBoard(), loadSource()]);
    await loadPeripherals();
  });
}
boot();
