/*
 * CORPIDE dashboard.
 *
 * Talks to the FastAPI backend on the same origin, so there is no CORS
 * hop and no second server to start: `uvicorn api.main:app` serves both
 * the API and this page.
 *
 * Every number on screen comes from the API. Colour is an argument, not
 * decoration: red is only ever used for something an edge id in
 * evidence_trail actually backs, amber for what the agent looked at but
 * did not prove, green for a company the SAT cleared.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const API = '';                       // same origin

const state = {
  investigationId: null,
  running: false,
  startedAt: null,
  clockTimer: null,
  steps: 0,
  dropped: 0,
  claims: 0,
  elapsed: 0,
  view: 'dashboard',
  graph: null,        // last /graph/export payload
  graphEdgeIds: null, // Set, to tell an archived case from the live estate
  caseFile: null,     // last one loaded, so a theme swap can repaint its trail
  // The dossier. caseSeq counts successful loads and openedSeq the ones
  // whose opening already played, so the folder opens once per case.
  // loadedCaseId, not investigationId: the SSE 'done' handler sets that
  // one before it calls loadCaseFile.
  caseSeq: 0,
  openedSeq: 0,
  loadedCaseId: null,
  caseTab: 'verdict',
  sheetScroll: {},    // display:none drops a sheet's scroll; kept per tab
};

/* ------------------------------------------------------------ helpers */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}

function hhmmss(seconds) {
  const m = Math.floor(seconds / 60), s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function clockNow() {
  return new Date().toLocaleTimeString('en-US', { hour12: false });
}

/* "1 step" / "3 steps": English plurals instead of a "(s)" suffix. */
function plural(count, one, many) {
  return count === 1 ? one : many;
}

// Built once: the amount count-up formats on every animation frame.
// narrowSymbol keeps amounts reading "$585,105" rather than "MX$585,105".
const MXN = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'MXN', currencyDisplay: 'narrowSymbol', maximumFractionDigits: 0,
});

function pesos(value) {
  return MXN.format(value || 0);
}

function pesosShort(value) {
  if (!value) return '$0';
  if (value >= 1e6) return `$${(value / 1e6).toFixed(2)}M`;
  if (value >= 1e3) return `$${(value / 1e3).toFixed(0)}K`;
  return `$${value.toFixed(0)}`;
}

function setMeter(valueId, fillId, text, fraction) {
  $(valueId).textContent = text;
  const fill = $(fillId);
  if (fill) fill.style.width = `${Math.max(0, Math.min(1, fraction || 0)) * 100}%`;
}

async function api(path, options) {
  const resp = await fetch(API + path, options);
  let body = null;
  try { body = await resp.json(); } catch (_) { /* empty body is fine */ }
  if (!resp.ok) {
    const detail = (body && (body.detail || body.message)) || `HTTP ${resp.status}`;
    const error = new Error(detail);
    error.status = resp.status;
    throw error;
  }
  return body;
}

function json(payload) {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  };
}

/* ------------------------------------------------------------- motion */
/*
 * Three switches, persisted here and applied as classes on <html>, so
 * turning a group off never reaches application logic. The OS setting
 * still wins: tokens.css answers prefers-reduced-motion on its own.
 */

const MOTION = {
  ambience: { key: 'fa.ambience', cls: 'no-ambience', sw: 'sw-ambience' },
  sweep:    { key: 'fa.sweep',    cls: 'no-sweep',    sw: 'sw-sweep' },
  micro:    { key: 'fa.micro',    cls: 'no-micro',    sw: 'sw-micro' },
  sound:    { key: 'fa.sound',    cls: 'no-sound',    sw: 'sw-sound' },
};

function motionOn(name) {
  return localStorage.getItem(MOTION[name].key) !== 'off';
}

function reducedMotion() {
  return Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}
/* The big beats (the dossier opening) ride the sweep switch; the small
   ones (stamp, count-up, dealt contents) ride micro. The OS setting
   turns both off. */
function cinematic() { return motionOn('sweep') && !reducedMotion(); }
function micro() { return motionOn('micro') && !reducedMotion(); }

function applyMotion() {
  for (const [name, conf] of Object.entries(MOTION)) {
    const on = motionOn(name);
    document.documentElement.classList.toggle(conf.cls, !on);
    const sw = $(conf.sw);
    if (sw) { sw.classList.toggle('is-on', on); sw.setAttribute('aria-checked', String(on)); }
  }
}

function toggleMotion(name) {
  localStorage.setItem(MOTION[name].key, motionOn(name) ? 'off' : 'on');
  applyMotion();
  if (name === 'ambience' && motionOn('ambience')) spawnMotes();
}

function spawnMotes() {
  const box = $('ambience');
  for (const old of box.querySelectorAll('.ambience__mote')) old.remove();
  // Negative delays so the field is already in motion on first paint --
  // nobody should see the dust start.
  for (let i = 0; i < 8; i += 1) {
    const mote = el('span', 'ambience__mote');
    const duration = 19 + Math.random() * 17;
    mote.style.left = `${Math.random() * 100}%`;
    mote.style.setProperty('--mote-size', `${4 + Math.random() * 8}px`);
    mote.style.setProperty('--mote-dx', `${(Math.random() - 0.5) * 90}px`);
    mote.style.setProperty('--mote-dur', `${duration}s`);
    mote.style.setProperty('--mote-peak', (0.06 + Math.random() * 0.08).toFixed(3));
    mote.style.animationDelay = `-${Math.random() * duration}s`;
    box.append(mote);
  }
}


/* ---------------------------------------------------------------------
 * Xylophone, synthesised -- no audio files to ship or fail to load.
 *
 * A struck bar is a sine with a fast attack and an exponential decay,
 * plus a quieter partial an octave and a fifth up; that ratio is what
 * makes it read as wood rather than as a beep. Notes walk up a
 * pentatonic scale, so consecutive nodes sound like a phrase instead of
 * the same chime over and over.
 *
 * The context is created on the first click (Investigate), which is the
 * user gesture browsers require before any sound.
 * ------------------------------------------------------------------- */

let audioCtx = null;
let noteIndex = 0;

// C major pentatonic over two octaves: no interval in it can sound wrong.
const SCALE = [523.25, 587.33, 659.25, 783.99, 880.0,
               1046.5, 1174.66, 1318.51, 1567.98, 1760.0];

