// Bring-up Studio homepage: setup state, device list, and the "add a device" wizard.
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
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
  O.ping = await api('/api/board/ping').catch(() => ({}));
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
    ${cap(live && b.live && O.ping?.reachable !== false ? 'ok' : (sc ? 'warn' : 'todo'), 'Live board checks', 'driver enabled, .dtb booted, driver bound, /dev nodes, tools: needs the board over ssh')}
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
    'How this chip names its pins and controllers, and its camera layout.', `<button data-act="setup-soc">Change</button>`);

  h += card(srcState, 'Kernel source', src.ok ? (src.warnings.length ? 'check' : 'connected') : (src.configured ? 'problem' : 'not set'),
    (src.tree_ok ? kv('Kernel', `${esc(src.version || '?')}${src.flavor ? ` <span class="muted">· ${esc(src.flavor)}</span>` : ''}`)
      + kv('Board file', `<code>${esc((src.board_dts || '').split('/').pop())}</code>`, src.board_dts)
      + kv('Folder', esc(src.kernel), src.kernel)
      : `<p class="muted">The kernel tree that built your board's kernel: your Yocto <code>kernel-source</code>, or your vendor's kernel at the matching tag.</p>`)
    + checksHtml((src.checks || []).filter(c => c.level !== 'ok'))
    + (src.get_source && !(src.tree_ok && !src.warnings.length) ? `<div class="get-src"><b>Get the exact source your board runs</b>
        <span class="muted small">(board kernel <code>${esc(src.get_source.release)}</code>${src.get_source.commit ? `, commit <code>${esc(src.get_source.commit)}</code>` : ''})</span>
        ${cmd(src.get_source.cmd)}<span class="muted small">On your laptop (≈ 300 MB). Then set Kernel source to that folder.</span></div>` : ''),
    'Used for: editing guidance, pin names, finding drivers.',
    `<button data-act="${!src.ok && live ? 'detect' : 'setup-source'}" class="${src.ok && !src.warnings.length ? '' : 'primary'}">${!src.ok && live ? '✨ Detect from my board' : (src.configured ? 'Change' : 'Set up')}</button>`);

  const off = c.board.transport === 'off', ping = O.ping || {}, lost = b.live && ping.reachable === false;
  h += card(lost ? 'todo' : off ? 'warn' : brdState, 'Board', lost ? 'not answering' : off ? 'disconnected' : live ? 'connected' : (sc ? 'demo data' : (b.live ? 'not scanned' : 'not connected')),
    lost ? `<div class="sc-warn">⚠ ${esc(c.board.host)} is not answering: ${esc((ping.reason || 'no reply').replace(/\.$/, ''))}.
        ${ping.last_ok ? `Last answered at ${esc(ping.last_ok)}.` : ''}</div>
      <p class="small">Usually: it is rebooting, it lost Wi-Fi after a reboot, or its IP changed. Open the <b>serial console</b> to see what it's doing
        (no network needed). The last scan stays visible meanwhile.</p>` :
    off ? `<p class="muted">Not talking to the board. ${sc && !sc.demo ? `Still showing the <b>last scan</b> (${esc(sc.time)}) so you can keep reading; board-side checks show “?” until you reconnect.` : ''}</p>` :
    live ? kv('Model', esc(b.model || '?')) + kv('Kernel', esc(b.release || '?')) + kv('Via', `<code>${esc(b.transport)}</code>`)
      + kv('Scanned', esc(sc.time)) + kv('Drivers', `${b.findings.bound} bound · <b class="${b.findings.unbound.length ? 'err' : ''}">${b.findings.unbound.length} not bound</b> · ${b.findings.deferred.length} deferred`)
      : sc ? `<p class="muted">Showing a <b>demo snapshot</b>: the device tree is real, but drivers, kernel config and dmesg are made up. Connect your board to see the real thing.</p>`
        : `<p class="muted">${b.live ? 'Connected settings saved. Press <b>Scan now</b> to read the board.' : 'Reach the board over ssh to see what is really running on it.'}</p>`,
    'Used for: checking what really runs, which drivers bound, dmesg errors.',
    `<div class="row-btns">${lost
      ? `<a class="btn primary" href="/app?tab=serial">🖥 Serial console</a><button data-act="recheck">↻ Check again</button><button data-act="setup-board">Change</button>`
      : b.live
      ? `<button data-act="scan">⟳ Scan now</button><button data-act="setup-board">Change</button><button data-act="disconnect" title="Stop contacting the board; keep showing the last scan">⏏ Disconnect</button>`
      : (c.board.transport === 'off' && c.board.host
        ? `<button data-act="reconnect" class="primary">🔌 Connect to ${esc(c.board.host)}</button><button data-act="setup-board">Change</button>`
        : `<button data-act="setup-board" class="primary">Connect</button>`)}</div>`);
  h += '</div>';
  $('#setup-cards').innerHTML = h;
}

