// Bring-up Studio homepage: setup state, device list, and the "add a device" wizard.
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.body && typeof o.body !== 'string') { o.body = JSON.stringify(o.body); o.headers = { 'Content-Type': 'application/json' }; }
  const r = await fetch(path, o); const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg, ms = 2600) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), ms); }
async function guard(fn) { try { return await fn(); } catch (e) { toast('⚠ ' + e.message, 6000); console.error(e); } }

let O = null, KB = null;

// ------------------------------------------------------------ page
async function load() {
  [O, KB] = await Promise.all([api('/api/overview'), api('/api/knowledge')]);
  renderSetup(); renderDevices(); renderLadder(); renderAi();
}

function renderSetup() {
  const st = O.status, sc = st.board.scanned, c = O.config;
  const card = (state, title, body, btn) => `<div class="setup-card ${state}">
    <div class="sc-head"><span class="state-dot ${state}"></span><b>${title}</b></div>
    <div class="sc-body">${body}</div>${btn || ''}</div>`;
  $('#setup-cards').innerHTML = [
    card(st.soc.id ? 'ok' : 'todo', 'SoC profile',
      st.soc.id ? `${esc(st.soc.title)}${st.soc.status === 'starter' ? ' <span class="pill warn">starter</span>' : ''}` : 'not found',
      `<button data-act="setup">Change</button>`),
    card(st.source.ok ? 'ok' : 'todo', 'Kernel source',
      st.source.ok ? `<code>${esc(st.source.board_dts)}</code><div class="muted small ellipsis" title="${esc(st.source.kernel)}">${esc(st.source.kernel)}</div>`
        : 'Not set: source checks and driver lookups are off. Point it at the kernel your board runs.',
      `<button data-act="setup" class="${st.source.ok ? '' : 'primary'}">${st.source.ok ? 'Change' : 'Set up'}</button>`),
    card(sc && !sc.demo ? 'ok' : (sc ? 'warn' : 'todo'), 'Board',
      sc ? (sc.demo ? 'Demo snapshot: device tree is real, drivers and dmesg are not. Connect your board over ssh.'
        : `${esc(st.board.model || '')}<div class="muted small">${esc(st.board.transport)} · scanned ${esc(sc.time)} · ${st.board.findings.bound} bound, <b>${st.board.findings.unbound.length}</b> unbound</div>`)
        : `${esc(st.board.transport)} · not scanned yet`,
      `<div class="row-btns"><button data-act="setup">${c.board.transport === 'ssh' ? 'Change' : 'Connect'}</button>${st.board.live ? '<button data-act="scan">⟳ Scan now</button>' : ''}</div>`),
  ].join('');
}

function renderDevices() {
  const cards = O.items.map(d => {
    const pct = d.total ? Math.round(100 * d.done / d.total) : 0;
    return `<a class="dev-card" href="/app?p=${encodeURIComponent(d.id)}">
      <div class="dc-head"><b>${esc(d.name)}</b>${d.needs_driver ? '<span class="pill warn" title="No driver for this device in your kernel tree: the guide includes the Driver layer">needs a driver</span>' : ''}</div>
      <div class="muted small">${esc(d.bus || '?')} → ${esc(d.subsystem || '?')}</div>
      ${d.error ? `<div class="err small">⚠ ${esc(d.error)}</div>` : `
      <div class="bar"><span style="width:${pct}%"></span></div>
      <div class="small">${d.done}/${d.total} steps done</div>
      ${d.next ? `<div class="small next">Next: <b>${esc(d.next.title)}</b> <span class="layer">${esc(d.next.layer)}</span></div>` : '<div class="small next">🎉 all green</div>'}`}
    </a>`;
  });
  cards.push(`<button class="dev-card add" data-act="wizard"><span class="plus">＋</span>Add a device<span class="muted small">sensor, mic, camera, display, motor… on any bus</span></button>`);
  $('#devices').innerHTML = cards.join('');
}

function renderLadder() {
  $('#ladder-strip').innerHTML = O.layers.map(l => `<li><b>${esc(l.title)}</b><span>${esc(l.eli5)}</span></li>`).join('');
}