function audio() {
  if (!motionOn('sound')) return null;
  if (!audioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    try { audioCtx = new Ctx(); } catch (_) { return null; }
  }
  if (audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
  return audioCtx;
}

function strike(freq, { at = 0, gain = 0.22, decay = 1.1 } = {}) {
  const ctx = audio();
  if (!ctx) return;
  const t = ctx.currentTime + at;

  // Fundamental plus one bright partial, each with its own decay.
  for (const [ratio, level, tail] of [[1, gain, decay], [3.0, gain * 0.3, decay * 0.45]]) {
    const osc = ctx.createOscillator();
    const env = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = freq * ratio;
    env.gain.setValueAtTime(0, t);
    env.gain.linearRampToValueAtTime(level, t + 0.006);   // hard mallet attack
    env.gain.exponentialRampToValueAtTime(0.0001, t + tail);
    osc.connect(env).connect(ctx.destination);
    osc.start(t);
    osc.stop(t + tail + 0.05);
  }
}

/* One note per node, walking up the scale and folding back down so a
 * long investigation doesn't climb into a whistle. */
function soundNode() {
  const position = noteIndex % (SCALE.length * 2 - 2);
  const step = position < SCALE.length ? position : SCALE.length * 2 - 2 - position;
  strike(SCALE[step], { gain: 0.2, decay: 0.9 });
  noteIndex += 1;
}

/* The case file closing: a rising arpeggio, a little louder, with the
 * root doubled underneath so it lands. */
function soundDone() {
  noteIndex = 0;
  [0, 2, 4, 7].forEach((step, i) => {
    strike(SCALE[step], { at: i * 0.11, gain: 0.24, decay: 1.5 });
  });
  strike(SCALE[0] / 2, { at: 0, gain: 0.12, decay: 2.0 });
}

/* A flatter, quieter pair for a run that ended without a verdict. */
function soundStopped() {
  noteIndex = 0;
  strike(SCALE[2], { gain: 0.16, decay: 0.7 });
  strike(SCALE[0], { at: 0.13, gain: 0.16, decay: 1.0 });
}


/* ---------------------------------------------------------------------
 * Theme
 *
 * The DOM follows [data-theme] through CSS, but the graph does not: vis
 * paints on a canvas, so no stylesheet reaches it. Every graph colour
 * is therefore read from the computed style (tokens.css) rather than
 * hardcoded, and a swap repaints the canvas with the new values.
 * ------------------------------------------------------------------- */

const THEME_KEY = 'fa.theme';

function cssVar(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function currentTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved === 'light' || saved === 'dark') return saved;
  // No choice stored: follow the operating system.
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
    ? 'light' : 'dark';
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const button = $('theme-toggle');
  if (button) {
    button.setAttribute('aria-label',
      theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode');
  }

  // Repaint the canvas with the new palette, keeping whatever the run
  // had already highlighted -- switching theme must not erase the trail.
  if (state.graph && nodeSet) {
    drawGraph(state.graph);
    if (state.caseFile) highlightTrail(state.caseFile);
  }
}

function toggleTheme() {
  const next = currentTheme() === 'light' ? 'dark' : 'light';
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
}

/* -------------------------------------------------------------- views */

function switchView(name) {
  const changed = name !== state.view;
  if (changed && state.view === 'casefile') leaveCaseFile();
  state.view = name;
  // Opening a fresh case plays the dossier's own entrance instead.
  const willOpen = name === 'casefile' && openingDue() && cinematic();

  for (const view of document.querySelectorAll('.view')) {
    const active = view.id === `view-${name}`;
    view.hidden = !active;
    // The sweep is the most recognisable thing in the interface, which
    // is exactly why it fires on a real view change and never on a
    // re-render of the view you are already looking at.
    if (active && changed && !willOpen) {
      view.classList.remove('is-entering');
      void view.offsetWidth;                 // restart the animation
      view.classList.add('is-entering');
      view.addEventListener('animationend', function done(e) {
        if (e.target !== view) return;
        view.classList.remove('is-entering');
        view.removeEventListener('animationend', done);
      });
    }
  }
  for (const item of document.querySelectorAll('.nav__item')) {
    item.classList.toggle('is-active', item.dataset.view === name);
  }
  if (name === 'casefile') {
    fitTabs();                                    // widths were 0 while hidden
    if (changed) sheet(state.caseTab).scrollTop = state.sheetScroll[state.caseTab] || 0;
    maybeOpenDossier();                           // same task as the unhide
  }
  if (name === 'archive') loadCaseIndex();
}

/* ------------------------------------------------------------ banners */

function clearBanner(kind) {
  const existing = $('banners').querySelector(`[data-kind="${kind}"]`);
  if (existing) existing.remove();
}

function showBanner(kind, { variant, icon, title, body, actions = [] }) {
  clearBanner(kind);
  const banner = el('div', `banner banner--${variant} u-enter-sys`);
  banner.dataset.kind = kind;
  banner.innerHTML = icon;
  const text = el('div', 'banner__text');
  text.append(el('span', 'banner__title', title));
  const bodyEl = el('span', 'banner__body');
  bodyEl.innerHTML = body;
  text.append(bodyEl);
  banner.append(text);

  if (actions.length) {
    const box = el('div', 'banner__actions');
    for (const action of actions) {
      const button = el('button', `btn u-tactile ${action.className || 'btn--ghost'}`, action.label);
      button.addEventListener('click', action.onClick);
      box.append(button);
    }
    banner.append(box);
  }
  $('banners').append(banner);
}

const ICON_ERROR = '<svg class="banner__icon" width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#C4462F" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5 21 19H3z"></path><path d="M12 9.5v4.5"></path><path d="M12 16.6h.01"></path></svg>';
const ICON_WARN = '<svg class="banner__icon" width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#E0A03A" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H12z"></path></svg>';
const ICON_INFO = '<svg class="banner__icon" width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#5A5348" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"></circle><path d="M12 8h.01"></path><path d="M11.5 12h1v5"></path></svg>';

/* --------------------------------------------------------------- seal */

const SEAL_STATES = {
  idle:     { cls: '', word: 'UNOPENED', sub: 'CASE FILE' },
  running:  { cls: 'seal--running', word: 'AUDIT', sub: 'UNDERWAY' },
  accused:  { cls: 'seal--accused', word: 'FRAUD', sub: 'PROVEN' },
  clean:    { cls: 'seal--clean', word: 'NO CHARGES', sub: 'CLOSED' },
};

function setSeal(name) {
  const seal = $('seal');
  const s = SEAL_STATES[name] || SEAL_STATES.idle;
  seal.className = `seal ${s.cls}`.trim();
  $('seal-word').textContent = s.word;
  $('seal-sub').textContent = s.sub;
}

/* ------------------------------------------------------ ollama status */
/*
 * Three states, because "down" and "degraded" are different problems:
 *   green  server up and the configured model is there
 *   amber  reachable but the model is missing, or we are on the cloud
 *          fallback -- a run will work, just not the way we planned
 *   red    unreachable, or the API itself is not answering
 */

function setStatus(variant, title, detail) {
  const status = $('ollama-status');
  status.className = `status status--${variant}`;
  $('ollama-led').classList.toggle('is-beating', variant !== 'down');
  $('ollama-title').textContent = title;
  $('ollama-detail').textContent = detail;
}

async function refreshOllama() {
  // With the agent on Snowflake Cortex the LAN laptop is not in the
  // path: probing it costs ~6s when it sleeps, and its red banner would
  // cry wolf over a server nobody is using.
  const info = await refreshIntegrations();
  if (info && info.agent.provider === 'cortex') {
    const ready = info.snowflake.configured;
    setStatus(ready ? 'ok' : 'down', ready ? 'Cortex ready' : 'Cortex: no credentials',
              `${info.agent.model} · Snowflake`);
    clearBanner('ollama');
    return ready;
  }

  let health;
  try {
    health = await api('/health/ollama');
  } catch (err) {
    setStatus('down', 'API unavailable', 'localhost:8000');
    showBanner('ollama', {
      variant: 'error', icon: ICON_ERROR, title: 'No backend',
      body: `Could not reach the API: ${escapeHtml(err.message)}. Is <code>uvicorn api.main:app</code> running?`,
      actions: [{ label: 'Retry', className: 'btn--dark', onClick: refreshOllama }],
    });
    return false;
  }

  const host = (health.url || '').replace(/^https?:\/\//, '');

  if (health.ollama_reachable && health.model_available) {
    setStatus('ok', 'Model ready', `${health.model} · ${host}`);
    clearBanner('ollama');
    return true;
  }

  if (health.ollama_reachable) {
    setStatus('warn', 'Model not found', `${health.model} · ${host}`);
  } else if (health.cloud_fallback) {
    setStatus('warn', 'Using cloud fallback', host);
  } else {
    setStatus('down', 'Ollama not responding', host || '—');
  }

  showBanner('ollama', {
    variant: health.cloud_fallback ? 'warn' : 'error',
    icon: health.cloud_fallback ? ICON_WARN : ICON_ERROR,
    title: health.ollama_reachable
      ? 'The model is not on the server'
      : (health.cloud_fallback ? 'Ollama is down, fallback available' : 'Ollama unavailable'),
    // health.message is the client's own human-facing diagnosis.
    body: escapeHtml(health.message),
    actions: [
      { label: 'Retry', className: 'btn--dark', onClick: refreshOllama },
      { label: 'Settings', onClick: () => switchView('settings') },
    ],
  });
  return Boolean(health.cloud_fallback);
}

/* ------------------------------------------------ gemini + snowflake */
/*
 * The other two services a run leans on. Gemini writes the final
 * narrative and answers the judge; Snowflake, when DATA_SOURCE asks for
 * it, filters the estate before the graph is drawn. Both fall back to
 * local on their own -- which is exactly why the screen has to say when
 * they did, instead of "using Snowflake" on stage from the laptop.
 */

let integrations = null;

function setMiniStatus(prefix, variant, title, detail) {
  const box = $(`${prefix}-status`);
  box.hidden = false;
  box.className = `status status--mini status--${variant}`;
  box.title = detail;                   // the detail is cut at sidebar width
  $(`${prefix}-title`).textContent = title;
  $(`${prefix}-detail`).textContent = detail;
}

/* Refreshes the lights and returns what the API said. `announce` also
 * (re)raises the Snowflake-fallback banner: only when a graph was just
 * built, never on the 15s poll, where it would re-animate forever. */
async function refreshIntegrations({ announce = false } = {}) {
  try {
    integrations = await api('/health/integrations');
  } catch (_) {
    return null;                        // refreshOllama already reports a dead API
  }
  const { agent, gemini, snowflake, last_build: build } = integrations;

  if (gemini.configured) setMiniStatus('gemini', 'ok', 'Gemini ready', gemini.model);
  else setMiniStatus('gemini', 'warn', 'Gemini: no key',
    `narrative and questions via ${agent.provider === 'cortex' ? 'Cortex' : 'the local model'}`);

  if (announce) clearBanner('snowflake');
  if (!snowflake.requested) {
    // Local data is the default and needs no light: a grey one only
    // added noise next to the ones that can actually go red.
    $('data-status').hidden = true;
  } else if (!snowflake.configured) {
    setMiniStatus('data', 'down', 'Snowflake: no credentials', 'the graph is built locally');
  } else if (!build) {
    setMiniStatus('data', 'warn', 'Snowflake', 'used when a scenario is generated');
  } else if (build.active === 'snowflake') {
    setMiniStatus('data', 'ok', 'Data in Snowflake',
      `${build.nodes_kept} of ${build.nodes_total} nodes · ${build.seconds} s`);
  } else {
    setMiniStatus('data', 'down', 'Snowflake failed', 'the graph on screen is local');
    if (announce) {
      showBanner('snowflake', {
        variant: 'warn', icon: ICON_WARN, title: 'Snowflake did not respond',
        body: 'The graph on screen was built locally, without the warehouse filter. '
            + `<code>${escapeHtml(build.error)}</code>`,
      });
    }
  }
  return integrations;
}

/* --------------------------------------------------------------- graph */
/*
 * One /graph/export per estate change and no more. Highlighting the
 * agent's path mutates the existing DataSets in place: re-fetching would
 * restart the physics layout and throw away the picture the judge has
 * been watching settle.
 */

let network = null;
let nodeSet = null;
let edgeSet = null;
let walkTimers = [];

function graphPalette() {
  return {
    nodeLine: cssVar('--graph-node-line', '#4A4238'),
    nodeText: cssVar('--graph-node-text', '#8A8073'),
    nodeFill: cssVar('--graph-node', '#241F1A'),
    nodeFillDim: cssVar('--graph-bg', '#1C1916'),
    edge: cssVar('--graph-edge', '#2E2A24'),
    amber: cssVar('--amber', '#E0A03A'),
    red: cssVar('--red', '#C4462F'),
    green: cssVar('--green', '#6E8F62'),
  };
}

function cancelWalk() {
  for (const timer of walkTimers) clearTimeout(timer);
  walkTimers = [];
}

function nodeStyle(node, tone) {
  // tone: 'dim' | 'suspect' | 'fraud'. Colour by blacklist_status too,
  // since a cleared company must never be able to look accused.
  const status = (node.attributes && node.attributes.blacklist_status) || 'none';
  const cleared = status === 'desvirtuado' || status === 'sentencia_favorable';
  const flagged = status === 'presunto' || status === 'definitivo';

  const palette = graphPalette();
  let border = palette.nodeLine;
  let text = palette.nodeText;
  if (cleared) { border = palette.green; text = palette.green; }
  else if (flagged) { border = palette.amber; }

  if (tone === 'suspect') { border = cleared ? palette.green : palette.amber; text = palette.amber; }
  if (tone === 'fraud' && !cleared) { border = palette.red; text = palette.red; }

  const size = tone === 'fraud' ? 17 : tone === 'suspect' ? 13 : (node.type === 'Invoice' ? 6 : 10);
  return {
    color: {
      background: tone === 'dim' ? palette.nodeFillDim : palette.nodeFill,
      border,
      highlight: { background: palette.nodeFill,
                   border: tone === 'fraud' ? palette.red : palette.amber },
    },
    borderWidth: tone === 'fraud' ? 4 : (tone === 'suspect' || flagged || cleared ? 2.5 : 1),
    size,
    font: { size: tone === 'dim' ? 10 : 14, color: text, face: 'IBM Plex Mono' },
    opacity: tone === 'dim' ? 0.45 : 1,
  };
}

function edgeStyle(tone) {
  const palette = graphPalette();
  const color = tone === 'fraud' ? palette.red
    : tone === 'suspect' ? palette.amber : palette.edge;
  return {
    color: { color, highlight: color, opacity: tone === 'dim' ? 0.5 : 1 },
    width: tone === 'fraud' ? 4 : tone === 'suspect' ? 2.2 : 1,
    arrows: { to: { enabled: true, scaleFactor: tone === 'dim' ? 0.35 : 0.65 } },
  };
}

async function loadGraph() {
  // Every new graph may have come from somewhere else (or fallen back).
  refreshIntegrations({ announce: true });
  let data;
  try {
    data = await api('/graph/export');
  } catch (err) {
    // 400 = no estate yet. Not an error: it is the cold-start state, and
    // the empty panel inside the graph card already says what to do.
    $('graph-shell').classList.add('is-empty');
    $('graph-sub').textContent = 'No estate generated';
    return;
  }
  drawGraph(data);
}

function drawGraph(data) {
  cancelWalk();
  stopFollowing();
  stopTrailFlow();
  stopCamera();
  state.graph = data;
  state.graphEdgeIds = new Set(data.edges.map((e) => e.id));
  $('graph-shell').classList.toggle('is-empty', data.nodes.length === 0);
  $('graph-sub').textContent =
    `${data.nodes.length.toLocaleString('en-US')} ${plural(data.nodes.length, 'node', 'nodes')} · `
    + `${data.edges.length.toLocaleString('en-US')} ${plural(data.edges.length, 'edge', 'edges')}`;

  // The legend only claims what the data can show. The generator never
  // seeds an exonerated company today, so green would otherwise be a
  // promise the screen cannot keep.
  const anyCleared = data.nodes.some((n) => {
    const s = (n.attributes && n.attributes.blacklist_status) || 'none';
    return s === 'desvirtuado' || s === 'sentencia_favorable';
  });
  $('legend-cleared').hidden = !anyCleared;

  nodeSet = new vis.DataSet(data.nodes.map((node) => ({
    id: node.id,
    label: node.label && node.label.length > 26 ? `${node.label.slice(0, 24)}…` : node.label,
    title: `${node.type} · ${node.id}`,
    shape: 'dot',
    ...nodeStyle(node, 'dim'),
  })));

  edgeSet = new vis.DataSet(data.edges.map((edge) => ({
    id: edge.id,
    from: edge.source,
    to: edge.target,
    title: `${edge.type} · ${edge.id}`,
    ...edgeStyle('dim'),
  })));

  const options = {
    physics: {
      stabilization: { iterations: 180 },
      barnesHut: { gravitationalConstant: -12000, springLength: 130, springConstant: 0.03 },
    },
    layout: {
      // Kamada-Kawai pre-layout is O(n^3)-ish and this graph is already
      // laid out well by barnesHut; skipping it is most of the startup
      // time back on a 130-node estate.
      improvedLayout: false,
    },
    interaction: {
      hover: true,
      tooltipDelay: 120,
      // Dragging or zooming a graph with 267 edges stays fluid if the
      // edges sit out the gesture.
      hideEdgesOnDrag: true,
      hideEdgesOnZoom: true,
    },
    nodes: { shadow: false },
    edges: { smooth: { type: 'continuous' } },
  };

  if (network) network.destroy();
  network = new vis.Network($('graph'), { nodes: nodeSet, edges: edgeSet }, options);
  // The orbiting dots are painted in the graph's own canvas, so they
  // track the node through pan, zoom and physics for free.
  // Order matters: the flow has to land ON TOP of vis's own edges (in
  // beforeDrawing it was painted under them and invisible), and the
  // spinner on top of the flow.
  network.on('afterDrawing', drawTrailFlow);
  network.on('afterDrawing', drawSpinner);

  // Once the layout settles, freeze it. A live solver repaints forever
  // at 60fps for nothing -- the nodes have stopped moving -- and it
  // fights the camera while it tries to follow one. Everything after
  // this point (focus, fit, our own animations) still works, and the
  // graph stops being the reason the page feels warm.
  let settleTimer = null;
  const freeze = () => { if (network) network.setOptions({ physics: false }); };
  network.once('stabilized', freeze);

  // Physics off is what keeps the page cool, but it also killed the
  // feel of the thing: with the solver stopped, dragging a node no
  // longer pulls its neighbours and the graph stops being an elastic
  // web. So it wakes up for the drag and goes back to sleep after.
  network.on('dragStart', (params) => {
    stopCamera();                    // a hand on the graph outranks the camera
    if (!params.nodes || !params.nodes.length) return;   // panning, not dragging
    if (settleTimer) { clearTimeout(settleTimer); settleTimer = null; }
    network.setOptions({ physics: true });
  });

  network.on('dragEnd', (params) => {
    if (!params.nodes || !params.nodes.length) return;
    // Let it spring back into shape, then freeze again.
    if (settleTimer) clearTimeout(settleTimer);
    settleTimer = setTimeout(freeze, 1400);
  });
  network.on('zoom', stopCamera);    // wheel or pinch; moveTo never fires it
}

function nodeById(id) {
  return state.graph && state.graph.nodes.find((n) => n.id === id);
}

/* Live: paint what the current step touched, amber, as it arrives. */
function markLive(ids) {
  if (!nodeSet || !ids || !ids.length) return;
  for (const id of ids) {
    const node = nodeById(id);
    if (node) {
      // Don't overwrite the live node's halo with the plain style.
      if (id !== activeId) nodeSet.update({ id, ...nodeStyle(node, 'suspect') });
      continue;
    }
    if (state.graphEdgeIds && state.graphEdgeIds.has(id)) edgeSet.update({ id, ...edgeStyle('suspect') });
  }
  followSequence(ids);
}

/* ---------------------------------------------------------------------
 * The node the agent is looking at RIGHT NOW.
 *
 * markLive() paints everything a step touched amber and leaves it there.
 * That says "visited" but not "this one, now", so a twelve-step run
 * ends up as a field of amber with no story. This adds the pointer: one
 * node at a time carries a breathing halo while the agent is on it, and
 * the camera walks the sequence with it.
 *
 * Both halves obey the motion switches that already exist: the halo is a
 * micro-animation, the camera move is a sweep. Turn either off in
 * Settings and this degrades to exactly the previous behaviour, which is
 * also what someone on prefers-reduced-motion gets.
 * ------------------------------------------------------------------- */

const FOCUS_STEP_MS = 1150;   // dwell time per node when a step names several
const FLASH_MS = 460;         // the arrival flash: once per node, then still
// Read per use, so a theme swap mid-run recolours the halo too.
function halo() { return cssVar('--graph-halo', 'rgba(224, 160, 58, .9)'); }
const SPINNER_DOTS = 8;
const SPINNER_RADIUS = 26;

let activeId = null;
let flashTimer = null;
const flashed = new Set();      // nodes that already had their one flash
let focusQueue = [];
let focusTimer = null;
let spinnerFrame = null;
let spinnerOn = false;

function clearHalo(id) {
  const node = id && nodeById(id);
  if (!node || !nodeSet) return;
  // Back to plain "visited": amber, no halo.
  nodeSet.update({ id, ...nodeStyle(node, 'suspect'), shadow: { enabled: false } });
}

function stopFlash() {
  if (flashTimer) { clearInterval(flashTimer); flashTimer = null; }
}

function stopFollowing() {
  flashed.clear();
  if (focusTimer) { clearTimeout(focusTimer); focusTimer = null; }
  focusQueue = [];
  stopFlash();
  stopSpinner();
  clearHalo(activeId);
  activeId = null;
}

/* ---------------------------------------------------------------------
 * "Working" lives in the spinner, not in the node.
 *
 * A halo that breathes forever reads as decoration after ten seconds,
 * so arriving at a node is now a single flash that settles, and the
 * fact that the agent is still busy is carried by dots orbiting the
 * node it is on -- a loading indicator pinned to the thing being
 * loaded. Drawn in vis's own afterDrawing pass, so it stays glued to
 * the node through pan, zoom and physics with no coordinate maths.
 * ------------------------------------------------------------------- */

/* ---------------------------------------------------------------------
 * One render loop for every canvas animation.
 *
 * vis only repaints when something changes, so anything that moves has
 * to ask for frames. Two independent timers asking separately meant two
 * full repaints per frame of a 130-node graph. This is the single
 * driver: it runs only while something actually needs animating, caps
 * at 30fps, and stops dead when the tab is hidden -- a background tab
 * repainting a graph is pure heat.
 * ------------------------------------------------------------------- */

let frameTimer = null;
let positions = null;       // refreshed once per frame, shared by all painters
let trailFlow = { amber: [], cited: [], startedAt: 0, done: false };

// How long the closing animation plays before it settles. One full pass
// of the road markings and three breaths of the ember is a statement;
// looping it forever is wallpaper, and it never stops asking for frames.
const TRAIL_ANIM_MS = 5200;

function trailAnimating() {
  return !trailFlow.done && (trailFlow.amber.length > 0 || trailFlow.cited.length > 0);
}

function needsFrames() {
  return spinnerOn || trailAnimating() || camera.to !== null;
}

function startFrames() {
  if (frameTimer || !network || !needsFrames()) return;
  const tick = () => {
    frameTimer = null;
    if (!network || !needsFrames()) return;
    // A camera step queues its own paint through moveTo; painting here
    // as well would draw the graph twice per frame.
    if (!stepCamera() && !document.hidden) network.redraw();
    // 30fps ceiling, except while the camera travels: a pan at 30fps judders.
    frameTimer = setTimeout(tick, camera.to ? 16 : 33);
  };
  tick();
}

/* ---------------------------------------------------------------------
 * The camera, moved by hand.
 *
 * vis's own animated focus()/fit() were what kept jamming the graph.
 * Their tween advances one step per repaint, not per millisecond, and
 * vis only retires a running tween once a repaint has moved it. Two
 * moves in the same frame -- an action and its observation arrive
 * together, since the tool answers in milliseconds -- leave the first
 * tween hooked into every future repaint and vis's render counter below
 * zero. From then on the camera replays that stale move on each hover,
 * the closing pull-back stops halfway, and nothing vis animates gets a
 * frame again. So vis only ever gets instant moveTo() calls, and the
 * tween lives here, on the clock, in the one frame loop.
 * ------------------------------------------------------------------- */

const camera = { from: null, to: null, startedAt: 0, duration: 0 };

function stopCamera() {
  camera.to = null;
}

/* With another view open the canvas has no size, and vis centres a
 * moveTo() on a zero-width frame: the camera comes back off by half. */
function graphHidden() {
  return !$('graph').clientWidth;
}

/* Glide to {position, scale}. A new move starts from wherever the
 * camera is right now, so interrupting one is always safe. */
function moveCamera(to, duration) {
  if (!network || !to || graphHidden()) return;
  camera.from = { position: network.getViewPosition(), scale: network.getScale() };
  camera.to = to;
  camera.startedAt = performance.now();
  camera.duration = duration;
  startFrames();
}

/* One step of the glide. True if the camera moved. */
function stepCamera() {
  if (!camera.to || !network) return false;
  if (graphHidden()) { camera.to = null; return false; }
  const t = Math.min(1, (performance.now() - camera.startedAt) / camera.duration);
  const ease = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;   // easeInOutCubic
  const { from, to } = camera;
  network.moveTo({
    position: {
      x: from.position.x + (to.position.x - from.position.x) * ease,
      y: from.position.y + (to.position.y - from.position.y) * ease,
    },
    // Zoom in ratios, not in steps: halving the scale should take as
    // long as doubling it.
    scale: from.scale * Math.pow(to.scale / from.scale, ease),
  });
  if (t >= 1) camera.to = null;
  return true;
}

/* Where the camera centres a node. */
function nodeTarget(id, scale) {
  const position = network.getPositions([id])[id];
  return position ? { position, scale } : null;
}

/* Where vis's fit() would put the camera, without moving it: vis has no
 * compute-only call, so fit instantly, read the view and put it back
 * before anything can paint. An empty list fits the whole graph. */
function fitTarget(nodeIds) {
  if (graphHidden()) return null;       // vis would compute a zero zoom
  const from = { position: network.getViewPosition(), scale: network.getScale() };
  network.fit({ nodes: nodeIds, animation: false });
  const to = { position: network.getViewPosition(), scale: network.getScale() };
  network.moveTo(from);
  return to;
}

function stopFrames() {
  if (frameTimer) { clearTimeout(frameTimer); frameTimer = null; }
}

function startSpinner() {
  if (spinnerOn || !motionOn('micro')) return;
  spinnerOn = true;
  startFrames();
}

function stopSpinner() {
  spinnerOn = false;
  if (!needsFrames()) { stopFrames(); if (network) network.redraw(); }
}

function stopTrailFlow() {
  trailFlow = { amber: [], cited: [], startedAt: 0, done: false };
  if (!needsFrames()) stopFrames();
}

/* Repaint stops while the tab is in the background and resumes on
 * return, so a demo left on a second screen isn't burning the CPU. */
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && needsFrames()) startFrames();
});

