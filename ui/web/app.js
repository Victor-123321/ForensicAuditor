/*
 * Forensic Auditor dashboard.
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
  return new Date().toLocaleTimeString('es-MX', { hour12: false });
}

function pesos(value) {
  return new Intl.NumberFormat('es-MX', {
    style: 'currency', currency: 'MXN', maximumFractionDigits: 0,
  }).format(value || 0);
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
};

function motionOn(name) {
  return localStorage.getItem(MOTION[name].key) !== 'off';
}

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

/* -------------------------------------------------------------- views */

function switchView(name) {
  const changed = name !== state.view;
  state.view = name;

  for (const view of document.querySelectorAll('.view')) {
    const active = view.id === `view-${name}`;
    view.hidden = !active;
    // The sweep is the most recognisable thing in the interface, which
    // is exactly why it fires on a real view change and never on a
    // re-render of the view you are already looking at.
    if (active && changed) {
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
  idle:     { cls: '', word: 'SIN ABRIR', sub: 'EXPEDIENTE' },
  running:  { cls: 'seal--running', word: 'EN CURSO', sub: 'AUDITORÍA' },
  accused:  { cls: 'seal--accused', word: 'FRAUDE', sub: 'PROBADO' },
  clean:    { cls: 'seal--clean', word: 'SIN CARGOS', sub: 'ARCHIVADO' },
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
  let health;
  try {
    health = await api('/health/ollama');
  } catch (err) {
    setStatus('down', 'API no disponible', 'localhost:8000');
    showBanner('ollama', {
      variant: 'error', icon: ICON_ERROR, title: 'No hay backend',
      body: `No pude hablar con la API: ${escapeHtml(err.message)}. ¿Está corriendo <code>uvicorn api.main:app</code>?`,
      actions: [{ label: 'Reintentar', className: 'btn--dark', onClick: refreshOllama }],
    });
    return false;
  }

  const host = (health.url || '').replace(/^https?:\/\//, '');

  if (health.ollama_reachable && health.model_available) {
    setStatus('ok', 'Modelo listo', `${health.model} · ${host}`);
    clearBanner('ollama');
    return true;
  }

  if (health.ollama_reachable) {
    setStatus('warn', 'Modelo no encontrado', `${health.model} · ${host}`);
  } else if (health.cloud_fallback) {
    setStatus('warn', 'Usando respaldo en la nube', host);
  } else {
    setStatus('down', 'Ollama no responde', host || '—');
  }

  showBanner('ollama', {
    variant: health.cloud_fallback ? 'warn' : 'error',
    icon: health.cloud_fallback ? ICON_WARN : ICON_ERROR,
    title: health.ollama_reachable
      ? 'El modelo no está en el servidor'
      : (health.cloud_fallback ? 'Ollama caído, hay respaldo' : 'Ollama no disponible'),
    // health.message is the client's own human-facing diagnosis.
    body: escapeHtml(health.message),
    actions: [
      { label: 'Reintentar', className: 'btn--dark', onClick: refreshOllama },
      { label: 'Ajustes', onClick: () => switchView('settings') },
    ],
  });
  return Boolean(health.cloud_fallback);
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

const NODE_BASE = '#4A4238';
const NODE_TEXT = '#8A8073';

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

  let border = NODE_BASE;
  let text = NODE_TEXT;
  if (cleared) { border = '#6E8F62'; text = '#6E8F62'; }
  else if (flagged) { border = '#E0A03A'; }

  if (tone === 'suspect') { border = cleared ? '#6E8F62' : '#E0A03A'; text = '#E0A03A'; }
  if (tone === 'fraud' && !cleared) { border = '#C4462F'; text = '#C4462F'; }

  const size = tone === 'fraud' ? 17 : tone === 'suspect' ? 13 : (node.type === 'Invoice' ? 6 : 10);
  return {
    color: {
      background: tone === 'dim' ? '#1C1916' : '#241F1A',
      border,
      highlight: { background: '#241F1A', border: tone === 'fraud' ? '#C4462F' : '#E0A03A' },
    },
    borderWidth: tone === 'fraud' ? 4 : (tone === 'suspect' || flagged || cleared ? 2.5 : 1),
    size,
    font: { size: tone === 'dim' ? 10 : 14, color: text, face: 'IBM Plex Mono' },
    opacity: tone === 'dim' ? 0.45 : 1,
  };
}

function edgeStyle(tone) {
  const color = tone === 'fraud' ? '#C4462F' : tone === 'suspect' ? '#E0A03A' : '#2E2A24';
  return {
    color: { color, highlight: color, opacity: tone === 'dim' ? 0.5 : 1 },
    width: tone === 'fraud' ? 4 : tone === 'suspect' ? 2.2 : 1,
    arrows: { to: { enabled: true, scaleFactor: tone === 'dim' ? 0.35 : 0.65 } },
  };
}

async function loadGraph() {
  let data;
  try {
    data = await api('/graph/export');
  } catch (err) {
    // 400 = no estate yet. Not an error: it is the cold-start state, and
    // the empty panel inside the graph card already says what to do.
    $('graph-shell').classList.add('is-empty');
    $('graph-sub').textContent = 'Sin estate generado';
    return;
  }
  drawGraph(data);
}

function drawGraph(data) {
  cancelWalk();
  state.graph = data;
  state.graphEdgeIds = new Set(data.edges.map((e) => e.id));
  $('graph-shell').classList.toggle('is-empty', data.nodes.length === 0);
  $('graph-sub').textContent =
    `${data.nodes.length.toLocaleString('es-MX')} nodos · ${data.edges.length.toLocaleString('es-MX')} aristas`;

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
    interaction: { hover: true, tooltipDelay: 120 },
    nodes: { shadow: false },
    edges: { smooth: { type: 'continuous' } },
  };

  if (network) network.destroy();
  network = new vis.Network($('graph'), { nodes: nodeSet, edges: edgeSet }, options);
}

function nodeById(id) {
  return state.graph && state.graph.nodes.find((n) => n.id === id);
}

/* Live: paint what the current step touched, amber, as it arrives. */
function markLive(ids) {
  if (!nodeSet || !ids || !ids.length) return;
  for (const id of ids) {
    const node = nodeById(id);
    if (node) { nodeSet.update({ id, ...nodeStyle(node, 'suspect') }); continue; }
    if (state.graphEdgeIds && state.graphEdgeIds.has(id)) edgeSet.update({ id, ...edgeStyle('suspect') });
  }
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
  ordered.forEach((edge, i) => {
    if (!state.graphEdgeIds.has(edge.id)) { missing += 1; return; }
    const tone = cited.has(edge.id) ? 'fraud' : 'suspect';
    walkTimers.push(setTimeout(() => {
      edgeSet.update({ id: edge.id, ...edgeStyle(tone) });
      for (const endpoint of [edge.source, edge.target]) {
        const node = nodeById(endpoint);
        if (node) nodeSet.update({ id: endpoint, ...nodeStyle(node, tone) });
      }
    }, i * step));
  });

  $('graph-sub').textContent =
    `${state.graph.nodes.length.toLocaleString('es-MX')} nodos · ` +
    `${trail.edges.length} en el rastro · ${cited.size} citadas como evidencia`;

  return { missing, cited: cited.size };
}

/* ------------------------------------------------------ investigation */

const STEP_TITLE = {
  thought: 'Razonando',
  action: 'Herramienta',
  observation: 'Observación',
  lead_dropped: 'Pista descartada',
  conclusion: 'Conclusión',
};

/*
 * The backend hands us whole steps, not tokens, so "streaming" here is
 * honest theatre with a hard budget: no step takes longer than one
 * entry animation to land, and long observations never type at all.
 */
function typeOut(node, text) {
  if (!motionOn('micro') || text.length > 240) { node.textContent = text; return; }
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

  state.steps += 1;
  if (step.type === 'lead_dropped') state.dropped += 1;
  $('log-meta').textContent = `${state.steps} paso${state.steps === 1 ? '' : 's'}`;
  setMeter('m-dropped', 'm-dropped-fill', String(state.dropped),
           state.steps ? state.dropped / state.steps : 0);
}

function setRunning(running) {
  state.running = running;
  $('btn-investigate').hidden = running;
  $('btn-investigate').disabled = running;
  $('btn-stop').hidden = !running;
  $('btn-inject').disabled = running;
  $('log-beacon').hidden = !running;

  const pill = $('run-state');
  pill.className = `pill ${running ? 'pill--live' : 'pill--idle'}`;
  $('run-state-text').textContent = running ? 'Investigando' : 'En espera';
  if (running) setSeal('running');

  if (running) {
    state.startedAt = Date.now();
    state.clockTimer = setInterval(() => {
      state.elapsed = (Date.now() - state.startedAt) / 1000;
      $('clock').textContent = hhmmss(state.elapsed);
    }, 500);
  } else if (state.clockTimer) {
    clearInterval(state.clockTimer);
    state.clockTimer = null;
  }
}

async function investigate() {
  if (state.running) return;
  state.steps = 0;
  state.dropped = 0;
  state.claims = 0;
  $('log-body').innerHTML = '';
  clearBanner('stream');
  setRunning(true);

  const hint = 'Algo no cuadra en los pagos a proveedores de este trimestre';
  let response;
  try {
    response = await fetch(API + '/investigate', json({ hint }));
  } catch (err) {
    setRunning(false);
    setSeal('idle');
    showBanner('stream', {
      variant: 'error', icon: ICON_ERROR, title: 'No pude iniciar la investigación',
      body: escapeHtml(err.message),
      actions: [{ label: 'Reintentar', className: 'btn--dark', onClick: investigate }],
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
        title: 'Ya hay una investigación corriendo',
        body: escapeHtml(detail),
        actions: [{ label: 'Cancelar la actual', className: 'btn--dark', onClick: stopInvestigation }],
      });
    } else {
      showBanner('estate', {
        variant: 'empty', icon: ICON_INFO, title: 'Sin escenario activo',
        body: `${escapeHtml(detail)} Inyecta un escenario antes de investigar.`,
        actions: [{ label: 'Inyectar escenario', className: 'btn--primary', onClick: openInjector }],
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
        break;
      case 'error':
        // The only message that ever explains why a run died. Show it
        // verbatim instead of the generic "ended without a verdict".
        outcome = 'error';
        setSeal('idle');
        showBanner('stream', {
          variant: 'error', icon: ICON_ERROR,
          title: `La investigación falló en el paso ${state.steps}`,
          body: `<code>${escapeHtml(payload.message)}</code>`,
          actions: [{ label: 'Reintentar', className: 'btn--dark', onClick: investigate }],
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
        title: `Stream interrumpido en el paso ${state.steps}`,
        body: `La conexión se cortó mientras llegaba la respuesta: ${escapeHtml(err.message)}. `
            + `Los pasos 1‑${state.steps} quedaron guardados.`,
        actions: [{ label: 'Reintentar', className: 'btn--dark', onClick: investigate }],
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
      title: 'El stream se cerró sin veredicto',
      body: 'El backend cerró la conexión sin enviar <code>done</code> ni <code>error</code>. '
          + 'Revisa la consola de <code>uvicorn</code>.',
      actions: [{ label: 'Reintentar', className: 'btn--dark', onClick: investigate }],
    });
  }
}

async function stopInvestigation() {
  $('btn-stop').disabled = true;
  try {
    await api('/investigate/cancel', { method: 'POST' });
    $('run-state-text').textContent = 'Cancelando…';
  } catch (err) {
    showBanner('stream', {
      variant: 'warn', icon: ICON_WARN, title: 'No pude cancelar',
      body: escapeHtml(err.message),
    });
  } finally {
    $('btn-stop').disabled = false;
  }
}

/* ------------------------------------------------- scenario injector */

const PATTERN_COPY = {
  fake_billing:   ['Facturación falsa', 'Un proveedor en la lista 69-B factura servicios que nunca se prestaron.'],
  kickback_shell: ['Comisiones vía fantasma', 'Una empresa de papel cobra y devuelve el dinero a alguien de dentro.'],
  round_tripping: ['Dinero en círculo', 'El pago sale y vuelve por un anillo de proveedores hasta perder el rastro.'],
  inflated_sales: ['Ventas infladas', 'Facturas por encima de lo entregado, con pagos que no cuadran con el CFDI.'],
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
      || [name, 'Patrón registrado en el backend; sin descripción local.'];
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
  $('injector-msg').textContent = 'Generando estate limpio…';
  try {
    // inject-scenario needs an estate; generating first is idempotent
    // enough for a demo and removes an ordering footgun.
    await api('/estate/generate', json({ seed: Number($('cfg-seed').value) || 42 }));
    $('injector-msg').textContent = `Enterrando '${pattern}'…`;
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
    $('injector-msg').textContent = `No pude inyectarlo: ${err.message}`;
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
 *            that as "sin fraude probado" would be a lie told in green.
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

async function loadCaseFile(id, { silent = false } = {}) {
  let caseFile;
  try {
    caseFile = await api(`/case-file/${id}`);
  } catch (err) {
    if (!silent) {
      showBanner('stream', {
        variant: 'error', icon: ICON_ERROR, title: 'No pude abrir el expediente',
        body: escapeHtml(err.message),
      });
    }
    return;
  }

  state.investigationId = id;
  $('btn-open-case').disabled = false;

  const claims = caseFile.implicated_suppliers || [];
  const trail = caseFile.evidence_trail || { nodes: [], edges: [] };
  const leads = caseFile.leads_not_pursued || [];
  const verdict = verdictOf(caseFile);
  state.claims = claims.length;

  $('case-id').textContent = id.slice(0, 8).toUpperCase();
  $('case-date').textContent = new Date().toLocaleString('es-MX',
    { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });

  const badge = $('case-severity');
  badge.hidden = verdict !== 'accused';
  if (verdict === 'accused') {
    const high = (caseFile.total_amount_at_risk || 0) >= 1e6;
    badge.className = `pill ${high ? 'pill--accused' : 'pill--idle'}`;
    badge.textContent = high ? 'EXPOSICIÓN ALTA' : 'EXPOSICIÓN MEDIA';
  }

  const card = $('verdict');
  card.className = `card verdict verdict--${verdict === 'accused' ? 'guilty' : verdict}`;
  setSeal(verdict === 'accused' ? 'accused' : verdict === 'clean' ? 'clean' : 'idle');

  if (verdict === 'accused') {
    $('verdict-headline').textContent = 'Fraude sostenido con evidencia';
    $('amount-note').textContent =
      `Cada peso está respaldado por al menos una de las ${trail.edges.length} aristas citadas.`;
  } else if (verdict === 'clean') {
    $('verdict-headline').textContent = 'Sin fraude probado';
    $('amount-note').textContent =
      `El agente recorrió ${trail.nodes.length} nodos y no encontró una arista que sostuviera una acusación. `
      + 'Un cero aquí es un resultado, no un fallo.';
  } else {
    $('verdict-headline').textContent = 'La investigación no llegó a correr';
    $('amount-note').textContent =
      'Sin pasos y sin rastro: casi siempre es el modelo, no los datos. Revisa el indicador del servidor.';
  }
  $('verdict-narrative').textContent = caseFile.scheme_narrative || '—';
  $('amount-value').textContent = verdict === 'accused'
    ? pesos(caseFile.total_amount_at_risk)
    : '$0';

  $('m-suppliers').textContent = String(claims.length);
  $('m-evidence').textContent = String(trail.edges.length);
  $('m-time').textContent = state.elapsed ? `${Math.round(state.elapsed)} s` : '—';

  renderClaims(claims);
  renderDropped(leads, verdict);
  renderTrail(trail, claims);

  // Dashboard meters track the same run.
  const total = state.graph ? state.graph.nodes.length : 0;
  setMeter('m-trail-nodes', 'm-trail-fill',
           total ? `${trail.nodes.length}/${total}` : String(trail.nodes.length),
           total ? trail.nodes.length / total : 0);
  setMeter('m-edges', 'm-edges-fill', String(trail.edges.length),
           trail.edges.length ? Math.min(1, trail.edges.length / 20) : 0);
  $('m-dropped-note').textContent = leads.length
    ? `${leads.length} pista(s) descartadas con motivo`
    : 'Acusaciones sin una arista que las sostenga';

  const walk = highlightTrail(caseFile);
  // An archived case was investigated over a different estate; its edge
  // ids simply do not exist in the graph on screen. Say so rather than
  // drawing a path that silently omits half the evidence.
  if (walk && walk.missing) {
    showBanner('estate', {
      variant: 'empty', icon: ICON_INFO,
      title: 'Expediente sin su grafo',
      body: `${walk.missing} de ${trail.edges.length} aristas de este expediente no están en el estate actual. `
          + 'El expediente se lee completo; el grafo corresponde a otro escenario.',
    });
  }
}

function renderClaims(claims) {
  const box = $('claims');
  box.innerHTML = '';
  $('claims-meta').textContent = claims.length
    ? `${claims.length} proveedor(es) con evidencia citada`
    : 'ninguna';

  if (!claims.length) {
    box.append(el('p', 'empty-note',
      'El agente no sostuvo ninguna acusación. Todo lo que no pudo probar está abajo, con el motivo.'));
    return;
  }

  for (const claim of claims) {
    const row = el('div', 'claim u-enter-sys');
    const top = el('div', 'claim__top');
    top.append(el('span', 'claim__rfc', claim.supplier_rfc));
    top.append(el('span', 'claim__amount', pesos(claim.peso_amount)));
    row.append(top);
    row.append(el('span', 'claim__rule', claim.rule_broken));

    const edges = el('div', 'claim__edges');
    for (const id of claim.evidence_edge_ids || []) {
      const chip = el('span', 'claim__edge', id);
      chip.title = 'Enfocar esta arista en el grafo';
      chip.addEventListener('click', () => focusEdge(id));
      edges.append(chip);
    }
    row.append(edges);
    box.append(row);
  }
}

function renderDropped(leads, verdict) {
  const box = $('dropped');
  box.innerHTML = '';
  $('dropped-meta').textContent = leads.length ? `${leads.length} descartada(s)` : 'ninguna';

  if (!leads.length) {
    box.append(el('p', 'empty-note', 'El agente no registró pistas descartadas en esta corrida.'));
    return;
  }
  // With nothing proven, this list IS the case file: it is the only
  // record of what the agent looked at and why it let it go.
  if (verdict === 'clean') {
    box.append(el('p', 'empty-note',
      'Sin acusaciones, esto es el expediente: lo que el agente revisó y por qué no lo sostuvo.'));
  }

  for (const lead of leads) {
    const row = el('div', 'dropped u-enter-sys');
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

  $('trail-meta').textContent =
    `${trail.edges.length} arista(s) · ${cited.size} citada(s) como evidencia`;

  if (!trail.edges.length) {
    rows.append(el('p', 'empty-note', 'El agente no dejó rastro: la corrida no llegó a explorar el grafo.'));
    return;
  }

  trail.edges.forEach((edge, index) => {
    const row = el('div', 'trail__row');
    if (cited.has(edge.id)) row.classList.add('is-hot');
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
  if (edge) network.focus(edge.source, { scale: 1.4, animation: { duration: 420 } });
}

/* -------------------------------------------------------- case index */

async function loadCaseIndex() {
  const box = $('cases');
  box.innerHTML = '';
  let cases;
  try {
    cases = await api('/case-files');
  } catch (err) {
    box.append(el('p', 'empty-note', `No pude leer el archivo: ${err.message}`));
    return;
  }

  $('cases-meta').textContent = `${cases.length} expediente(s)`;
  if (!cases.length) {
    box.append(el('p', 'empty-note',
      'Todavía no hay expedientes. Los que se guardan en disco reaparecen aquí al reiniciar la API.'));
    return;
  }

  for (const summary of cases) {
    const row = el('button', 'case-row u-enter-sys');
    row.append(el('span', 'case-row__id', summary.investigation_id.slice(0, 8).toUpperCase()));
    row.append(el('span', 'case-row__preview', summary.narrative_preview || '—'));
    const amount = el('span',
      `case-row__amount ${summary.num_implicated_suppliers ? 'case-row__amount--accused' : ''}`,
      summary.num_implicated_suppliers ? pesosShort(summary.total_amount_at_risk) : 'sin cargos');
    row.append(amount);
    row.addEventListener('click', async () => {
      await loadCaseFile(summary.investigation_id);
      switchView('casefile');
    });
    box.append(row);
  }
}

/* ----------------------------------------------------------------- ask */

async function ask(question) {
  if (!state.investigationId) return;
  const text = (question || $('ask-input').value || '').trim();
  if (!text) return;

  const box = $('answer');
  box.innerHTML = '';
  const q = el('div', 'answer__q u-enter-me');
  q.append(el('span', 'answer__q-text', text), el('span', 'avatar avatar--judge', 'J'));
  box.append(q);

  const pending = el('p', 'empty-note', 'Preguntando al agente…');
  box.append(pending);

  const started = performance.now();
  let answer;
  try {
    answer = await api(`/case-file/${state.investigationId}/ask`, json({ question: text }));
  } catch (err) {
    pending.textContent = `No pude preguntar: ${err.message}`;
    return;
  }
  pending.remove();

  const wrap = el('div', 'answer__a u-enter-sys');
  const avatar = el('span', 'avatar avatar--agent');
  avatar.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"></circle><path d="M20 20l-4.2-4.2"></path></svg>';
  const body = el('div', 'answer__body');
  const text_ = el('p', 'answer__text');
  body.append(text_);
  typeOut(text_, answer.answer || '—');

  // The contract calls these referenced_ids (shared/schemas.py):
  // the graph entities the agent leaned on to answer.
  const sources = answer.referenced_ids || [];
  if (sources.length) {
    const card = el('div', 'sources');
    card.append(el('span', 'sources__label', 'FUENTES CONSULTADAS'));
    for (const source of sources) card.append(el('span', 'sources__item', String(source)));
    body.append(card);
  }
  body.append(el('span', 'answer__timing',
    `respondido en ${((performance.now() - started) / 1000).toFixed(1)} s`));

  wrap.append(avatar, body);
  box.append(wrap);
  $('ask-input').value = '';
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
  $('cfg-msg').textContent = 'Sondeando…';
  try {
    const probe = await api(`/config/ollama/models?url=${encodeURIComponent(url)}`);
    const select = $('cfg-model');
    select.innerHTML = '';
    for (const model of probe.models) select.append(new Option(model, model));
    $('cfg-msg').textContent = probe.message;
  } catch (err) {
    $('cfg-msg').textContent = `No pude sondear: ${err.message}`;
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
    $('cfg-msg').textContent = `Guardado en ${saved.config_file}`;
    applyEnvOverrides(saved);      // may immediately say "but .env wins"
    await refreshOllama();
  } catch (err) {
    $('cfg-msg').textContent = `No pude guardar: ${err.message}`;
  }
}

function applyEnvOverrides(cfg) {
  // shared/config.py merges {**file, **env}: the environment WINS. If a
  // key is pinned in .env, saving from this panel changes the file and
  // nothing else -- so disable the field instead of pretending it took.
  const pinned = cfg.env_overrides || {};
  const fields = { url: 'cfg-url', model: 'cfg-model' };
  const names = [];
  for (const [key, id] of Object.entries(fields)) {
    const field = $(id);
    if (!field) continue;
    field.disabled = Object.prototype.hasOwnProperty.call(pinned, key);
    if (field.disabled) names.push(key);
  }
  const save = $('btn-save-cfg');
  if (save) save.disabled = names.length === Object.keys(fields).length;
  if (names.length) {
    $('cfg-msg').textContent =
      `${names.join(' y ')} vienen fijados por .env y mandan sobre lo que guardes aquí. ` +
      `Para cambiar de servidor a media demo, edita .env y reinicia la API.`;
    $('cfg-msg').classList.add('field__note--pinned');
  }
  return pinned;
}

/* --------------------------------------------------------------- init */

function wire() {
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
    $('estate-msg').textContent = 'Generando…';
    try {
      const info = await api('/estate/generate', json({ seed: Number($('cfg-seed').value) || 42 }));
      $('estate-msg').textContent =
        `${info.num_companies} empresas · ${info.num_invoices} facturas · ${info.num_payments} pagos`;
      await loadGraph();
    } catch (err) {
      $('estate-msg').textContent = `Falló: ${err.message}`;
    }
  });
  $('btn-export').addEventListener('click', () => {
    if (!state.investigationId) return;
    window.open(`${API}/case-file/${state.investigationId}`, '_blank');
  });

  for (const name of Object.keys(MOTION)) {
    $(MOTION[name].sw).addEventListener('click', () => toggleMotion(name));
  }
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
      title: 'Hay una investigación en curso',
      body: 'Empezó antes de cargar esta página, así que sus pasos no se pueden recuperar. '
          + 'Espera a que termine y ábrela desde el archivo, o cancélala.',
      actions: [{ label: 'Cancelar', className: 'btn--dark', onClick: stopInvestigation }],
    });
  } catch (_) { /* refreshOllama already reported a dead API */ }
}

async function init() {
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

init();