function renderAi() {
  const a = O.ai;
  $('#ai-card').innerHTML = `<h3>Optional AI helper ${a.enabled ? (a.has_key ? '<span class="pill ok">on</span>' : '<span class="pill warn">key missing</span>') : '<span class="pill">off</span>'}</h3>
    <p>For what rules can't cover (a strange dmesg error, porting a driver across kernel versions, "why is my mic silent?"),
      each step gets an <b>Ask AI</b> button. It sends that step together with the tool's evidence to Claude (<code>${esc(a.model)}</code>)
      and shows you exactly what was sent.</p>
    ${a.enabled ? (a.has_key ? '' : '<p class="err small">Start the tool with <code>ANTHROPIC_API_KEY=sk-ant-… ./run.sh</code></p>')
      : `<p class="small">To turn it on: add <code>ai: {enabled: true}</code> to workspace.yaml and start with
         <code>ANTHROPIC_API_KEY=sk-ant-… ./run.sh</code>. The key stays in your environment and is never written to a file.
         Your form, snippets and dmesg lines are sent to Anthropic when you press Ask.</p>`}`;
}

document.addEventListener('click', e => {
  const t = e.target.closest('[data-act]'); if (!t) return;
  e.preventDefault();
  if (t.dataset.act === 'setup') openSetup();
  if (t.dataset.act === 'wizard') openWizard();
  if (t.dataset.act === 'scan') guard(async () => { t.disabled = true; t.textContent = 'scanning…'; await api('/api/scan', { method: 'POST' }); await load(); toast('scan done'); });
});

// ------------------------------------------------------------ setup dialog
function openSetup() {
  const f = $('#setup-form'), c = O.config;
  f.soc.innerHTML = O.socs.map(s => `<option value="${esc(s.id)}">${esc(s.title)}${s.status === 'starter' ? ' (starter)' : ''}</option>`).join('');
  f.soc.value = c.soc || '';
  f.kernel_src.value = c.kernel_src || ''; f.board_dts.value = c.board_dts || '';
  f['board.transport'].value = c.board.transport || 'snapshot'; f['board.host'].value = c.board.host || '';
  const sync = () => $('.ssh-only').classList.toggle('hidden', f['board.transport'].value !== 'ssh');
  f['board.transport'].onchange = sync; sync();
  $('#dlg-setup').showModal();
}
$('#setup-save').onclick = e => {
  e.preventDefault();
  const f = $('#setup-form');
  guard(async () => {
    const body = {}; ['soc', 'kernel_src', 'board_dts', 'board.transport', 'board.host'].forEach(k => { body[k] = f[k].value.trim(); });
    await api('/api/config/fields', { method: 'POST', body });
    $('#dlg-setup').close(); toast('saved'); await load();
  });
};

// ------------------------------------------------------------ add-a-device wizard
const STEPS = ['What is it?', 'Driver', 'Connection', 'Name'];
const TOP_LEVEL = new Set(['format', 'codec_ref', 'card_name']);
let W;

function openWizard() {
  W = { step: 0, q: '', results: [], compatible: '', check: null, driver_src: '', bus: '', subsystem: '', fields: {}, name: '', goal: '', description: '' };
  renderWizard(); $('#dlg-wizard').showModal(); setTimeout(() => $('#wz-q')?.focus(), 50);
}
$('#wiz-cancel').onclick = () => $('#dlg-wizard').close();
$('#wiz-back').onclick = () => { if (W.step > 0) { saveFields(); W.step--; renderWizard(); } };
$('#wiz-next').onclick = () => guard(async () => {
  saveFields();
  if (W.step === 0) {
    if (!W.compatible) return toast('pick a match, or type the compatible string');
    W.check = await api('/api/wizard/check?compatible=' + encodeURIComponent(W.compatible));
    const g = W.check.guess || {}, hit = W.results.find(r => r.compatible === W.compatible) || {};
    W.bus = W.bus || g.bus || ''; W.subsystem = W.subsystem || g.subsystem || hit.subsystem || '';
    W.name = W.name || W.compatible.split(',').pop().replace(/[^a-z0-9-]/gi, '-');
  }
  if (W.step === 2 && !W.bus) return toast('choose how it is connected (bus)');
  if (W.step === STEPS.length - 1) return createDevice();
  W.step++; renderWizard();
});

function renderWizard() {
  $('#wiz-steps').innerHTML = STEPS.map((s, i) => `<span class="${i === W.step ? 'cur' : i < W.step ? 'done' : ''}">${i < W.step ? '✓' : i + 1}. ${s}</span>`).join('');
  $('#wiz-back').style.visibility = W.step ? 'visible' : 'hidden';
  $('#wiz-next').textContent = W.step === STEPS.length - 1 ? 'Create and open the guide →' : 'Next →';
  $('#wiz-body').innerHTML = [stepWhat, stepDriver, stepConnection, stepName][W.step]();
  if (W.step === 0) wireSearch();
  if (W.step === 2) $('#wz-bus').onchange = e => { saveFields(); W.bus = e.target.value; renderWizard(); };
}