/* ---------------------------------------------------------------------
 * The finished trail: amber lanes that flow, cited edges that burn.
 *
 * Painted over vis's own straight (smooth:false) trail edges, so the
 * dashes read as road markings on the lane rather than replacing it.
 * Both ends are trimmed by the node radius so a line drawn above the
 * edges never crosses the dots it connects.
 * ------------------------------------------------------------------- */

const LANE_TRIM = 15;   // px pulled back from each node centre

/* Shorten a segment at both ends, so it starts and stops at the rim. */
function lane(a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const length = Math.hypot(dx, dy);
  if (length < LANE_TRIM * 2.2) return null;      // too short to bother
  const ux = dx / length;
  const uy = dy / length;
  return {
    x1: a.x + ux * LANE_TRIM, y1: a.y + uy * LANE_TRIM,
    x2: b.x - ux * LANE_TRIM, y2: b.y - uy * LANE_TRIM,
  };
}

function drawTrailFlow(ctx) {
  if (!network || (!trailFlow.amber.length && !trailFlow.cited.length)) return;
  try { positions = network.getPositions(); } catch (_) { return; }

  const now = Date.now();
  const elapsed = trailFlow.startedAt ? now - trailFlow.startedAt : 0;

  // Past the window the paint holds a still frame: markings parked and
  // the ember at its brightest. Redraws triggered by hover or drag keep
  // rendering it correctly, but nothing asks for frames any more.
  if (!trailFlow.done && trailFlow.startedAt && elapsed > TRAIL_ANIM_MS) {
    trailFlow.done = true;
    stopFrames();
  }
  const settled = trailFlow.done;
  const phase = settled ? TRAIL_ANIM_MS : elapsed;

  // Amber: road markings travelling from payer to payee.
  if (trailFlow.amber.length) {
    ctx.save();
    // Bright and wider than the amber edge underneath, with a long gap:
    // at 2.4px in a near-identical amber these were invisible against
    // the very line they were supposed to be marking.
    ctx.strokeStyle = cssVar('--graph-lane', 'rgba(255, 238, 205, .96)');
    ctx.lineWidth = 3.6;
    ctx.lineCap = 'butt';
    ctx.setLineDash([7, 16]);
    ctx.lineDashOffset = -((phase / 26) % 22);   // negative: flows forward
    ctx.beginPath();
    for (const edge of trailFlow.amber) {
      const seg = positions[edge.from] && positions[edge.to]
        && lane(positions[edge.from], positions[edge.to]);
      if (!seg) continue;
      ctx.moveTo(seg.x1, seg.y1);
      ctx.lineTo(seg.x2, seg.y2);
    }
    ctx.stroke();
    ctx.restore();
  }

  // Cited: incandescent, breathing between a hot ember and a flare.
  if (trailFlow.cited.length) {
    // Settled: hold the bright end of the breath rather than a
    // random point in it.
    const beat = settled ? 1 : (Math.sin(phase / 300) + 1) / 2;
    ctx.save();
    const ember = cssVar('--graph-ember', '196, 70, 47');
    ctx.strokeStyle = `rgba(${ember}, ${(0.78 + beat * 0.22).toFixed(2)})`;
    ctx.lineWidth = 3.4 + beat * 2.4;
    ctx.lineCap = 'round';
    ctx.shadowColor = `rgba(${ember}, .95)`;
    ctx.shadowBlur = 9 + beat * 26;
    ctx.beginPath();
    for (const edge of trailFlow.cited) {
      const seg = positions[edge.from] && positions[edge.to]
        && lane(positions[edge.from], positions[edge.to]);
      if (!seg) continue;
      ctx.moveTo(seg.x1, seg.y1);
      ctx.lineTo(seg.x2, seg.y2);
    }
    ctx.stroke();
    ctx.restore();
  }
}

/* The loading ring: dots orbiting whatever node the agent is reading.
 *
 * Painted last, so it sits above the edges, the flow and the nodes. The
 * trailing fade is what gives the ring a direction of travel -- without
 * it, eight evenly lit dots read as a static decoration.
 */
