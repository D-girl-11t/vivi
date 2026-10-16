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
function toast(msg, ms = 2600, action) {
  const t = $('#toast'); t.textContent = msg;
  if (action) { const b = document.createElement('button'); b.textContent = action.label; b.onclick = () => { t.classList.remove('show'); action.run(); }; t.append(' ', b); }
  t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), ms);
}
async function guard(fn) { try { return await fn(); } catch (e) { toast('⚠ ' + e.message, 6000); console.error(e); } }

let O = null, KB = null;

// ------------------------------------------------------------ page
async function load() {
  [O, KB] = await Promise.all([api('/api/overview'), api('/api/knowledge')]);
  renderSetup(); renderDevices(); renderLadder(); renderAi();
}

function renderSetup() {
  const st = O.status, src = st.source, b = st.board, sc = b.scanned, c = O.config;
  const live = sc && !sc.demo, mm = (v => v ? v.split('.').slice(0, 2).join('.') : null);
  const mismatch = live && b.release && src.version && mm(b.release) !== mm(src.version);
  const srcState = src.ok && !src.warnings.length ? 'ok' : (src.ok ? 'warn' : 'todo');
  const brdState = live ? 'ok' : (sc ? 'warn' : 'todo');

  // what the tool can do right now, in plain words
  const cap = (on, label, why) => `<span class="cap ${on}" title="${esc(why)}"><span class="state-dot ${on}"></span>${label}</span>`;
  let h = `<div class="caps">
    ${cap(live ? 'ok' : (sc ? 'warn' : 'todo'), 'Live board checks', 'driver enabled, .dtb booted, driver bound, /dev nodes, tools: needs the board over ssh')}
    ${cap(src.ok ? 'ok' : 'todo', 'Source checks & file links', 'pins, controller, device node, opening the .dts at the right line: needs the kernel source')}
    ${cap(src.tree_ok ? 'ok' : 'todo', 'Driver & pin lookups', 'wizard search, is-there-a-driver, CONFIG_ names, reading the driver code, Pins tab: needs the kernel source')}
  </div>`;
  if (mismatch) h += `<div class="callout warn"><b>⚠ Board and source are different kernels.</b> The board runs <code>${esc(b.release)}</code>, but the source tree is
    <code>${esc(src.version)}</code>${src.flavor ? ` (${esc(src.flavor)})` : ''}. Steps would compare your board against code it isn't running.
    Point <b>Kernel source</b> at the tree that built the board's kernel.</div>`;

  const card = (state, title, stateText, body, uses, btn) => `<div class="setup-card ${state}">
    <div class="sc-head"><span class="state-dot ${state}"></span><b>${title}</b><span class="sc-state ${state}">${stateText}</span></div>
    <div class="sc-body">${body}</div>
    <div class="sc-uses">${uses}</div>${btn}</div>`;
  const kv = (k, v, title) => v ? `<div class="kv"><span>${k}</span><span class="ellipsis" title="${esc(title || v)}">${v}</span></div>` : '';
  const warns = list => list.map(w => `<div class="sc-warn">⚠ ${esc(w)}</div>`).join('');

  h += '<div class="setup-grid">';
  h += card(st.soc.id ? 'ok' : 'todo', 'SoC profile', st.soc.id ? (st.soc.status === 'starter' ? 'starter' : 'ready') : 'missing',
    st.soc.id ? kv('Chip', esc(st.soc.title)) + kv('Profile', `<code>knowledge/soc/${esc(st.soc.id)}.yaml</code>`) : warns([`profile '${c.soc}' not found`]),
    'How this chip names its pins and controllers, and its camera layout.', `<button data-act="setup">Change</button>`);

  h += card(srcState, 'Kernel source', src.ok ? (src.warnings.length ? 'check' : 'connected') : (src.configured ? 'problem' : 'not set'),
    (src.tree_ok ? kv('Kernel', `${esc(src.version || '?')}${src.flavor ? ` <span class="muted">· ${esc(src.flavor)}</span>` : ''}`)
      + kv('Board file', `<code>${esc((src.board_dts || '').split('/').pop())}</code>`, src.board_dts)
      + kv('Folder', esc(src.kernel), src.kernel)
      : `<p class="muted">The kernel tree that built your board's kernel: your Yocto <code>kernel-source</code>, or your vendor's kernel at the matching tag.</p>`)
    + warns(src.warnings),
    'Used for: editing guidance, pin names, finding drivers.',
    `<button data-act="setup" class="${src.ok && !src.warnings.length ? '' : 'primary'}">${src.configured ? 'Change' : 'Set up'}</button>`);

  h += card(brdState, 'Board', live ? 'connected' : (sc ? 'demo data' : (b.live ? 'not scanned' : 'not connected')),
    live ? kv('Model', esc(b.model || '?')) + kv('Kernel', esc(b.release || '?')) + kv('Via', `<code>${esc(b.transport)}</code>`)
      + kv('Scanned', esc(sc.time)) + kv('Drivers', `${b.findings.bound} bound · <b class="${b.findings.unbound.length ? 'err' : ''}">${b.findings.unbound.length} not bound</b> · ${b.findings.deferred.length} deferred`)
      : sc ? `<p class="muted">Showing a <b>demo snapshot</b>: the device tree is real, but drivers, kernel config and dmesg are made up. Connect your board to see the real thing.</p>`
        : `<p class="muted">${b.live ? 'Connected settings saved. Press <b>Scan now</b> to read the board.' : 'Reach the board over ssh to see what is really running on it.'}</p>`,
    'Used for: checking what really runs, which drivers bound, dmesg errors.',
    `<div class="row-btns"><button data-act="setup" class="${live ? '' : 'primary'}">${c.board.transport === 'ssh' ? 'Change' : 'Connect'}</button>${b.live ? '<button data-act="scan">⟳ Scan now</button>' : ''}</div>`);
  h += '</div>';
  $('#setup-cards').innerHTML = h;
}