// a list of source checks: ✓ / ⚠ / ✗ with the fix under each
function checksHtml(list) {
  const icon = { ok: '✓', warn: '⚠', error: '✗' };
  return list.map(c => `<div class="chk ${c.level}"><span class="ci">${icon[c.level]}</span><div>${esc(c.text)}${c.fix ? `<div class="cfix">${esc(c.fix)}</div>` : ''}</div></div>`).join('');
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
  if (t.dataset.act.startsWith('setup')) openSetup(t.dataset.act.split('-')[1] || 'source');
  if (t.dataset.act === 'detect') { openSetup('source'); $('#btn-detect').click(); }
  if (t.dataset.act === 'recheck') guard(async () => { O.ping = await api('/api/board/ping?force=1'); renderSetup(); toast(O.ping.reachable ? 'the board answers again' : 'still not answering'); });
  if (t.dataset.act === 'disconnect') guard(async () => { await api('/api/board/disconnect', { method: 'POST' }); await load(); toast('disconnected: showing the last scan'); });
  if (t.dataset.act === 'reconnect') guard(async () => { t.disabled = true; t.textContent = 'connecting…';
    const r = await api('/api/board/connect', { method: 'POST' });
    if (r.ok) { await api('/api/scan', { method: 'POST' }); await load(); toast('connected and scanned'); }
    else { await load(); toast(`✗ ${r.summary}${r.fix ? ' · ' + r.fix : ''}`, 9000); } });
  if (t.dataset.act === 'wizard') openWizard();
  if (t.dataset.act === 'scan') guard(async () => { t.disabled = true; t.textContent = 'scanning…'; await api('/api/scan', { method: 'POST' }); await load(); toast('scan done'); });
});