function drawSpinner(ctx) {
  if (!spinnerOn || !activeId || !network) return;

  let position;
  try {
    position = (positions && positions[activeId])
      || network.getPositions([activeId])[activeId];
  } catch (_) { return; }
  if (!position) return;

  const turn = ((Date.now() % 1600) / 1600) * Math.PI * 2;
  for (let i = 0; i < SPINNER_DOTS; i += 1) {
    const angle = turn + (i / SPINNER_DOTS) * Math.PI * 2;
    const fade = 0.15 + 0.85 * (i / (SPINNER_DOTS - 1));
    ctx.beginPath();
    ctx.arc(position.x + Math.cos(angle) * SPINNER_RADIUS,
            position.y + Math.sin(angle) * SPINNER_RADIUS,
            1.1 + fade * 1.7, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(${cssVar('--graph-spinner', '224, 160, 58')}, ${(fade * 0.75).toFixed(3)})`;
    ctx.fill();
  }
}

/* One node becomes the live one: it flashes once, the camera travels to
 * it, the spinner follows, and a note marks the change. */
function setActiveNode(id) {
  if (!nodeSet || !network) return;
  const node = nodeById(id);
  if (!node || id === activeId) return;

  clearHalo(activeId);
  stopFlash();
  activeId = id;

  const base = nodeStyle(node, 'suspect');
  const REST = { size: base.size + 3, borderWidth: 3,
                 shadow: { enabled: true, color: halo(), size: 13, x: 0, y: 0 } };

  // Once per node for the whole run. The agent comes back to the same
  // company several times, and re-flashing on every visit is the
  // blinking-lights effect that looked cheap.
  const firstVisit = !flashed.has(id);
  flashed.add(id);

  if (motionOn('micro') && firstVisit) {
    // Arrive big and settle: one gesture, then the node holds still.
    const started = Date.now();
    flashTimer = setInterval(() => {
      if (activeId !== id || !nodeSet) { stopFlash(); return; }
      const progress = Math.min(1, (Date.now() - started) / FLASH_MS);
      const ease = 1 - Math.pow(1 - progress, 3);       // easeOutCubic
      nodeSet.update({
        id,
        size: base.size + 12 - ease * 9,
        borderWidth: 4.6 - ease * 1.6,
        shadow: { enabled: true, color: halo(), size: 34 - ease * 21, x: 0, y: 0 },
      });
      if (progress >= 1) { stopFlash(); nodeSet.update({ id, ...REST }); }
    }, 28);
  } else {
    nodeSet.update({ id, ...REST });
  }

  soundNode();
  startSpinner();

  // The camera follows the agent's attention. I tried skipping the move
  // when the node was already on screen, which sounded reasonable and
  // was wrong: the graph opens zoomed to fit, so every node counts as
  // visible and the camera never moved at all. What needed calming was
  // the flash repeating per visit, and that is handled above.
  if (motionOn('sweep')) moveCamera(nodeTarget(id, 1.25), 900);
}


function followSequence(ids) {
  if (!nodeSet || !network) return;
  const nodes = (ids || []).filter((id) => nodeById(id));
  if (!nodes.length) return;

  if (focusTimer) { clearTimeout(focusTimer); focusTimer = null; }
  focusQueue = nodes.slice(1);
  setActiveNode(nodes[0]);

  const advance = () => {
    const next = focusQueue.shift();
    if (!next) { focusTimer = null; return; }
    setActiveNode(next);
    focusTimer = setTimeout(advance, FOCUS_STEP_MS);
  };
  if (focusQueue.length) focusTimer = setTimeout(advance, FOCUS_STEP_MS);
}


/*
 * On completion: red only for the edges the accusations actually cite,
 * amber for the rest of the trail the agent walked, everything else
 * desaturated. Walked one edge at a time so the path reads as a route
 * rather than appearing all at once.
 */
function highlightTrail(caseFile) {
  if (!nodeSet || !state.graph) return { missing: 0 };
  cancelWalk();
  // The pointer has done its job; the verdict is about the whole route.
  stopFollowing();
  stopTrailFlow();

  const cited = new Set();
  for (const claim of caseFile.implicated_suppliers || []) {
    for (const id of claim.evidence_edge_ids || []) cited.add(id);
  }
  const trail = caseFile.evidence_trail || { nodes: [], edges: [] };

  // Reset: anything not in the trail fades into the background.
  nodeSet.update(state.graph.nodes.map((n) => ({ id: n.id, ...nodeStyle(n, 'dim') })));
  edgeSet.update(state.graph.edges.map((e) => ({ id: e.id, ...edgeStyle('dim') })));

  // Cited edges last, so red lands on top of the amber context.
  const ordered = [
    ...trail.edges.filter((e) => !cited.has(e.id)),
    ...trail.edges.filter((e) => cited.has(e.id)),
  ];

  let missing = 0;
  const step = motionOn('micro') ? 95 : 0;
  const flowing = motionOn('micro');
  ordered.forEach((edge, i) => {
    if (!state.graphEdgeIds.has(edge.id)) { missing += 1; return; }
    const tone = cited.has(edge.id) ? 'fraud' : 'suspect';
    walkTimers.push(setTimeout(() => {
      // Straight for the trail: our dashes and glow are drawn as
      // straight lines, and they have to sit exactly on vis's own edge.
      // It also sets the route apart from the curved context.
      edgeSet.update({ id: edge.id, ...edgeStyle(tone), smooth: false });
      for (const endpoint of [edge.source, edge.target]) {
        const node = nodeById(endpoint);
        if (node) nodeSet.update({ id: endpoint, ...nodeStyle(node, tone) });
      }
      if (flowing) {
        const lane = { from: edge.source, to: edge.target };
        trailFlow[tone === 'fraud' ? 'cited' : 'amber'].push(lane);
        // The clock starts with the last edge of the walk, so the whole
        // route animates together instead of each edge on its own timer.
        trailFlow.startedAt = Date.now();
        trailFlow.done = false;
        startFrames();
      }
    }, i * step));
  });

  $('graph-sub').textContent =
    `${state.graph.nodes.length.toLocaleString('en-US')} ${plural(state.graph.nodes.length, 'node', 'nodes')} · ` +
    `${trail.edges.length} in the trail · ${cited.size} cited as evidence`;

  // The camera spent the whole run zoomed in on one node at a time.
  // Pull back once the walk has finished so the verdict is read against
  // the shape of the whole route, which is the point of the trail.
  if (motionOn('sweep') && network && trail.edges.length) {
    const nodesInTrail = (trail.nodes || []).map((n) => n.id).filter((id) => nodeById(id));
    walkTimers.push(setTimeout(() => {
      if (network) moveCamera(fitTarget(nodesInTrail), 1100);
    }, ordered.length * step + 260));
  }

  return { missing, cited: cited.size };
}

/* ------------------------------------------------------ investigation */

const STEP_TITLE = {
  thought: 'Reasoning',
  action: 'Tool',
  observation: 'Observation',
  lead_dropped: 'Dropped lead',
  conclusion: 'Conclusion',
};

/*
 * The backend hands us whole steps, not tokens, so "streaming" here is
 * honest theatre with a hard budget: no step takes longer than one
 * entry animation to land, and long observations never type at all.
 */
function typeOut(node, text) {
  if (!micro() || text.length > 240) { node.textContent = text; return; }
  node.classList.add('caret');
  const perChar = Math.max(6, Math.min(18, 400 / Math.max(text.length, 1)));
  let i = 0;
  const timer = setInterval(() => {
    i += Math.max(1, Math.ceil(text.length / 60));
    node.textContent = text.slice(0, i);
    if (i >= text.length) {
      clearInterval(timer);
      node.textContent = text;
      node.classList.remove('caret');
    }
  }, perChar);
}

function appendStep(step) {
  const body = $('log-body');
  if (state.steps === 0) body.innerHTML = '';

  const wrap = el('div', `step step--${step.type} u-enter-sys`);

  const head = el('div', 'step__head');
  head.append(el('span', 'step__title', STEP_TITLE[step.type] || step.type));
  head.append(el('span', 'step__time', clockNow()));
  wrap.append(head);

  const detail = el('span', 'step__detail');
  wrap.append(detail);
  typeOut(detail, step.content || '');

  const refs = step.referenced_ids || [];
  if (refs.length) {
    const box = el('div', 'step__refs');
    for (const ref of refs.slice(0, 8)) box.append(el('span', 'step__ref', ref));
    wrap.append(box);
  }

  body.append(wrap);
  body.scrollTop = body.scrollHeight;

  // Real-time: the graph follows the agent's attention.
  markLive(refs);
  progressStep(step);

  state.steps += 1;
  if (step.type === 'lead_dropped') state.dropped += 1;
  $('log-meta').textContent = `${state.steps} ${plural(state.steps, 'step', 'steps')}`;
  setMeter('m-dropped', 'm-dropped-fill', String(state.dropped),
           state.steps ? state.dropped / state.steps : 0);
}


/* ---------------------------------------------------------------------
 * Progress while the agent works.
 *
 * The three meters can only read "—" until there is a verdict, so they
 * step aside and hand their slot to a bar. A ReAct step costs seconds
 * against a CPU-bound 7B, so the fill creeps forward inside the current
 * step instead of sitting still between them: a frozen bar reads as a
 * hung app, and the whole point is that nobody gets bored watching.
 * ------------------------------------------------------------------- */

const PHASE = {
  thought: 'Reasoning over the evidence',
  action: 'Querying the graph',
  observation: 'Reading what came back',
  lead_dropped: 'Dropping a lead',
  conclusion: 'Drafting the verdict',
};

let maxSteps = 12;
const progress = { step: 0, creep: 0, timer: null };

function buildPips(total) {
  const box = $('progress-pips');
  box.innerHTML = '';
  for (let i = 0; i < total; i += 1) box.append(el('span', 'progress__pip'));
}

function paintProgress() {
  const done = progress.step / maxSteps;
  const room = 1 / maxSteps;
  // Creep across at most 80% of the current step's slice, so the bar
  // always has somewhere left to go when the next step lands.
  const shown = Math.min(0.995, done + room * progress.creep * 0.8);
  $('progress-fill').style.width = `${(shown * 100).toFixed(1)}%`;
  $('progress-count').textContent = `step ${progress.step} of ${maxSteps}`;

  const pips = $('progress-pips').children;
  for (let i = 0; i < pips.length; i += 1) {
    pips[i].classList.toggle('is-done', i < progress.step);
    pips[i].classList.toggle('is-live', i === progress.step);
  }
}

function showProgress() {
  progress.step = 0;
  progress.creep = 0;
  buildPips(maxSteps);
  $('progress-phase').textContent = 'Preparing the investigation';
  $('progress-detail').textContent = integrations && integrations.agent.provider === 'cortex'
    ? 'Requesting the first step from Snowflake Cortex…'
    : 'Loading the model on the team server… the first step is the slowest.';
  paintProgress();
  $('gauges').hidden = true;
  $('progress').hidden = false;

  // Asymptotic creep: fast at first, never quite arriving.
  if (progress.timer) clearInterval(progress.timer);
  progress.timer = setInterval(() => {
    progress.creep += (1 - progress.creep) * 0.06;
    paintProgress();
  }, 260);
}

function hideProgress() {
  if (progress.timer) { clearInterval(progress.timer); progress.timer = null; }
  $('progress').hidden = true;
  $('gauges').hidden = false;      // the verdict's numbers get the slot back
}

function progressStep(step) {
  progress.step = Math.min(maxSteps, progress.step + 1);
  progress.creep = 0;              // a real step resets the optimism
  $('progress-phase').textContent = PHASE[step.type] || step.type;

  const text = (step.content || '').replace(/\s+/g, ' ').trim();
  $('progress-detail').textContent = text.length > 150 ? `${text.slice(0, 148)}…` : text;
  paintProgress();
}

function setRunning(running) {
  state.running = running;
  if (running) showProgress(); else hideProgress();
  $('btn-investigate').hidden = running;
  $('btn-investigate').disabled = running;
  $('btn-stop').hidden = !running;
  $('btn-inject').disabled = running;
  $('log-beacon').hidden = !running;

  const pill = $('run-state');
  pill.className = `pill ${running ? 'pill--live' : 'pill--idle'}`;
  $('run-state-text').textContent = running ? 'Investigating' : 'Idle';
  if (running) setSeal('running');

  if (running) {
    state.startedAt = Date.now();
    state.clockTimer = setInterval(() => {
      state.elapsed = (Date.now() - state.startedAt) / 1000;
      $('clock').textContent = hhmmss(state.elapsed);
    }, 500);
  } else {
    stopFollowing();
    if (state.clockTimer) {
      clearInterval(state.clockTimer);
      state.clockTimer = null;
    }
  }
}

async function loadStepBudget() {
  try {
    const status = await api('/investigate/status');
    if (status && status.max_steps) maxSteps = status.max_steps;
  } catch (_) { /* the default of 12 is the backend's default too */ }
}

async function investigate() {
  if (state.running) return;
  await loadStepBudget();      // draw the bar against the real budget
  state.steps = 0;
  state.dropped = 0;
  state.claims = 0;
  $('log-body').innerHTML = '';
  clearBanner('stream');
  setRunning(true);

  const hint = "Something looks off in this quarter's supplier payments";
  let response;
  try {
    response = await fetch(API + '/investigate', json({ hint }));
  } catch (err) {
    setRunning(false);
    setSeal('idle');
    showBanner('stream', {
      variant: 'error', icon: ICON_ERROR, title: 'Could not start the investigation',
      body: escapeHtml(err.message),
      actions: [{ label: 'Retry', className: 'btn--dark', onClick: investigate }],
    });
    return;
  }

  if (!response.ok) {
    setRunning(false);
    setSeal('idle');
    let detail = `HTTP ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* no body */ }

    if (response.status === 409) {
      // One investigation at a time: the cancel flag and the graph are
      // process-wide, so the backend refuses a second run.
      showBanner('stream', {
        variant: 'warn', icon: ICON_WARN,
        title: 'An investigation is already running',
        body: escapeHtml(detail),
        actions: [{ label: 'Cancel the current one', className: 'btn--dark', onClick: stopInvestigation }],
      });
    } else {
      showBanner('estate', {
        variant: 'empty', icon: ICON_INFO, title: 'No active scenario',
        body: `${escapeHtml(detail)} Inject a scenario before investigating.`,
        actions: [{ label: 'Inject scenario', className: 'btn--primary', onClick: openInjector }],
      });
    }
    return;
  }

  // Hand-rolled SSE reader: /investigate is a POST, and EventSource only
  // does GET.
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  // 'done' | 'error' | 'cut' | null. Never wait for 'done': when the
  // backend's worker thread raises it emits 'error' and closes the
  // stream, so 'done' may never arrive (api/README.md).
  let outcome = null;

  async function handleFrame(frame) {
    const line = frame.trim();
    if (!line.startsWith('data: ')) return;
    let payload;
    try { payload = JSON.parse(line.slice(6)); } catch (_) { return; }

    switch (payload.type) {
      case 'step':
        appendStep(payload.data);
        break;
      case 'done':
        outcome = 'done';
        state.investigationId = payload.investigation_id;
        $('btn-open-case').disabled = false;
        await loadCaseFile(payload.investigation_id, { silent: true });
        soundDone();          // the arpeggio lands once the file is on screen
        break;
      case 'error':
        // The only message that ever explains why a run died. Show it
        // verbatim instead of the generic "ended without a verdict".
        outcome = 'error';
        setSeal('idle');
        soundStopped();
        showBanner('stream', {
          variant: 'error', icon: ICON_ERROR,
          title: `The investigation failed at step ${state.steps}`,
          body: `<code>${escapeHtml(payload.message)}</code>`,
          actions: [{ label: 'Retry', className: 'btn--dark', onClick: investigate }],
        });
        break;
      default:
        // Unknown event type: the backend may add more after today.
        // Skip it rather than breaking the read loop.
        break;
    }
  }

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const frames = buffer.split('\n\n');
      buffer = frames.pop();                      // keep the partial frame
      for (const frame of frames) await handleFrame(frame);
      // An 'error' frame is the last thing the backend sends. Stop
      // reading instead of waiting for a 'done' that is not coming.
      if (outcome === 'error') { await reader.cancel(); break; }
    }
    // A final frame with no trailing blank line still counts.
    if (outcome === null && buffer.trim()) await handleFrame(buffer);
  } catch (err) {
    if (outcome === null) {
      outcome = 'cut';
      setSeal('idle');
      showBanner('stream', {
        variant: 'warn', icon: ICON_WARN,
        title: `Stream interrupted at step ${state.steps}`,
        body: `The connection dropped while the response was arriving: ${escapeHtml(err.message)}. `
            + `Steps 1‑${state.steps} were saved.`,
        actions: [{ label: 'Retry', className: 'btn--dark', onClick: investigate }],
      });
    }
  } finally {
    setRunning(false);
  }

  // Closed with neither 'done' nor 'error': don't leave the spinner
  // running forever waiting for an event that already didn't arrive.
  if (outcome === null) {
    setSeal('idle');
    showBanner('stream', {
      variant: 'warn', icon: ICON_WARN,
      title: 'The stream closed without a verdict',
      body: 'The backend closed the connection without sending <code>done</code> or <code>error</code>. '
          + 'Check the <code>uvicorn</code> console.',
      actions: [{ label: 'Retry', className: 'btn--dark', onClick: investigate }],
    });
  }
}