function renderDevices() {
  const cards = O.items.map(d => {
    const pct = d.total ? Math.round(100 * d.done / d.total) : 0;
    return `<a class="dev-card" href="/app?p=${encodeURIComponent(d.id)}">
      <div class="dc-head"><b>${esc(d.name)}</b><span class="spacer"></span>${d.unfilled?.includes('compatible') ? '<span class="pill">not filled in</span>' : ''}${d.needs_driver ? '<span class="pill warn" title="No driver for this device in your kernel tree: the guide includes the Driver layer">needs a driver</span>' : ''}<button class="dc-del" data-del="${esc(d.id)}" title="Delete this device (moves its file to the trash, you can undo)">🗑</button></div>
      <div class="muted small">${esc(d.bus || '?')} → ${esc(d.subsystem || '?')}</div>
      ${d.error ? `<div class="err small">⚠ ${esc(d.error)}</div>` : d.unfilled?.includes('compatible') ? `
      <div class="callout warn small">Still the <b>blank template</b> (<code>compatible: vendor,chip</code>). Open it and fill in the form,
        or delete <code>peripherals/${esc(d.id)}.yaml</code> and use <b>＋ Add a device</b>.</div>` : `
      <div class="bar"><span style="width:${pct}%"></span></div>
      <div class="small">${d.done}/${d.total} steps done</div>
      ${d.problems?.length ? `<div class="small card-problem" title="${esc(d.problems.join('\n'))}">⚠ ${esc(d.problems[0])}${d.problems.length > 1 ? ` (+${d.problems.length - 1} more)` : ''}</div>` : ''}
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
  const state = !a.enabled ? '<span class="pill">off</span>' : a.ready ? '<span class="pill ok">on</span>' : '<span class="pill warn">not ready</span>';
  $('#ai-card').innerHTML = `<h3>✦ Ask Claude ${state}</h3>
    <p>For what the rules can't cover, like a strange dmesg error, porting a driver to your kernel version, or "why is my mic silent?",
      every step has <b>✦ Ask Claude</b>, and the workspace has <b>✦ Ask</b> for the whole device. Your question goes out
      together with the tool's evidence (form, checks, what the driver reads, dmesg, your board-file block), you can
      ask follow-ups, and you can always see exactly what was sent.</p>
    <p class="small">Using: <b>${esc(a.label)}</b>.
      ${a.backend === 'claude-code' ? 'This runs on your Claude Code login, so there is no API key to manage. Tools are disabled: it reads the evidence and answers, and never touches your files.' : 'Uses ANTHROPIC_API_KEY from the environment; the key is never stored.'}
      ${!a.enabled ? 'Turn it on with <code>ai: {enabled: true}</code> in workspace.yaml.' : (!a.ready ? `<span class="err">${a.backend === 'claude-code' ? 'The claude CLI was not found on PATH.' : 'ANTHROPIC_API_KEY is not set.'}</span>` : '')}</p>`;
}

async function deleteDevice(id) {
  if (!confirm(`Delete "${id}"?\n\nIts form (peripherals/${id}.yaml) and ticked-off steps move to .state/trash/. Your kernel source and board are not touched.`)) return;
  const r = await api('/api/peripheral?id=' + encodeURIComponent(id), { method: 'DELETE' });
  try { if (localStorage.getItem('bs.pid') === id) localStorage.removeItem('bs.pid'); } catch { }
  await load();
  toast(`Deleted ${id}`, 8000, { label: 'Undo', run: () => guard(async () => { await api('/api/peripheral/restore', { method: 'POST', body: { name: r.trashed } }); await load(); toast('restored ' + id); }) });
}

document.addEventListener('click', e => {
  const del = e.target.closest('[data-del]');
  if (del) { e.preventDefault(); e.stopPropagation(); guard(() => deleteDevice(del.dataset.del)); return; }
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
  const sync = () => { $('.ssh-only').classList.toggle('hidden', f['board.transport'].value !== 'ssh'); renderReach(); $('#test-out').innerHTML = ''; };
  f['board.transport'].onchange = sync; f['board.host'].oninput = renderReach; sync();
  $('#reach-help').open = !(O.status.board.scanned && !O.status.board.scanned.demo);
  $('#dlg-setup').showModal();
}
// step-by-step help for the selected way of reaching the board; commands use the target you typed
const cmd = c => `<div class="cmd"><pre>${esc(c)}</pre><button type="button" class="copy-cmd" title="copy">copy</button></div>`;
function renderReach() {
  const f = $('#setup-form'), kind = f['board.transport'].value;
  const host = f['board.host'].value.trim() || 'root@<board-ip>', ip = host.split('@').pop();
  const step = (n, title, body) => `<li><b>${title}</b>${body}</li>`;
  let h = '';
  if (kind === 'ssh') h = `<p class="muted small">The tool logs in to the board over the network and runs read-only commands.
      You only need the serial cable once, to find the board's address.</p><ol class="reach">
    ${step(1, 'Connect the cables', `<p>Power, the board's <b>debug USB</b> port to your laptop (serial console), and <b>Ethernet</b> to your router or straight to your laptop.</p>`)}
    ${step(2, 'Open the serial console', `<p>On your laptop. The board usually shows up as one or two ports; the Linux console is on one of them.</p>
      ${cmd('ls /dev/ttyACM* /dev/ttyUSB*')}${cmd('sudo minicom -D /dev/ttyACM0 -b 115200')}
      <p>Power the board on, wait for <code>login:</code> and log in as <code>root</code>. Nothing shows up? Try the other port.</p>`)}
    ${step(3, 'Find the board\'s address', `<p>In the serial console, look for <code>inet 192.168.x.y</code> under <code>eth0</code> or <code>eth1</code>:</p>${cmd('ip addr')}
      <p>No address? <code>udhcpc -i eth0</code>. <b>Cable straight to your laptop?</b> Give both ends a fixed address:</p>
      ${cmd('ip addr add 192.168.7.2/24 dev eth0 && ip link set eth0 up      # on the board')}
      ${cmd('sudo ip addr add 192.168.7.1/24 dev <your-ethernet>             # on the laptop (see: ip link)')}`)}
    ${step(4, 'Give root a password', `<p>Most SSH servers refuse logins with an empty password. On the board:</p>${cmd('passwd')}`)}
    ${step(5, 'Let your laptop log in without a password', `<p>The tool can't type passwords, so it uses a key. On your laptop:</p>
      ${cmd(`ssh ${host}          # first time: type "yes", then the password`)}
      ${cmd(`ssh-copy-id ${host}  # no key yet? first run: ssh-keygen -t ed25519`)}
      ${cmd(`ssh -o BatchMode=yes ${host} uname -r   # must print a version with NO password prompt`)}`)}
    ${step(6, 'Fill in the SSH target above and press Test connection', `<p>Then <b>Save</b> and <b>⟳ Scan now</b> on the home page.</p>`)}
  </ol>`;
  else if (kind === 'local') h = `<p class="muted small">Use this when the tool itself runs <b>on the board</b>, for example on a board with a desktop,
      or when you open it from your laptop's browser over the network.</p><ol class="reach">
    ${step(1, 'Copy the tool to the board', `${cmd('scp -r bringup-studio root@<board-ip>:/root/')}`)}
    ${step(2, 'Check it has what it needs', `<p>python3 with PyYAML and Jinja2:</p>${cmd(`python3 -c "import yaml, jinja2; print('ok')"`)}`)}
    ${step(3, 'Start it on the board and open it from your laptop', `${cmd('cd /root/bringup-studio && ./run.sh --host 0.0.0.0')}
      <p>Then browse to <code>http://&lt;board-ip&gt;:8765</code>. <b>Only on a network you trust</b>: the page has no login.</p>`)}
    ${step(4, 'Kernel source', `<p>The kernel source usually lives on your laptop, not the board. For source checks, run the tool on the laptop with <b>ssh</b> instead.</p>`)}
  </ol>`;
  else h = `<p class="muted small">No board needed: the tool reads a saved scan. Good for learning offline, or for comparing before and after a change.</p><ol class="reach">
    ${step(1, 'Use the demo', `<p><code>snapshots/demo-imx93-frdm.json</code> has a real device tree, but its drivers, kernel config and dmesg are <b>made up</b>.</p>`)}
    ${step(2, 'Or save your own', `<p>Connect once over <b>ssh</b>, scan, then in the workspace's Board tab click <b>Save snapshot…</b>.
      Set <code>board.snapshot</code> in workspace.yaml to that file.</p>`)}
  </ol>`;
  $('#reach-steps').innerHTML = h;
}
document.addEventListener('click', e => {
  const b = e.target.closest('.copy-cmd'); if (!b) return;
  const t = b.previousElementSibling.innerText.replace(/\s+#.*$/, '');
  navigator.clipboard.writeText(t).then(() => toast('copied: ' + t), () => toast('select the text and copy it manually'));
});
$('#btn-test').onclick = () => guard(async () => {
  const f = $('#setup-form'), out = $('#test-out'), b = $('#btn-test');
  b.disabled = true; out.innerHTML = '<span class="muted">trying…</span>';
  try {
    const r = await api('/api/board/test', { method: 'POST', body: { transport: f['board.transport'].value, host: f['board.host'].value } });
    out.innerHTML = r.ok
      ? `<span class="okc">✓ ${esc(r.summary)}</span>${(r.notes || []).map(n => `<div class="muted">• ${esc(n)}</div>`).join('')}`
      : `<span class="err">✗ ${esc(r.summary)}</span>${r.fix ? `<div><b>Fix:</b> ${esc(r.fix)}</div>` : ''}${r.detail ? `<div class="muted mono">${esc(r.detail)}</div>` : ''}`;
  } finally { b.disabled = false; }
});

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
      <label><span>Found a driver? Paste the path to its <code>.c</code> file, <b>or a web link</b> (a GitHub branch or file page works; the guide downloads it). Optional, you can add it later.</span>
      <input id="wz-src" value="${esc(W.driver_src)}" placeholder="drivers-extra/${esc(c.compatible.split(',').pop())}.c" autocomplete="off"></label>
      <span class="hint">Relative to this workspace or the kernel tree, or an absolute path.</span></div>`;
  if (c.vendor_known === false) box = `<div class="callout warn"><b>⚠ “${esc(c.compatible.split(',')[0])}” is not a registered vendor prefix.</b>
      Compatible strings start with the <b>chip maker's</b> prefix (OmniVision = <code>ovti</code>, Sony = <code>sony</code>, Bosch = <code>bosch</code>).
      Go back and fix it, or the driver will never match.</div>` + (box || '');
  if (!box) box = `<div class="callout warn">Kernel source isn't set up, so the driver can't be looked up yet. You can continue; the guide will check later.</div>`;
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