function stepWhat() {
  return `<h2>What are you connecting?</h2>
    <p class="muted">Type the chip's part number (e.g. <code>bme280</code>, <code>imx219</code>, <code>ics43432</code>).
      Linux identifies hardware by a <b>compatible string</b>, <code>vendor,part</code>. We search your kernel tree for matches.</p>
    <input id="wz-q" class="big-input" placeholder="part number or compatible…" value="${esc(W.q)}" autocomplete="off">
    <div id="wz-results" class="results">${resultsHtml()}</div>
    <label class="small">Or type the exact compatible string yourself
      <input id="wz-compat" value="${esc(W.compatible)}" placeholder="vendor,part" autocomplete="off"></label>
    <p class="hint">Not sure of the vendor prefix? It's usually the maker's short name (<code>sony</code>, <code>bosch</code>, <code>ti</code>, <code>st</code>).
      If the part isn't in your kernel at all, still type it: the guide will show you how to bring in a driver.</p>`;
}
function resultsHtml() {
  if (!W.q) return '';
  if (!W.results.length) return `<div class="muted small">No compatible containing “${esc(W.q)}” in your kernel tree. That usually means <b>no driver yet</b>. Type the string below (e.g. <code>sony,${esc(W.q)}</code>).</div>`;
  return W.results.map(r => `<button class="res ${r.compatible === W.compatible ? 'sel' : ''}" data-c="${esc(r.compatible)}">
    <code>${esc(r.compatible)}</code>
    <span class="small">${r.driver ? `✓ driver <span class="muted">${esc(r.driver)}</span>` : '✗ no driver'} · ${r.binding ? '✓ binding' : 'no binding doc'}</span></button>`).join('');
}
function wireSearch() {
  let tm;
  $('#wz-q').oninput = e => { W.q = e.target.value.trim(); clearTimeout(tm); tm = setTimeout(() => guard(async () => {
    if (W.q.length < 2) { W.results = []; $('#wz-results').innerHTML = ''; return; }
    $('#wz-results').innerHTML = '<div class="muted small">searching your kernel tree…</div>';
    const r = await api('/api/wizard/search?q=' + encodeURIComponent(W.q));
    W.results = r.items; $('#wz-results').innerHTML = r.note ? `<div class="muted small">${esc(r.note)}</div>` : resultsHtml();
  }), 300); };
  $('#wz-results').onclick = e => { const b = e.target.closest('[data-c]'); if (!b) return; W.compatible = b.dataset.c; $('#wz-compat').value = W.compatible; $('#wz-results').innerHTML = resultsHtml(); };
  $('#wz-compat').oninput = e => { W.compatible = e.target.value.trim(); };
}

function stepDriver() {
  const c = W.check || {};
  let box;
  if (c.state === 'ok') box = `<div class="callout ok"><b>✓ Your kernel has a driver for <code>${esc(c.compatible)}</code>.</b><br>
      <code>${esc(c.driver.file)}</code> → <code>${esc(c.driver.config)}</code>${c.binding ? `<br>Binding: <code>${esc(c.binding.file)}</code>` : ''}
      <p class="small">The guide will check that this option is enabled on your board.</p></div>`;
  else if (c.state === 'not_built') box = `<div class="callout warn"><b>The driver source is in your tree but nothing builds it.</b><br><code>${esc(c.driver.file)}</code>
      <p class="small">The guide includes a <b>Driver</b> layer that adds the Makefile/Kconfig hooks.</p></div>`;
  else if (c.state === 'missing') box = `<div class="callout bad"><b>✗ No driver for <code>${esc(c.compatible)}</code> in your kernel.</b>
      <p>That's normal for newer or less common parts (many camera modules, for example). The guide adds a <b>Driver</b> layer that walks you through it:
      find a driver (vendor BSP, the module maker's kernel fork, a sibling driver), add it to the build, and compile it against your kernel.
      It also <b>reads the driver's C code</b> to tell you which supplies, clocks, GPIOs and lanes your device-tree node needs.</p>
      <label><span>Already have the driver's <code>.c</code> file? Path to it (optional, you can add it later)</span>
      <input id="wz-src" value="${esc(W.driver_src)}" placeholder="drivers-extra/${esc(c.compatible.split(',').pop())}.c" autocomplete="off"></label>
      <span class="hint">Relative to this workspace or the kernel tree, or an absolute path.</span></div>`;
  else box = `<div class="callout warn">Kernel source isn't set up, so the driver can't be looked up yet. You can continue; the guide will check later.</div>`;
  return `<h2>Is there a driver?</h2>${box}`;
}