/* ---------------------------------------------------------------------
 * Rehearsal: replay a real investigation without the model.
 *
 * A live run costs about a minute against a CPU-bound 7B, which makes
 * it useless for judging the animations -- and risky as the thing you
 * lean on in front of judges. This replays the recorded step stream of
 * an investigation that actually happened, through exactly the same
 * code path (appendStep, markLive, highlightTrail), just faster.
 *
 * It is labelled as a rehearsal on screen the whole time. Nothing here
 * fabricates evidence: if no run has been recorded yet, it says so.
 * ------------------------------------------------------------------- */

const REHEARSE_MS = 260;      // between steps; a live run takes seconds
let rehearsing = false;

async function rehearse(pick) {
  if (state.running || rehearsing || !pick) return;

  let steps;
  let recordedGraph;
  try {
    const recording = await api(`/case-file/${pick.investigation_id}/steps`);
    steps = recording.steps || [];
    recordedGraph = recording.graph;
  } catch (err) {
    showBanner('stream', {
      variant: 'empty', icon: ICON_INFO, title: 'That case file has no recording',
      body: 'It was saved before the backend started recording steps. '
            + 'The next investigation will be available for rehearsal.',
    });
    return;
  }

  // Draw the graph the run actually happened on. Ids aren't reproducible
  // from the seed (uuid4 invoices and payments), so replaying over the
  // graph on screen lit up almost nothing.
  if (recordedGraph) {
    drawGraph(recordedGraph);
    // Let vis lay it out before the camera starts moving.
    await new Promise((done) => setTimeout(done, 700));
  }

  rehearsing = true;
  clearBanner('stream');
  $('log-body').innerHTML = '';
  state.steps = 0;
  state.dropped = 0;
  setRunning(true);
  $('run-state-text').textContent = 'Rehearsal';
  $('progress-detail').textContent = 'Replaying a recorded investigation…';

  try {
    for (const step of steps) {
      if (!rehearsing) break;          // someone pressed Stop
      appendStep(step);
      await new Promise((done) => setTimeout(done, REHEARSE_MS));
    }
    if (rehearsing) {
      await loadCaseFile(pick.investigation_id, { silent: true });
      soundDone();
    }
  } finally {
    rehearsing = false;
    setRunning(false);
  }
}

async function stopInvestigation() {
  if (rehearsing) {            // a rehearsal needs no backend call
    rehearsing = false;
    return;
  }
  $('btn-stop').disabled = true;
  try {
    await api('/investigate/cancel', { method: 'POST' });
    $('run-state-text').textContent = 'Cancelling…';
    soundStopped();
  } catch (err) {
    showBanner('stream', {
      variant: 'warn', icon: ICON_WARN, title: 'Could not cancel',
      body: escapeHtml(err.message),
    });
  } finally {
    $('btn-stop').disabled = false;
  }
}

/* ------------------------------------------------- scenario injector */

const PATTERN_COPY = {
  fake_billing:   ['Fake billing', 'A supplier on the 69-B list bills for services that were never delivered.'],
  kickback_shell: ['Kickbacks via a shell', 'A paper company gets paid and sends the money back to an insider.'],
  round_tripping: ['Round-tripping', 'The payment leaves and comes back through a ring of suppliers until the trail goes cold.'],
  inflated_sales: ['Inflated sales', "Invoices above what was delivered, with payments that don't match the CFDI."],
};
const PATTERN_FALLBACK = Object.keys(PATTERN_COPY);

/*
 * The backend already owns the list of valid patterns, and says so in
 * its 400. Asking it with a name that cannot exist is cheaper than
 * keeping a second copy here that silently rots when a teammate adds
 * one -- inject_pattern validates before it mutates anything.
 */