// ------------------------------------------------------------ setup dialog
const SETUP = {
  soc: ['SoC profile', 'Which chip family the tool should assume: how it names pins and controllers.', ['soc']],
  source: ['Kernel source', 'Files on this laptop: the kernel tree your board runs, and your board\'s .dts inside it. This is what you edit.', ['kernel_src', 'board_dts']],
  board: ['Board connection', 'The live hardware: how the tool reaches the running board to see what is really loaded.', ['board.transport', 'board.host']],
};
let setupKind = 'source';
function openSetup(kind = 'source') {
  setupKind = kind;
  const [title, intro] = SETUP[kind];
  $('#setup-title').textContent = title; $('#setup-intro').textContent = intro;
  document.querySelectorAll('#setup-form [data-sec]').forEach(d => d.classList.toggle('hidden', d.dataset.sec !== kind));
  $('#detect-out').innerHTML = '';
  const f = $('#setup-form'), c = O.config;
  f.soc.innerHTML = O.socs.map(s => `<option value="${esc(s.id)}">${esc(s.title)}${s.status === 'starter' ? ' (starter)' : ''}</option>`).join('');
  f.soc.value = c.soc || '';
  f.kernel_src.value = c.kernel_src || ''; f.board_dts.value = c.board_dts || '';
  f['board.transport'].value = c.board.transport || 'snapshot'; f['board.host'].value = c.board.host || '';
  let vt; const revalidate = () => { clearTimeout(vt); vt = setTimeout(validateSrc, 400); };
  f.kernel_src.oninput = revalidate; f.board_dts.oninput = revalidate; f.soc.onchange = revalidate; if (kind === 'source') validateSrc();
  const sync = () => { const k = f['board.transport'].value; $('.ssh-only').classList.toggle('hidden', k !== 'ssh');
    $('.test-row').classList.toggle('hidden', k === 'off'); renderReach(); $('#test-out').innerHTML = ''; };
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
    ${step(1, 'Connect the cables', `<p>Power, and the board's <b>debug USB</b> port to your laptop (serial console). For the network, use either
      <b>Ethernet</b> (to your router or straight to your laptop) or the board's <b>Wi-Fi</b> (step 3 shows both).</p>`)}
    ${step(2, 'Open the serial console', `<p>On your laptop. The board usually shows up as one or two ports; the Linux console is on one of them.</p>
      ${cmd('ls /dev/ttyACM* /dev/ttyUSB*')}${cmd('sudo minicom -D /dev/ttyACM0 -b 115200')}
      <p>Power the board on, wait for <code>login:</code> and log in as <code>root</code>. Nothing shows up? Try the other port.</p>`)}
    ${step(3, 'Get the board on the network and find its address', `
      <div class="net-tabs"><b>Ethernet</b></div>
      <p>In the serial console, look for <code>inet 192.168.x.y</code> under <code>eth0</code> or <code>eth1</code>:</p>${cmd('ip addr')}
      <p>No address? <code>udhcpc -i eth0</code>. <b>Cable straight to your laptop?</b> Give both ends a fixed address:</p>
      ${cmd('ip addr add 192.168.7.2/24 dev eth0 && ip link set eth0 up      # on the board')}
      ${cmd('sudo ip addr add 192.168.7.1/24 dev <your-ethernet>             # on the laptop (see: ip link)')}
      <div class="net-tabs"><b>Wi-Fi</b></div>
      <p>Find the Wi-Fi interface (<code>wlan0</code>, or <code>mlan0</code> on NXP Wi-Fi chips), then connect with whichever tool the image has:</p>
      ${cmd('ip link')}
      ${cmd('nmcli dev wifi connect "YourWiFi" password "YourPassword"     # if nmcli exists')}
      ${cmd('connmanctl enable wifi; connmanctl scan wifi; connmanctl services   # if connmanctl exists; then: connmanctl agent on; connmanctl connect wifi_..._psk')}
      ${cmd('wpa_passphrase "YourWiFi" "YourPassword" > /etc/wpa_supplicant.conf && wpa_supplicant -B -i wlan0 -c /etc/wpa_supplicant.conf && udhcpc -i wlan0   # works everywhere')}
      ${cmd('ip addr show wlan0      # the "inet" number is the board\'s address')}
      <p class="muted">The board and your laptop must be on the same network. No Wi-Fi interface? Check <code>dmesg | grep -iE "wlan|mlan|firmware"</code>.
        Wi-Fi addresses can change after a reboot: reserve one in your router.</p>`)}
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
  else if (kind === 'off') h = `<p class="muted small">The tool stops contacting the board. The last scan stays visible; board-side checks show “?”. Your SSH target is kept, so reconnecting is one click on the Board card.</p>`;
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

$('#btn-detect').onclick = () => guard(async () => {
  const f = $('#setup-form'), out = $('#detect-out'), b = $('#btn-detect');
  b.disabled = true; out.innerHTML = '<div class="muted small">looking at the board and searching your home folder for kernel trees…</div>';
  try {
    const r = await api('/api/source/detect', { method: 'POST', body: { soc: O.config.soc } });
    if (r.best) {
      f.kernel_src.value = r.best.kernel_src; if (r.best.board_dts) f.board_dts.value = r.best.board_dts;
      out.innerHTML = `<div class="chk ok"><span class="ci">✓</span><div>Filled in from the board (<code>${esc(r.board.release)}</code>, <code>${esc(r.board.compatible[0] || '')}</code>):
        ${esc(r.best.why.join(' · '))}${r.note ? `<div class="cfix">${esc(r.note)}</div>` : ''}
        ${r.candidates.length > 1 ? `<div class="cfix">Other trees found: ${r.candidates.slice(1, 4).map(c => esc(c.kernel_src)).join(', ')}</div>` : ''}</div></div>`;
      validateSrc();
    } else {
      const gs = O.status.source.get_source;
      out.innerHTML = `<div class="chk error"><span class="ci">✗</span><div>${esc(r.error || 'nothing found')}
        ${(r.candidates || []).length ? `<div class="cfix">Found but not matching: ${r.candidates.slice(0, 3).map(c => `${esc(c.kernel_src)} (${esc(c.version || '?')})`).join(', ')}</div>` : ''}
        ${gs ? `<div class="cfix">Download the matching source, then press Detect again:</div>${cmd(gs.cmd)}` : ''}</div></div>`;
    }
  } finally { b.disabled = false; }
});

$('#dts-url').oninput = e => { const n = $('#dts-name'); if (!n.dataset.touched) n.value = (e.target.value.split('/').pop() || '').replace(/\.dts.*$/, '').replace(/[^\w.+-]/g, '') + (e.target.value ? '-web' : ''); };
$('#dts-name').oninput = e => { e.target.dataset.touched = 1; };
$('#btn-dts-fetch').onclick = () => guard(async () => {
  const f = $('#setup-form'), out = $('#dts-out'), b = $('#btn-dts-fetch');
  b.disabled = true; out.innerHTML = '<span class="muted small">downloading…</span>';
  try {
    const r = await api('/api/source/fetch-dts', { method: 'POST', body: { url: $('#dts-url').value.trim(), name: $('#dts-name').value.trim(), kernel_src: f.kernel_src.value.trim() } });
    f.board_dts.value = r.path;
    out.innerHTML = `<div class="chk ${r.check.ok ? 'ok' : 'warn'}"><span class="ci">${r.check.ok ? '✓' : '⚠'}</span><div>Saved as <code>${esc(r.path)}</code> (${esc(r.kind)} file, ${r.bytes} bytes) and selected above.
      ${r.kind !== 'board' ? `<div class="cfix">This is ${r.kind === 'overlay' ? 'an overlay (/plugin/)' : 'not a complete board file'}: it can't be used as the board file by itself.</div>` : ''}
      <div class="cfix">Press Save to use it. The checks above tell you whether it fits your tree and your board.</div></div></div>`;
    validateSrc();
  } catch (e) { out.innerHTML = `<div class="chk error"><span class="ci">✗</span><div>${esc(e.message)}</div></div>`; }
  finally { b.disabled = false; }
});

let lastCheck = null;
async function validateSrc() {
  const f = $('#setup-form'), box = $('#src-check');
  box.innerHTML = '<span class="muted small">checking the kernel source…</span>';
  try {
    lastCheck = await api('/api/source/validate', { method: 'POST', body: { kernel_src: f.kernel_src.value, board_dts: f.board_dts.value, soc: setupKind === 'soc' ? f.soc.value : O.config.soc } });
    box.innerHTML = `<div class="small"><b>${lastCheck.ok ? '✓ Kernel source looks good' : '✗ Kernel source needs fixing'}</b></div>` + checksHtml(lastCheck.checks);
  } catch (e) { box.innerHTML = `<div class="chk error"><span class="ci">✗</span><div>Could not check: ${esc(e.message)}</div></div>`; lastCheck = null; }
}

$('#setup-save').onclick = e => {
  e.preventDefault();
  const f = $('#setup-form');
  guard(async () => {
    if (setupKind === 'source') {
      await validateSrc();
      if (lastCheck && !lastCheck.ok && !confirm('The kernel source settings have errors (shown in red). The guide will not be able to read your files until they are fixed.\n\nSave anyway?')) return;
    }
    const body = {}; SETUP[setupKind][2].forEach(k => { body[k] = f[k].value.trim(); });
    await api('/api/config/fields', { method: 'POST', body });
    $('#dlg-setup').close(); toast('saved'); await load();
  });
};

// ------------------------------------------------------------ add-a-device wizard
const STEPS = ['What is it?', 'Driver', 'Connection', 'Name'];
const TOP_LEVEL = new Set(['format', 'codec_ref', 'card_name']);
let W;

function openWizard() {
  W = { step: 0, q: '', results: [], find: null, web: null, compatible: '', check: null, driver_src: '', bus: '', subsystem: '', fields: {}, name: '', goal: '', description: '' };
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
  $('#wiz-body').onclick = null;
  if (W.step === 0) wireSearch();
  if (W.step === 2) $('#wz-bus').onchange = e => { saveFields(); W.bus = e.target.value; renderWizard(); };
}

// step 1 is the Part finder: from whatever you know (product name, chip marking, part number) to the compatible string
function stepWhat() {
  const ai = O.ai || {};
  return `<h2>What are you connecting?</h2>
    <p class="muted">Type <b>anything you know</b>: the product name, the part number printed on the chip, or a line from the shop page
      (e.g. <code>ICS-43434 mic breakout</code>, <code>Sony IMX519</code>, <code>bme280</code>). Linux names hardware by a
      <b>compatible string</b>, <code>vendor,part</code>, and the Part finder looks it up in your kernel tree.</p>
    <input id="wz-q" class="big-input" placeholder="product name, chip marking or part number…" value="${esc(W.q)}" autocomplete="off">
    <div id="wz-results" class="results">${resultsHtml()}</div>
    <div id="wz-web">${webHtml()}</div>
    <details class="reach-help" ${W.web ? '' : ''}><summary>Where do I find the part number?</summary><ul class="small">
      <li><b>Bought a module or breakout board?</b> What matters is the <b>chip on it</b>. The product page's spec table usually names it
        ("Sensor: Sony IMX519", "Microphone: TDK InvenSense ICS-43434").</li>
      <li><b>The chip itself:</b> the marking printed on it, then the maker's datasheet (bus, pins, supplies).</li>
      <li><b>The vendor's Linux code</b> (often on GitHub): its device-tree example shows the exact compatible.</li>
      <li>Only know the product name? Use <b>🌐 Identify the chips</b>${ai.enabled && ai.ready ? '' : ' (needs the AI helper)'}: Claude searches the web for what's on it.</li></ul></details>
    <label class="small">Or type the exact compatible string yourself
      <input id="wz-compat" value="${esc(W.compatible)}" placeholder="vendor,part" autocomplete="off"></label>`;
}
const BOARD_CFG = v => v == null ? '' : v === 'y' ? '<span class="ok-t">built into your board\'s kernel</span>' :
  v === 'm' ? '<span class="ok-t">a module on your board</span>' : '<span class="warn-t">not enabled on your board</span>';
function hitHtml(h) {
  return `<button class="res ${h.compatible === W.compatible ? 'sel' : ''}" data-c="${esc(h.compatible)}">
    <span><code>${esc(h.compatible)}</code>${h.subsystem ? ` <span class="pill">${esc(h.subsystem)}</span>` : ''}</span>
    <span class="small">${h.driver ? `✓ driver <span class="muted">${esc(h.driver)}</span> · <code>${esc(h.config || '?')}</code> ${BOARD_CFG(h.board_config)}` : '✗ no driver in your kernel'}
      · ${h.binding ? '✓ binding' : 'no binding doc'}</span></button>`;
}
function partHtml(p, vendorHint) {
  let h = '';
  if (p.kind === 'exact') h += `<div class="pf-head">Matches for <b>${esc(p.token)}</b>:</div>`;
  else if (p.kind === 'family') h += `<div class="callout small"><b>No exact match for ${esc(p.token)}</b>, but your kernel has a driver for the same chip family
      (<code>${esc(p.family)}…</code>). Siblings usually share one driver when they speak the same interface: check the datasheet or
      the binding text says so, then pick it.</div>`;
  else h += `<div class="callout warn small"><b>Nothing in your kernel for ${esc(p.token)}</b>. Usually that means <b>no driver yet</b>:
      you'll need the maker's driver (the guide adds the Driver steps).
      ${vendorHint ? `<br>Its compatible is then most likely <button class="chip-btn" data-c="${esc(vendorHint + ',' + p.token)}">${esc(vendorHint + ',' + p.token)}</button> (vendor prefix of the maker you typed).` : ''}</div>`;
  h += p.hits.map(hitHtml).join('');
  if (p.mentions.length) h += `<details class="small pf-ment"><summary>Mentioned in ${p.mentions.length} kernel file${p.mentions.length > 1 ? 's' : ''}</summary>${p.mentions.map(m =>
    `<div><code>${esc(m.file)}:${m.line}</code> <span class="muted">${esc(m.text)}</span>
      ${(m.compatibles || []).map(c => `<button class="chip-btn" data-c="${esc(c)}">${esc(c)}</button>`).join('')}${m.config ? ` <code>${esc(m.config)}</code>` : ''}</div>`).join('')}</details>`;
  return h;
}
function resultsHtml() {
  const r = W.find;
  if (!W.q || !r) return '';
  if (r.error) return `<div class="callout warn small">${esc(r.error)}</div>`;
  if (!r.parts.length) return `<div class="muted small">No part number in that text (part numbers mix letters and digits, like <code>imx519</code>). Try the chip's marking, or 🌐 Identify the chips.</div>`;
  return r.parts.map(p => partHtml(p, r.vendor_hint)).join('<hr class="pf-sep">');
}
function webHtml() {
  const ai = O.ai || {}, w = W.web;
  let h = `<div class="pf-web-row"><button id="wz-webgo" ${ai.enabled && ai.ready && W.q ? '' : 'disabled'} title="${ai.enabled && ai.ready ? 'Claude searches the web for the chips on this product; each suggestion is then checked against your kernel tree' : 'Turn on the AI helper first (home page → Rules vs AI)'}">🌐 Identify the chips on this product</button>
    <span class="muted small">web search by Claude, ~30–90 s · checked against your kernel tree</span></div>`;
  if (!w) return h;
  if (w.loading) return h + '<div class="muted small">searching the web for the product, its datasheet and drivers…</div>';
  if (w.error) return h + `<div class="callout bad small">${esc(w.error)}</div>`;
  if (!w.chips?.length) return h + `<div class="callout warn small">Couldn't tell which chips are on it.${w.raw ? `<pre class="raw">${esc(w.raw.slice(0, 1500))}</pre>` : ''}</div>`;
  return h + `<div class="pf-web"><div class="small"><b>${esc(w.product || W.q)}</b> <span class="muted">· from the web: verify against the sources</span></div>
    ${w.chips.map(c => `<div class="pf-chip"><div><b>${esc(c.part)}</b> <span class="muted">by ${esc(c.maker || '?')}</span> · ${esc(c.role || '')}
        ${c.bus ? `<span class="pill">${esc(c.bus)}</span>` : ''}</div>
      ${(c.sources || []).length ? `<div class="small">sources: ${c.sources.slice(0, 3).map(u => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.replace(/^https?:\/\//, '').slice(0, 50))}</a>`).join(' · ')}</div>` : ''}
      ${c.linux_driver ? `<div class="small muted">Linux driver it found: ${esc(c.linux_driver)}</div>` : ''}
      ${c.kernel && !c.kernel.error ? c.kernel.parts.map(p => partHtml(p, (c.compatible_guess || '').split(',')[0] || c.kernel.vendor_hint)).join('') : ''}
      ${c.compatible_guess ? `<div class="small">Claude's guess for the compatible: <button class="chip-btn" data-c="${esc(c.compatible_guess)}">${esc(c.compatible_guess)}</button></div>` : ''}
    </div>`).join('')}
    ${w.notes ? `<div class="callout small">${esc(w.notes)}</div>` : ''}
    ${w.cost_usd ? `<div class="muted small">cost $${w.cost_usd.toFixed(3)}</div>` : ''}</div>`;
}
function pick(c) {
  W.compatible = c; $('#wz-compat').value = c;
  $$('#dlg-wizard .res, #dlg-wizard [data-c]').forEach(b => b.classList.toggle('sel', b.dataset.c === c));
  toast(`picked ${c}: press Next →`);
}
function wireSearch() {
  let tm;
  $('#wz-q').oninput = e => { W.q = e.target.value.trim(); W.web = null; $('#wz-web').innerHTML = webHtml(); clearTimeout(tm); tm = setTimeout(() => guard(async () => {
    if (W.q.length < 2) { W.find = null; W.results = []; $('#wz-results').innerHTML = ''; return; }
    $('#wz-results').innerHTML = '<div class="muted small">searching your kernel tree…</div>';
    const q = W.q, r = await api('/api/parts/find?q=' + encodeURIComponent(q));
    if (q !== W.q) return;
    W.find = r; W.results = (r.parts || []).flatMap(p => p.hits);
    $('#wz-results').innerHTML = resultsHtml();
  }), 400); };
  $('#wiz-body').onclick = e => {
    const b = e.target.closest('[data-c]'); if (b) return pick(b.dataset.c);
    if (e.target.id === 'wz-webgo') guard(async () => {
      W.web = { loading: true }; $('#wz-web').innerHTML = webHtml();
      try { W.web = await api('/api/parts/web', { method: 'POST', body: { q: W.q } }); }
      catch (err) { W.web = { error: err.message }; }
      W.results = W.results.concat((W.web.chips || []).flatMap(c => (c.kernel?.parts || []).flatMap(p => p.hits)));
      $('#wz-web').innerHTML = webHtml();
    });
  };
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

// keep checking that the board really answers (a setting saying "ssh" doesn't mean it's up)
setInterval(async () => {
  if (document.hidden || !O || !O.status.board.live) return;
  const p = await api('/api/board/ping').catch(() => null);
  if (p && (p.reachable !== O.ping?.reachable)) { O.ping = p; renderSetup(); if (!p.reachable) toast('⚠ The board stopped answering', 5000); else toast('✓ The board answers again'); }
  else if (p) O.ping = p;
}, 15000);
guard(async () => { await load(); if (location.hash === '#add') { history.replaceState(null, '', '/'); openWizard(); } });