function stepConnection() {
  const bus = KB.bus.find(b => b.id === W.bus), help = bus ? bus.form_help || {} : {};
  const fields = Object.entries(help).map(([k, h]) => `<label>${esc(k.replace(/_/g, ' '))}
      <input data-f="${esc(k)}" value="${esc(W.fields[k] ?? '')}" autocomplete="off"><span class="hint">${esc(h)}</span></label>`).join('');
  return `<h2>How is it connected?</h2>
    ${W.check?.guess?.bus || W.check?.guess?.subsystem ? '<p class="muted small">Pre-filled from where the driver and binding live in the kernel tree. Change it if your wiring differs.</p>' : ''}
    <div class="two-col">
      <label>Bus (the wires to the SoC)<select id="wz-bus"><option value="">choose…</option>${KB.bus.map(b => `<option value="${esc(b.id)}" ${b.id === W.bus ? 'selected' : ''}>${esc(b.title)}</option>`).join('')}</select>
        <span class="hint">${esc(bus?.summary || 'I2C, SPI, I2S/SAI, GPIO, USB, UART, MIPI CSI-2…')}</span></label>
      <label>Linux subsystem (how apps will see it)<select id="wz-sub"><option value="">choose…</option>${KB.subsystem.map(s => `<option value="${esc(s.id)}" ${s.id === W.subsystem ? 'selected' : ''}>${esc(s.title)}</option>`).join('')}</select>
        <span class="hint">${esc(KB.subsystem.find(s => s.id === W.subsystem)?.summary || '')}</span></label>
    </div>
    ${bus ? `<h3>Wiring <span class="muted small">(from your schematic; leave a field empty if you don't know yet, the guide will flag it)</span></h3><div class="two-col">${fields}</div>` : ''}
    <p class="hint">Pad names (e.g. GPIO_IO16): use the <b>Pins</b> tab in the workspace to see every function a pad can carry.</p>`;
}

function stepName() {
  return `<h2>Name it</h2>
    <label>Short name (used for labels and the file name)<input id="wz-name" value="${esc(W.name)}" autocomplete="off"></label>
    <label>What is it? (optional)<input id="wz-desc" value="${esc(W.description)}" placeholder="e.g. 12 MP autofocus camera module" autocomplete="off"></label>
    <label>What does "working" mean to you? (optional)<input id="wz-goal" value="${esc(W.goal)}" placeholder="e.g. capture 1080p frames into OpenCV" autocomplete="off"></label>
    <div class="callout"><b>Summary</b><br><code>${esc(W.compatible)}</code> on <b>${esc(W.bus)}</b> → <b>${esc(W.subsystem || 'other')}</b>
      ${W.check?.state === 'missing' ? '<br>Includes the <b>Driver</b> layer (no driver in your kernel yet).' : ''}</div>`;
}

function saveFields() {
  const v = id => $(id)?.value?.trim();
  if ($('#wz-compat')) W.compatible = v('#wz-compat');
  if ($('#wz-src')) W.driver_src = v('#wz-src');
  if ($('#wz-bus')) W.bus = v('#wz-bus');
  if ($('#wz-sub')) W.subsystem = v('#wz-sub');
  document.querySelectorAll('[data-f]').forEach(i => { W.fields[i.dataset.f] = i.value.trim(); });
  if ($('#wz-name')) { W.name = v('#wz-name'); W.goal = v('#wz-goal'); W.description = v('#wz-desc'); }
}

async function createDevice() {
  if (!W.name) return toast('give it a name');
  const form = { bus: W.bus, subsystem: W.subsystem || 'other', compatible: W.compatible, driver_src: W.driver_src,
    description: W.description, goal: W.goal, connection: {} };
  for (const [k, val] of Object.entries(W.fields)) { if (!val) continue; if (TOP_LEVEL.has(k)) form[k] = val; else form.connection[k] = val; }
  const r = await api('/api/peripheral/create', { method: 'POST', body: { id: W.name, form } });
  try { localStorage.setItem('bs.pid', r.id); } catch { }
  location.href = '/app?p=' + encodeURIComponent(r.id);
}

guard(async () => { await load(); if (location.hash === '#add') { history.replaceState(null, '', '/'); openWizard(); } });