async function probePatterns() {
  try {
    await api('/estate/inject-scenario', json({ pattern: '__probe__', params: {} }));
  } catch (err) {
    const found = String(err.message).match(/'([a-z_]+)'/g);
    if (found && found.length > 1) {
      return found.slice(1).map((s) => s.replace(/'/g, ''));
    }
  }
  return PATTERN_FALLBACK;
}

async function openInjector() {
  if (state.running) return;
  const injector = $('injector');
  injector.hidden = false;
  injector.classList.add('is-open');
  $('injector-msg').textContent = '';

  const list = $('injector-list');
  list.innerHTML = '';
  const patterns = await probePatterns();
  patterns.forEach((name, i) => {
    const [title, desc] = PATTERN_COPY[name]
      || [name, 'Pattern registered in the backend; no local description.'];
    const card = el('button', 'pattern u-tactile');
    card.style.animationDelay = `${i * 64}ms`;
    card.append(el('span', 'pattern__flash'));
    card.append(el('span', 'pattern__name', title));
    card.append(el('span', 'pattern__desc', desc));
    // The raw id is what /estate/inject-scenario takes; a judge asking
    // "what did you just run?" gets the exact string back.
    card.append(el('span', 'pattern__id', name));
    card.addEventListener('click', () => {
      card.classList.add('is-picked');
      injectScenario(name);
    });
    list.append(card);
  });

  loadRehearsals(patterns.length);
}

/* The recorded runs, offered as the second half of the injector. */
async function loadRehearsals(offset = 0) {
  const section = $('rehearse-section');
  const box = $('rehearse-list');
  box.innerHTML = '';

  let saved = [];
  try { saved = await api('/case-files'); } catch (_) { saved = []; }
  const replayable = (saved || []).filter((c) => c.has_steps);
  section.hidden = replayable.length === 0;

  // One compact row per run: a secondary option, not a fifth scenario.
  // The narrative lives in the case file; here it only crowded the list.
  replayable.forEach((run) => {
    const row = el('button', 'rehearse__row u-tactile');
    row.type = 'button';
    row.title = run.narrative_preview || '';

    const accused = run.num_implicated_suppliers;
    row.append(el('span', 'rehearse__play'));
    row.append(el('span', 'rehearse__label', 'Replay recording'));
    row.append(el('span', `rehearse__stat${accused ? '' : ' rehearse__stat--clean'}`, accused
      ? `${pesosShort(run.total_amount_at_risk)} · ${accused} ${plural(accused, 'accusation', 'accusations')}`
      : 'no accusation'));
    row.append(el('span', 'rehearse__id', run.investigation_id.slice(0, 8)));

    row.addEventListener('click', () => {
      closeInjector();
      rehearse(run);
    });
    box.append(row);
  });
}

function closeInjector() {
  const injector = $('injector');
  injector.classList.add('is-closing');
  setTimeout(() => {
    injector.classList.remove('is-open', 'is-closing');
    injector.hidden = true;
  }, 220);
}

async function injectScenario(pattern) {
  // With Snowflake each step is a warehouse load plus four detectors --
  // seconds, not milliseconds -- so say where the time is going.
  const warehouse = Boolean(integrations && integrations.snowflake.requested
                            && integrations.snowflake.configured);
  $('injector-msg').textContent = warehouse
    ? 'Generating a clean estate and loading it into Snowflake…' : 'Generating a clean estate…';
  try {
    // inject-scenario needs an estate; generating first is idempotent
    // enough for a demo and removes an ordering footgun.
    await api('/estate/generate', json({ seed: Number($('cfg-seed').value) || 42 }));
    $('injector-msg').textContent = warehouse
      ? `Burying '${pattern}' and running the detectors in Snowflake…`
      : `Burying '${pattern}'…`;
    await api('/estate/inject-scenario', json({ pattern, params: {} }));
    clearBanner('estate');
    $('scenario-title').textContent = (PATTERN_COPY[pattern] || [pattern])[0];
    $('scenario-title').title = pattern;
    state.investigationId = null;
    $('btn-open-case').disabled = true;
    setSeal('idle');
    await loadGraph();
    closeInjector();
  } catch (err) {
    $('injector-msg').textContent = `Could not inject it: ${err.message}`;
  }
}

/* ------------------------------------------------------------ casefile */

/*
 * Three outcomes, and they must not be confused on stage:
 *
 *   accused  the agent sustained at least one claim through the guardrail
 *   clean    it walked the graph and found nothing it could prove -- a
 *            real verdict, and the thing this product is proudest of
 *   failed   no claims AND no trail: the run never got off the ground,
 *            almost always because the model was unreachable. Showing
 *            that as "No fraud proven" would be a lie told in green.
 *
 * The discriminator is Diego's own is_worth_keeping() in api/state.py.
 */
function verdictOf(caseFile) {
  const claims = caseFile.implicated_suppliers || [];
  const trail = caseFile.evidence_trail || { nodes: [], edges: [] };
  if (claims.length) return 'accused';
  if (trail.edges.length || trail.nodes.length) return 'clean';
  return 'failed';
}

/* ---------------------------------------------------------------------
 * The dossier: four index tabs over four sheets.
 *
 * Opening a case plays once (playOpening): the cover swings open, the
 * verdict sheet slides out, a stamp strikes, the amount counts up. Any
 * click or key skips straight to the end. Every tab switch files the
 * current sheet away and deals the next one, its contents dealt too.
 * Every cleanup is a timer, never animationend: with motion off those
 * events never fire.
 * ------------------------------------------------------------------- */

const TABS = ['verdict', 'claims', 'dropped', 'trail'];
// How many items get dealt; the rest arrive already in place, so a long
// trail never makes anyone wait.
const DEAL = { cards: 6, chips: 6, leads: 6, rows: 14 };
const STAMP = {
  accused: ['FRAUD', 'PROVEN'],
  clean: ['NO CHARGES', 'CLOSED'],
  failed: ['DID NOT RUN', 'CHECK MODEL'],
};
const opening = { playing: false, timers: [] };
const sheetTimers = new Map();

function sheet(key) { return $(`sheet-${key}`); }
function openingDue() { return Boolean(state.caseFile) && state.caseSeq !== state.openedSeq; }

function selectTab(name, { animate = true, focus = false } = {}) {
  const next = TABS.indexOf(name);
  if (next < 0) return;
  // Closed folder: the tabs and the buttons under the cover do nothing.
  // (loadCaseFile's own reset passes animate:false and still runs.)
  if (animate && $('dossier').classList.contains('is-empty')) return;
  if (opening.playing) finishOpening();
  const prevKey = state.caseTab;
  const prev = TABS.indexOf(prevKey);
  const moved = next !== prev;
  // The tab already open: a double click must not cut its deal short or
  // jump its scroll back to the top.
  if (!moved && animate) {
    if (focus) $(`tab-${name}`).focus();
    return;
  }
  if (moved) state.sheetScroll[prevKey] = sheet(prevKey).scrollTop;
  state.caseTab = name;
  $('dossier').style.setProperty('--dir', next >= prev ? '1' : '-1');

  TABS.forEach((key, i) => {
    const tab = $(`tab-${key}`);
    tab.setAttribute('aria-selected', String(i === next));
    tab.tabIndex = i === next ? 0 : -1;
    const s = sheet(key);
    clearTimeout(sheetTimers.get(key));
    s.classList.remove('is-dealing', 'is-filing');
    s.removeAttribute('aria-hidden');
    if (key === name) s.hidden = false;
    else if (!(animate && moved && key === prevKey)) s.hidden = true;
  });
  $('dossier-folio').textContent = `SHEET ${String(next + 1).padStart(2, '0')}/04`;
  const incoming = sheet(name);
  if (moved) incoming.scrollTop = state.sheetScroll[name] || 0;
  if (focus) $(`tab-${name}`).focus();
  if (!animate || !moved) return;

  const outgoing = sheet(prevKey);
  outgoing.classList.add('is-filing');
  outgoing.setAttribute('aria-hidden', 'true');
  sheetTimers.set(prevKey, setTimeout(() => {
    outgoing.hidden = true;
    outgoing.classList.remove('is-filing');
    outgoing.removeAttribute('aria-hidden');
  }, reducedMotion() ? 0 : 180));
  void incoming.offsetWidth;                      // restart the deal
  incoming.classList.add('is-dealing');
  sheetTimers.set(name, setTimeout(() => incoming.classList.remove('is-dealing'), 1500));
}

function leaveCaseFile() {
  finishOpening();
  state.sheetScroll[state.caseTab] = sheet(state.caseTab).scrollTop;
  for (const key of TABS) {
    clearTimeout(sheetTimers.get(key));
    const s = sheet(key);
    s.classList.remove('is-dealing', 'is-filing');
    s.removeAttribute('aria-hidden');
    s.hidden = key !== state.caseTab;
  }
}

/* Numerals and folio first, then the long labels: measured, never
   guessed from the viewport width. */
function fitTabs() {
  const strip = $('dossier-strip');
  if (!strip.offsetWidth) return;                 // hidden: nothing to measure
  const list = $('dossier-tabs');
  const fits = () => {
    const last = list.lastElementChild;
    return last.offsetLeft + last.offsetWidth <= list.clientWidth;
  };
  strip.classList.remove('is-nofolio', 'is-compact', 'is-tight');
  if (!fits()) strip.classList.add('is-nofolio');
  if (!fits()) strip.classList.add('is-compact');
  if (!fits()) strip.classList.add('is-tight');
}

/* The exact final numbers. A count-up only ever animates toward these. */
/* Distinct edge ids the accusations cite. "Cited" means this set, never
   the whole evidence trail, which also holds edges the agent only read. */
function citedEdgeIds(cf) {
  const cited = new Set();
  for (const claim of cf.implicated_suppliers || []) {
    for (const id of claim.evidence_edge_ids || []) cited.add(id);
  }
  return cited;
}

function paintCaseNumbers(cf) {
  const claims = cf.implicated_suppliers || [];
  $('amount-value').textContent = verdictOf(cf) === 'accused' ? pesos(cf.total_amount_at_risk) : '$0';
  $('m-suppliers').textContent = String(claims.length);
  $('m-evidence').textContent = String(citedEdgeIds(cf).size);
}

function setDossierIndex(id, verdict, claims, leads, trail, dateText) {
  const short = id.slice(0, 8).toUpperCase();
  $('dossier').dataset.verdict = verdict;
  $('tab-count-claims').textContent = String(claims.length);
  $('tab-count-dropped').textContent = String(leads.length);
  $('tab-count-trail').textContent = String(trail.edges.length);
  $('tab-claims').classList.toggle('has-items', claims.length > 0);
  $('tab-dropped').classList.toggle('has-items', leads.length > 0);
  const [word, sub] = STAMP[verdict];
  $('stamp-word').textContent = word;
  $('stamp-sub').textContent = sub;
  $('stamp-ref').textContent = `CASE ${short}`;
  $('cover-id').textContent = `CASE · ${short}`;
  $('cover-meta').textContent = dateText;
  const nextBtn = $('verdict-next');
  nextBtn.hidden = verdict === 'failed';
  const [goto, label] = verdict === 'clean' ? ['dropped', 'Dropped leads'] : ['claims', 'Accusations'];
  nextBtn.dataset.gotoTab = goto;
  $('verdict-next-label').textContent = label;
}

function maybeOpenDossier() {
  if (state.view !== 'casefile' || !openingDue()) return;
  state.openedSeq = state.caseSeq;                // consumed: plays once per load
  playOpening();
}

function playOpening() {
  finishOpening();
  const view = $('view-casefile');
  const dossier = $('dossier');
  const cf = state.caseFile;
  const verdict = verdictOf(cf);
  const claims = cf.implicated_suppliers || [];
  const citedCount = citedEdgeIds(cf).size;
  dossier.classList.remove('is-empty');
  for (const tab of document.querySelectorAll('#dossier-tabs .tab')) tab.removeAttribute('aria-disabled');
  fitTabs();

  const at = (ms, fn) => opening.timers.push(setTimeout(fn, ms));
  const ints = (v) => String(Math.round(v));
  const counts = (base) => {
    if (verdict === 'accused' && cf.total_amount_at_risk > 0) {
      $('amount-value').textContent = pesos(0);
      at(base, () => tween($('amount-value'), cf.total_amount_at_risk, 600, (v) => pesos(Math.round(v))));
    }
    [['m-suppliers', claims.length], ['m-evidence', citedCount]].forEach(([id, n], i) => {
      if (!n) return;
      $(id).textContent = '0';
      at(base + i * 64, () => tween($(id), n, 400, ints));
    });
  };

  if (!cinematic()) {                             // .no-sweep or reduced motion
    if (!micro()) return;                         // the end state is already painted
    opening.playing = true;
    const stamp = $('verdict-stamp');
    stamp.classList.remove('is-landing'); void stamp.offsetWidth; stamp.classList.add('is-landing');
    counts(120);
    at(800, finishOpening);
    listenForSkip(view);
    return;
  }

  view.classList.remove('is-entering', 'is-opening');
  void view.offsetWidth;                          // restart every keyframe underneath
  view.classList.add('is-opening');
  opening.playing = true;
  if (micro()) {
    counts(1000);
    if (verdict !== 'failed') at(1190, () => strike(SCALE[0] / 2, { gain: 0.12, decay: 0.3 }));
  }
  at(1700, finishOpening);
  listenForSkip(view);
}

/* Skippable: the click or key still does its own job afterwards. */
function listenForSkip(view) {
  view.addEventListener('pointerdown', finishOpening, { capture: true, once: true });
  document.addEventListener('keydown', finishOpening, { capture: true, once: true });
}

function finishOpening() {
  if (!opening.playing) return;                   // idempotent
  opening.playing = false;
  opening.timers.forEach(clearTimeout);
  opening.timers = [];
  stopTweens();
  const view = $('view-casefile');
  view.classList.remove('is-opening');
  $('verdict-stamp').classList.remove('is-landing');
  view.removeEventListener('pointerdown', finishOpening, { capture: true });
  document.removeEventListener('keydown', finishOpening, { capture: true });
  if (state.caseFile) paintCaseNumbers(state.caseFile);
}

/* One requestAnimationFrame loop for every count-up, quartic ease-out. */
const tweens = new Set();
let tweenRaf = 0;
function tween(node, to, ms, format) {
  tweens.add({ node, to: Number(to) || 0, ms, format, t0: performance.now() });
  if (!tweenRaf) tweenRaf = requestAnimationFrame(stepTweens);
}
function stepTweens(now) {
  for (const tw of tweens) {
    const k = Math.min(1, Math.max(0, (now - tw.t0) / tw.ms));
    tw.node.textContent = tw.format(tw.to * (1 - Math.pow(1 - k, 4)));
    if (k >= 1) { tw.node.textContent = tw.format(tw.to); tweens.delete(tw); }
  }
  tweenRaf = tweens.size ? requestAnimationFrame(stepTweens) : 0;
}
function stopTweens() { tweens.clear(); cancelAnimationFrame(tweenRaf); tweenRaf = 0; }

function wireDossier() {
  for (const tab of document.querySelectorAll('#dossier-tabs [role="tab"]')) {
    tab.addEventListener('click', () => selectTab(tab.dataset.tab));
  }
  $('dossier-tabs').addEventListener('keydown', (e) => {
    const i = TABS.indexOf(state.caseTab);
    const n = TABS.length;
    const to = { ArrowRight: (i + 1) % n, ArrowLeft: (i - 1 + n) % n, Home: 0, End: n - 1 }[e.key];
    if (to === undefined) return;
    e.preventDefault();
    selectTab(TABS[to], { focus: true });
  });
  for (const btn of document.querySelectorAll('[data-goto-tab]')) {
    // Read at click time; focus follows, since this button's own sheet is
    // about to be hidden under it.
    btn.addEventListener('click', () => selectTab(btn.dataset.gotoTab, { focus: true }));
  }
  // 1-4 jump between sheets, unless someone is typing a question.
  document.addEventListener('keydown', (e) => {
    if (state.view !== 'casefile' || !state.caseFile || e.repeat || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.target.closest && e.target.closest('input, textarea, select, [contenteditable]')) return;
    const n = Number(e.key);
    if (n >= 1 && n <= TABS.length) {
      e.preventDefault();
      selectTab(TABS[n - 1], { focus: true });
    }
  });
  if (window.ResizeObserver) new ResizeObserver(() => fitTabs()).observe($('dossier-strip'));
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fitTabs);
}

async function loadCaseFile(id, { silent = false } = {}) {
  let caseFile;
  try {
    caseFile = await api(`/case-file/${id}`);
  } catch (err) {
    if (!silent) {
      showBanner('stream', {
        variant: 'error', icon: ICON_ERROR, title: 'Could not open the case file',
        body: escapeHtml(err.message),
      });
    }
    return;
  }

  const fresh = id !== state.loadedCaseId;
  state.loadedCaseId = id;
  state.investigationId = id;
  state.caseFile = caseFile;
  $('btn-open-case').disabled = false;

  const claims = caseFile.implicated_suppliers || [];
  const trail = caseFile.evidence_trail || { nodes: [], edges: [] };
  const leads = caseFile.leads_not_pursued || [];
  const verdict = verdictOf(caseFile);
  state.claims = claims.length;

  $('case-id').textContent = id.slice(0, 8).toUpperCase();
  // 24-hour, like the dashboard clock.
  const dateText = new Date().toLocaleString('en-US',
    { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
  $('case-date').textContent = dateText;

  const badge = $('case-severity');
  badge.hidden = verdict !== 'accused';
  if (verdict === 'accused') {
    const high = (caseFile.total_amount_at_risk || 0) >= 1e6;
    badge.className = `pill ${high ? 'pill--accused' : 'pill--idle'}`;
    badge.textContent = high ? 'HIGH EXPOSURE' : 'MEDIUM EXPOSURE';
  }

  const card = $('verdict');
  card.className = `card verdict verdict--${verdict === 'accused' ? 'guilty' : verdict}`;
  setSeal(verdict === 'accused' ? 'accused' : verdict === 'clean' ? 'clean' : 'idle');

  if (verdict === 'accused') {
    $('verdict-headline').textContent = 'Fraud backed by evidence';
    const citedCount = citedEdgeIds(caseFile).size;
    $('amount-note').textContent = citedCount === 1
      ? 'Every peso is backed by the one cited edge.'
      : `Every peso is backed by at least one of the ${citedCount} cited edges.`;
  } else if (verdict === 'clean') {
    $('verdict-headline').textContent = 'No fraud proven';
    $('amount-note').textContent =
      `The agent traversed ${trail.nodes.length} ${plural(trail.nodes.length, 'node', 'nodes')} `
      + 'and found no edge that could back an accusation. '
      + 'A zero here is a result, not a failure.';
  } else {
    $('verdict-headline').textContent = 'The investigation never ran';
    $('amount-note').textContent =
      'No steps and no trail: it is almost always the model, not the data. Check the server indicator.';
  }
  $('verdict-narrative').textContent = caseFile.scheme_narrative || '—';
  paintCaseNumbers(caseFile);
  $('m-time').textContent = state.elapsed ? `${Math.round(state.elapsed)} s` : '—';

  renderClaims(claims);
  renderDropped(leads, verdict);
  renderTrail(trail, claims);

  setDossierIndex(id, verdict, claims, leads, trail, dateText);
  if (fresh) resetInquiry();                      // answers about case A never land on case B
  state.sheetScroll = {};
  state.caseTab = 'verdict';
  selectTab('verdict', { animate: false });
  fitTabs();

  // Dashboard meters track the same run.
  const total = state.graph ? state.graph.nodes.length : 0;
  setMeter('m-trail-nodes', 'm-trail-fill',
           total ? `${trail.nodes.length}/${total}` : String(trail.nodes.length),
           total ? trail.nodes.length / total : 0);
  const cited = citedEdgeIds(caseFile).size;
  setMeter('m-edges', 'm-edges-fill', String(cited), cited ? Math.min(1, cited / 20) : 0);
  $('m-dropped-note').textContent = leads.length
    ? `${leads.length} ${plural(leads.length, 'lead', 'leads')} dropped with a reason`
    : 'Accusations with no edge to back them';

  const walk = highlightTrail(caseFile);
  // An archived case was investigated over a different estate; its edge
  // ids simply do not exist in the graph on screen. Say so rather than
  // drawing a path that silently omits half the evidence.
  if (walk && walk.missing) {
    showBanner('estate', {
      variant: 'empty', icon: ICON_INFO,
      title: 'Case file without its graph',
      body: `${walk.missing} of ${trail.edges.length} ${plural(trail.edges.length, 'edge', 'edges')} `
          + `in this case file ${plural(walk.missing, 'is', 'are')} not in the current estate. `
          + 'The case file is still complete; the graph on screen belongs to another scenario.',
    });
  }

  state.caseSeq += 1;
  // Plays now only if the case file is already on screen; otherwise
  // switchView plays it when the view is shown.
  maybeOpenDossier();
}

function renderClaims(claims) {
  const box = $('claims');
  box.innerHTML = '';
  $('claims-meta').textContent = claims.length
    ? `${claims.length} ${plural(claims.length, 'supplier', 'suppliers')} with cited evidence`
    : 'none';

  if (!claims.length) {
    const note = el('p', 'empty-note',
      'The agent did not sustain any accusation. What it could not prove is under Dropped leads, with the reason.');
    note.dataset.deal = '';
    note.style.setProperty('--i', 0);
    box.append(note);
    return;
  }

  // Dealt like index cards on every visit to the sheet; the chips behind
  // each one light up in turn. Only the first few, so nobody waits.
  claims.forEach((claim, i) => {
    const dealt = i < DEAL.cards;
    const row = el('article', 'claim');
    row.dataset.rfc = claim.supplier_rfc;
    if (dealt) { row.dataset.deal = ''; row.style.setProperty('--i', i); }
    const top = el('div', 'claim__top');
    top.append(el('span', 'claim__index', `A-${String(i + 1).padStart(2, '0')}`));
    top.append(el('span', 'claim__rfc', claim.supplier_rfc));
    top.append(el('span', 'claim__amount', pesos(claim.peso_amount)));
    row.append(top);
    row.append(el('span', 'claim__rule', claim.rule_broken));

    const edges = el('div', 'claim__edges');
    (claim.evidence_edge_ids || []).forEach((id, j) => {
      const chip = el('button', 'claim__edge', id);
      chip.type = 'button';
      chip.title = 'Focus this edge on the graph';
      if (dealt && j < DEAL.chips) { chip.dataset.deal = ''; chip.style.setProperty('--j', j); }
      chip.addEventListener('click', () => focusEdge(id));
      edges.append(chip);
    });
    row.append(edges);
    box.append(row);
  });
}

function renderDropped(leads, verdict) {
  const box = $('dropped');
  box.innerHTML = '';
  $('dropped-meta').textContent = leads.length ? `${leads.length} dropped` : 'none';

  let k = 0;
  const deal = (node) => {
    if (k < DEAL.leads) { node.dataset.deal = ''; node.style.setProperty('--i', k); }
    k += 1;
  };

  if (!leads.length) {
    const note = el('p', 'empty-note', 'The agent recorded no dropped leads in this run.');
    deal(note);
    box.append(note);
    return;
  }
  // With nothing proven, this list IS the case file: it is the only
  // record of what the agent looked at and why it let it go.
  if (verdict === 'clean') {
    const note = el('p', 'empty-note',
      'With no accusations, this is the case file: what the agent reviewed and why it let it go.');
    deal(note);
    box.append(note);
  }

  for (const lead of leads) {
    const row = el('div', 'dropped');
    deal(row);
    const ids = el('div', 'dropped__ids');
    for (const id of lead.entity_ids || []) ids.append(el('span', 'dropped__id', id));
    row.append(ids);
    row.append(el('span', 'dropped__reason', lead.reason));
    box.append(row);
  }
}

function renderTrail(trail, claims) {
  const rows = $('trail-rows');
  rows.innerHTML = '';
  const cited = new Set();
  for (const claim of claims) for (const id of claim.evidence_edge_ids || []) cited.add(id);

  const summary = `${trail.edges.length} ${plural(trail.edges.length, 'edge', 'edges')} · `
    + `${cited.size} cited as evidence`;
  $('trail-meta').textContent = summary;
  $('tab-trail').title = summary;

  if (!trail.edges.length) {
    rows.append(el('p', 'empty-note', 'The agent left no trail: the run never got to explore the graph.'));
    return;
  }

  trail.edges.forEach((edge, index) => {
    const row = el('div', 'trail__row');
    if (cited.has(edge.id)) row.classList.add('is-hot');
    row.dataset.edge = edge.id;
    // Printed like a ledger on each visit; the rest are already there.
    if (index < DEAL.rows) { row.dataset.deal = ''; row.style.setProperty('--i', index); }
    row.tabIndex = 0;
    row.setAttribute('role', 'button');
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); focusEdge(edge.id); }
    });
    row.append(el('span', 'trail__n', String(index + 1)));
    row.append(el('span', 'trail__id', edge.id));
    row.append(el('span', 'trail__type', edge.type));
    const amount = edge.attributes && (edge.attributes.amount || edge.attributes.total);
    row.append(el('span', 'trail__weight', amount ? pesosShort(amount) : '—'));
    row.addEventListener('click', () => focusEdge(edge.id));
    rows.append(row);
  });
}

function focusEdge(edgeId) {
  if (!network || !state.graphEdgeIds || !state.graphEdgeIds.has(edgeId)) return;
  switchView('dashboard');
  const edge = state.graph.edges.find((e) => e.id === edgeId);
  network.selectEdges([edgeId]);
  if (edge) moveCamera(nodeTarget(edge.source, 1.4), 420);
}

/* -------------------------------------------------------- case index */

async function loadCaseIndex() {
  const box = $('cases');
  box.innerHTML = '';
  let cases;
  try {
    cases = await api('/case-files');
  } catch (err) {
    box.append(el('p', 'empty-note', `Could not read the archive: ${err.message}`));
    return;
  }

  $('cases-meta').textContent = `${cases.length} ${plural(cases.length, 'case file', 'case files')}`;
  if (!cases.length) {
    box.append(el('p', 'empty-note',
      'No case files yet. The ones saved to disk reappear here when the API restarts.'));
    return;
  }

  for (const summary of cases) {
    const row = el('button', 'case-row u-enter-sys');
    row.append(el('span', 'case-row__id', summary.investigation_id.slice(0, 8).toUpperCase()));
    row.append(el('span', 'case-row__preview', summary.narrative_preview || '—'));
    const amount = el('span',
      `case-row__amount ${summary.num_implicated_suppliers ? 'case-row__amount--accused' : ''}`,
      summary.num_implicated_suppliers ? pesosShort(summary.total_amount_at_risk) : 'no charges');
    row.append(amount);
    row.addEventListener('click', async () => {
      if (row.disabled) return;          // a double click would replay the opening mid-swing
      row.disabled = true;
      try {
        await loadCaseFile(summary.investigation_id);
        switchView('casefile');
      } finally {
        row.disabled = false;
      }
    });
    box.append(row);
  }
}

/* ----------------------------------------------------------------- ask */

/* ---------------------------------------------------------------------
 * The examination record being drafted: the wait for an answer, shown honestly.
 *
 * /ask takes seconds and nobody can say how many, so there is no
 * percentage: an inked line that creeps toward (never past) 92%, the
 * three stages every answer goes through, and the real elapsed clock.
 * It completes only when the answer arrives. A failure is neutral ink,
 * never red -- red means evidence. A seq token plus an AbortController
 * keep a late answer from landing on a newer question or another case.
 * ------------------------------------------------------------------- */

const INQUIRY_PHASES = [
  { at: 0, step: 0, text: 'Sending the question with the case file' },
  { at: 900, step: 1, text: 'The model reviews the evidence trail' },
  { at: 5000, step: 2, text: 'Drafting the answer' },
];
const INQUIRY_SLOW_MS = 15000;
const INQUIRY_TIMEOUT_MS = 120000;
const INQUIRY_TAU_MS = 6000;       // ~58% at 6 s, ~80% at 12 s
const INQUIRY_CEILING = 0.92;
// agent/qa.py answers HTTP 200 with this when both models are down.
const NO_MODEL_PREFIX = 'Neither model is reachable';
const inquiry = { seq: 0, controller: null, bar: null, node: null, text: '' };

function buildInquiry() {
  const wrap = el('div', 'inquiry-wrap');
  const root = el('div', 'inquiry u-enter-sys');
  const head = el('div', 'inquiry__head');
  const phase = el('span', 'inquiry__phase');
  phase.setAttribute('role', 'status');
  phase.setAttribute('aria-live', 'polite');
  const clock = el('span', 'inquiry__clock', '0.0 s');
  clock.setAttribute('aria-hidden', 'true');
  head.append(phase, clock);
  const track = el('div', 'inquiry__track');
  track.setAttribute('role', 'progressbar');
  track.setAttribute('aria-label', "Waiting for the agent's answer");
  track.setAttribute('aria-valuetext', 'In progress');
  track.append(el('span', 'inquiry__ink'));
  const steps = el('ol', 'inquiry__steps');
  steps.setAttribute('aria-hidden', 'true');
  for (const label of ['Case file', 'Evidence', 'Answer']) steps.append(el('li', 'inquiry__step', label));
  const note = el('span', 'inquiry__note');
  const kicker = el('span', 'inquiry__kicker', 'Asking the agent');
  root.append(kicker, head, track, steps, note);
  wrap.append(root);
  return { wrap, root, kicker, phase, clock, track, steps: [...steps.children], note,
           phaseIndex: -1, slow: false, noted: false, timer: null };
}

function setInquiryPhase(bar, text) { bar.phase.replaceChildren(el('span', 'inquiry__phase-text', text)); }
function markInquirySteps(bar, live) {
  bar.steps.forEach((s, i) => {
    s.classList.toggle('is-done', i < live);
    s.classList.toggle('is-live', i === live);
  });
}

function paintInquiry(bar, ms) {
  bar.track.style.setProperty('--p', (INQUIRY_CEILING * (1 - Math.exp(-ms / INQUIRY_TAU_MS))).toFixed(3));
  bar.clock.textContent = `${(ms / 1000).toFixed(1)} s`;
  let phase = INQUIRY_PHASES[0];
  for (const p of INQUIRY_PHASES) if (ms >= p.at) phase = p;
  if (bar.phaseIndex !== phase.step) {
    bar.phaseIndex = phase.step;
    setInquiryPhase(bar, phase.text);
    markInquirySteps(bar, phase.step);
  }
  const noKey = Boolean(integrations && integrations.gemini && !integrations.gemini.configured);
  if (noKey && !bar.noted) {
    bar.noted = true;
    const who = integrations.agent && integrations.agent.provider === 'cortex' ? 'Snowflake Cortex' : "the team's model";
    bar.note.textContent = `No Gemini key: ${who} answers instead, which may take longer.`;
  } else if (!noKey && !bar.slow && ms >= INQUIRY_SLOW_MS) {
    bar.slow = true;
    bar.note.textContent = 'Taking longer than usual. If Gemini does not reply, the fallback model answers.';
  }
}
function stopInquiry(bar) {
  if (bar && bar.timer) { clearInterval(bar.timer); bar.timer = null; }
}

function askErrorText(err, timedOut) {
  if (timedOut) return `No answer within ${INQUIRY_TIMEOUT_MS / 1000} s.`;
  if (err && err.status === 404) return 'The API no longer has this case file in memory. Open it again from the Archive.';
  if (err && err.status === 422) return 'The API rejected the question: it was empty or too long.';
  if (err instanceof TypeError) return 'No connection to the API: the question never went out.';
  if (err && err.status >= 500) return `The server failed while preparing the answer (HTTP ${err.status}).`;
  return `The server responded with an error: ${(err && err.message) || String(err)}.`;
}

/* A newer question replaces a pending one: the old request is aborted
   (the server may still finish it) and its record files away. */
function supersedeInquiry() {
  if (!inquiry.node) return;
  const { node, bar, controller } = inquiry;
  stopInquiry(bar);
  controller.abort();
  setInquiryPhase(bar, 'Replaced by the new question');
  node.classList.add('is-superseded');
  setTimeout(() => node.remove(), micro() ? 200 : 0);
  Object.assign(inquiry, { node: null, bar: null, controller: null, text: '' });
  $('answer').removeAttribute('aria-busy');
}

function resetInquiry() {
  supersedeInquiry();
  inquiry.seq += 1;
  $('answer').replaceChildren(el('p', 'empty-note',
    "The agent's answers appear here, with the graph entities they relied on."));
}

function failInquiry(bar, message, question) {
  stopInquiry(bar);
  bar.root.classList.add('is-failed');
  bar.kicker.textContent = 'Question unanswered';
  bar.track.setAttribute('aria-valuetext', 'No answer');
  setInquiryPhase(bar, 'No answer came back');
  bar.note.textContent = '';
  const actions = el('div', 'inquiry__actions');
  const retry = el('button', 'btn btn--ghost u-tactile inquiry__retry', 'Retry');
  retry.type = 'button';
  retry.addEventListener('click', () => ask(question));
  actions.append(retry, el('span', 'inquiry__error', message));
  bar.root.append(actions);
}

function buildAnswer(answer, seconds) {
  const wrap = el('div', 'answer__a u-enter-sys');
  const avatar = el('span', 'avatar avatar--agent');
  // The agent answers as CORPIDE: its face is the logo, not the retired
  // magnifier mark.
  avatar.innerHTML = '<img src="logo-small.svg" alt="" width="28" height="28">';
  const body = el('div', 'answer__body');
  const text = el('p', 'answer__text');
  body.append(text);
  typeOut(text, answer.answer || '—');

  // The contract calls these referenced_ids (shared/schemas.py): the
  // graph entities the agent leaned on. Each one opens the sheet where
  // it lives in the file.
  const sources = answer.referenced_ids || [];
  if (sources.length) {
    const card = el('div', 'sources');
    card.append(el('span', 'sources__label', 'SOURCES CONSULTED'));
    for (const source of sources) {
      const item = el('button', 'sources__item', String(source));
      item.type = 'button';
      item.title = 'Show in the case file';
      item.addEventListener('click', () => revealInFile(String(source)));
      card.append(item);
    }
    body.append(card);
  }
  body.append(el('span', 'answer__timing', `answered in ${seconds.toFixed(1)} s`));

  wrap.append(avatar, body);
  return wrap;
}

async function ask(question) {
  if (!state.investigationId) return;
  const text = (question || $('ask-input').value || '').trim();
  if (!text) return;
  if (inquiry.node && inquiry.text === text) return;       // double Enter or double chip click

  supersedeInquiry();
  const seq = ++inquiry.seq;
  const caseId = state.investigationId;
  const controller = new AbortController();
  const box = $('answer');
  for (const old of box.querySelectorAll(':scope > .exchange:not(.is-superseded), :scope > .empty-note')) old.remove();

  const exchange = el('div', 'exchange');
  const q = el('div', 'answer__q u-enter-me');
  q.append(el('span', 'answer__q-text', text), el('span', 'avatar avatar--judge', 'J'));
  const bar = buildInquiry();
  exchange.append(q, bar.wrap);
  box.append(exchange);
  box.scrollTop = 0;
  box.setAttribute('aria-busy', 'true');
  $('ask-input').value = '';
  Object.assign(inquiry, { controller, bar, node: exchange, text });

  const started = performance.now();
  paintInquiry(bar, 0);
  bar.timer = setInterval(() => paintInquiry(bar, performance.now() - started), 200);
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, INQUIRY_TIMEOUT_MS);

  const release = () => {
    Object.assign(inquiry, { node: null, bar: null, controller: null, text: '' });
    box.removeAttribute('aria-busy');
  };
  let answer;
  try {
    answer = await api(`/case-file/${caseId}/ask`, { ...json({ question: text }), signal: controller.signal });
  } catch (err) {
    clearTimeout(timeout);
    if (seq !== inquiry.seq) return;                        // superseded: already filed away
    release();
    failInquiry(bar, askErrorText(err, timedOut), text);
    return;
  }
  clearTimeout(timeout);
  if (seq !== inquiry.seq) return;                          // a stale answer never renders
  release();
  // api() returns null for an unreadable body, which is also what a
  // timeout during the body read looks like.
  if (timedOut || !answer || typeof answer.answer !== 'string') {
    failInquiry(bar, timedOut ? askErrorText(null, true) : 'The API responded with no content.', text);
    return;
  }
  if (answer.answer.startsWith(NO_MODEL_PREFIX)) {
    failInquiry(bar, 'No model answered: neither the cloud model nor the fallback is available.', text);
    return;
  }

  const seconds = (performance.now() - started) / 1000;
  stopInquiry(bar);
  bar.root.classList.add('is-done');
  bar.track.style.setProperty('--p', '1');
  bar.track.setAttribute('aria-valuetext', 'Complete');
  markInquirySteps(bar, 3);
  bar.kicker.textContent = "Agent's answer";
  setInquiryPhase(bar, `Answer received · ${seconds.toFixed(1)} s`);
  await new Promise((done) => setTimeout(done, micro() ? 180 : 0));
  if (seq !== inquiry.seq || !exchange.isConnected) return; // a new question or case during the hold
  bar.wrap.classList.add('is-collapsing');
  setTimeout(() => bar.wrap.remove(), micro() ? 220 : 0);
  exchange.append(buildAnswer(answer, seconds));
}

/* A source from an answer: open the sheet it lives on and flag it. */
function revealInFile(id) {
  const q = window.CSS && CSS.escape ? CSS.escape(id) : id.replace(/"/g, '\\"');
  let key = 'claims';
  let node = document.querySelector(`#claims [data-rfc="${q}"]`);
  if (!node) { key = 'trail'; node = document.querySelector(`#trail-rows [data-edge="${q}"]`); }
  if (!node) return;
  // No deal: a dealt row is invisible for most of the flag's life.
  selectTab(key, { animate: false });
  const s = sheet(key);
  // Never scrollIntoView: it would scroll the overflow:hidden view too.
  s.scrollTop = Math.max(0, node.offsetTop - s.clientHeight / 3);
  node.classList.remove('is-flagged');
  void node.offsetWidth;
  node.classList.add('is-flagged');
  setTimeout(() => node.classList.remove('is-flagged'), 950);
}

/* ------------------------------------------------------------- config */

async function loadConfig() {
  try {
    const cfg = await api('/config/ollama');
    $('cfg-url').value = cfg.settings.url;
    $('cfg-model').append(new Option(cfg.settings.model, cfg.settings.model));
    applyEnvOverrides(cfg);
  } catch (_) { /* refreshOllama's banner already says what's wrong */ }
}

async function probeModels() {
  const url = $('cfg-url').value.trim();
  $('cfg-msg').textContent = 'Probing…';
  try {
    const probe = await api(`/config/ollama/models?url=${encodeURIComponent(url)}`);
    const select = $('cfg-model');
    select.innerHTML = '';
    for (const model of probe.models) select.append(new Option(model, model));
    $('cfg-msg').textContent = probe.message;
  } catch (err) {
    $('cfg-msg').textContent = `Could not probe: ${err.message}`;
  }
}

async function saveConfig() {
  try {
    const current = await api('/config/ollama');
    const saved = await api('/config/ollama', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...current.settings,
        url: $('cfg-url').value.trim() || current.settings.url,
        model: $('cfg-model').value || current.settings.model,
      }),
    });
    $('cfg-msg').textContent = `Saved to ${saved.config_file}`;
    applyEnvOverrides(saved);      // may immediately say "but .env wins"
    await refreshOllama();
  } catch (err) {
    $('cfg-msg').textContent = `Could not save: ${err.message}`;
  }
}

function applyEnvOverrides(cfg) {
  // shared/config.py merges {**file, **env}: the environment WINS. If a
  // key is pinned in .env, saving from this panel changes the file and
  // nothing else -- so disable the field instead of pretending it took.
  const pinned = cfg.env_overrides || {};
  const fields = { url: 'cfg-url', model: 'cfg-model' };
  const labels = { url: 'The server URL', model: 'The model' };
  const names = [];
  for (const [key, id] of Object.entries(fields)) {
    const field = $(id);
    if (!field) continue;
    field.disabled = Object.prototype.hasOwnProperty.call(pinned, key);
    if (field.disabled) names.push(names.length ? labels[key].toLowerCase() : labels[key]);
  }
  const save = $('btn-save-cfg');
  if (save) save.disabled = names.length === Object.keys(fields).length;
  if (names.length) {
    $('cfg-msg').textContent =
      `${names.join(' and ')} ${plural(names.length, 'is', 'are')} pinned by .env and `
      + `${plural(names.length, 'overrides', 'override')} anything you save here. `
      + 'To switch servers mid-demo, edit .env and restart the API.';
    $('cfg-msg').classList.add('field__note--pinned');
  }
  return pinned;
}

/* --------------------------------------------------------------- init */

function wire() {
  wireDossier();
  $('btn-inject').addEventListener('click', openInjector);
  $('btn-inject-empty').addEventListener('click', openInjector);
  $('btn-injector-close').addEventListener('click', closeInjector);
  $('injector-scrim').addEventListener('click', closeInjector);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('injector').classList.contains('is-open')) closeInjector();
  });

  $('btn-investigate').addEventListener('click', investigate);
  $('btn-stop').addEventListener('click', stopInvestigation);
  $('btn-open-case').addEventListener('click', () => {
    if (state.investigationId) switchView('casefile');
  });
  $('btn-back').addEventListener('click', () => switchView('dashboard'));
  $('btn-refresh-cases').addEventListener('click', loadCaseIndex);

  $('btn-ask').addEventListener('click', () => ask());
  $('ask-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') ask(); });
  for (const chip of document.querySelectorAll('.chip')) {
    chip.addEventListener('click', () => ask(chip.textContent));
  }

  $('ollama-status').addEventListener('click', refreshOllama);
  $('btn-probe').addEventListener('click', probeModels);
  $('btn-save-cfg').addEventListener('click', saveConfig);
  $('btn-generate').addEventListener('click', async () => {
    $('estate-msg').textContent = 'Generating…';
    try {
      const info = await api('/estate/generate', json({ seed: Number($('cfg-seed').value) || 42 }));
      $('estate-msg').textContent =
        `${info.num_companies} ${plural(info.num_companies, 'company', 'companies')} · `
        + `${info.num_invoices} ${plural(info.num_invoices, 'invoice', 'invoices')} · `
        + `${info.num_payments} ${plural(info.num_payments, 'payment', 'payments')}`;
      await loadGraph();
    } catch (err) {
      $('estate-msg').textContent = `Failed: ${err.message}`;
    }
  });
  $('btn-export').addEventListener('click', () => {
    if (!state.investigationId) return;
    window.open(`${API}/case-file/${state.investigationId}`, '_blank');
  });

  for (const name of Object.keys(MOTION)) {
    $(MOTION[name].sw).addEventListener('click', () => toggleMotion(name));
  }
  $('theme-toggle').addEventListener('click', toggleTheme);
  for (const item of document.querySelectorAll('.nav__item')) {
    item.addEventListener('click', () => switchView(item.dataset.view));
  }
}

/*
 * A run survives a page reload: the investigation lives in the API
 * process, not in this tab. Without this, reloading mid-demo shows an
 * idle dashboard and the next click gets a bare 409.
 */
async function resumeIfRunning() {
  try {
    const status = await api('/investigate/status');
    if (!status.running) return;
    setRunning(true);
    showBanner('stream', {
      variant: 'warn', icon: ICON_WARN,
      title: 'An investigation is in progress',
      body: 'It started before this page loaded, so its steps cannot be recovered. '
          + 'Wait for it to finish and open it from the Archive, or cancel it.',
      actions: [{ label: 'Cancel', className: 'btn--dark', onClick: stopInvestigation }],
    });
  } catch (_) { /* refreshOllama already reported a dead API */ }
}

async function init() {
  applyTheme(currentTheme());   // before the graph paints, so it paints once
  applyMotion();
  if (motionOn('ambience')) spawnMotes();
  wire();
  setSeal('idle');

  // /health/ollama waits on a socket to another laptop and takes ~6s to
  // give up when that laptop is asleep. Nothing else on this page
  // depends on the answer, so it must not hold the graph hostage.
  const health = refreshOllama();
  await Promise.all([loadGraph(), resumeIfRunning(), loadConfig()]);
  await health;
  // The server is another laptop on the wifi: keep checking quietly.
  setInterval(refreshOllama, 15000);
}

// Follow the OS only while the user hasn't picked a side themselves.
if (window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
    if (!localStorage.getItem(THEME_KEY)) applyTheme(currentTheme());
  });
}

init();
