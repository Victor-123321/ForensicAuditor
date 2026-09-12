/*
 * Forensic Auditor dashboard.
 *
 * Talks to the FastAPI backend on the same origin, so there is no CORS
 * hop and no second server to start: `uvicorn api.main:app` serves both
 * the API and this page.
 *
 * Every number on screen comes from the API. Where the design showed a
 * metric the backend doesn't produce (the mocked "92% confianza"), the
 * gauge is relabelled to something real -- how much of the agent's
 * accusation survived the evidence guardrail -- rather than inventing a
 * figure a judge could ask about.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const API = '';                       // same origin
const GAUGE_CIRCUMFERENCE = 302;      // 2*pi*48, matching the design's arcs

const state = {
  investigationId: null,
  running: false,
  startedAt: null,
  clockTimer: null,
  steps: 0,
  dropped: 0,
  claims: 0,
  elapsed: 0,
  graphNodes: 0,
  sse: null,
};

/* ------------------------------------------------------------ helpers */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function hhmmss(seconds) {
  const m = Math.floor(seconds / 60), s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function clockNow() {
  return new Date().toLocaleTimeString('es-MX', { hour12: false });
}

function money(value) {
  if (!value) return '$0';
  if (value >= 1e6) return `$${(value / 1e6).toFixed(2)}M`;
  if (value >= 1e3) return `$${(value / 1e3).toFixed(0)}K`;
  return `$${value.toFixed(0)}`;
}

function setArc(id, fraction) {
  const arc = $(id);
  if (!arc) return;
  const filled = Math.max(0, Math.min(1, fraction || 0)) * GAUGE_CIRCUMFERENCE;
  arc.setAttribute('stroke-dasharray', `${filled} ${GAUGE_CIRCUMFERENCE}`);
}

async function api(path, options) {
  const resp = await fetch(API + path, options);
  let body = null;
  try { body = await resp.json(); } catch (_) { /* empty body is fine */ }
  if (!resp.ok) {
    const detail = (body && (body.detail || body.message)) || `HTTP ${resp.status}`;
    throw new Error(detail);
  }
  return body;
}

/* ------------------------------------------------------------ banners */
/* Artboard 3: A (blocking error), B (interrupted stream), D (empty).    */

function clearBanner(kind) {
  const existing = $('banners').querySelector(`[data-kind="${kind}"]`);
  if (existing) existing.remove();
}

function showBanner(kind, { variant, icon, title, body, actions = [] }) {
  clearBanner(kind);
  const banner = el('div', `banner banner--${variant}`);
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
      const button = el('button', `btn ${action.className || 'btn--ghost'}`, action.label);
      button.addEventListener('click', action.onClick);
      box.append(button);
    }
    banner.append(box);
  }
  $('banners').append(banner);
}

const ICON_ERROR = '<svg class="banner__icon" width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="#B4262C" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5 21 19H3z"></path><path d="M12 9.5v4.5"></path><path d="M12 16.6h.01"></path></svg>';
const ICON_WARN = '<svg class="banner__icon" width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#8A5A0B" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H12z"></path></svg>';
const ICON_INFO = '<svg class="banner__icon" width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#3781C2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"></circle><path d="M12 8h.01"></path><path d="M11.5 12h1v5"></path></svg>';

/* ------------------------------------------------------ ollama status */

async function refreshOllama() {
  const status = $('ollama-status');
  try {
    const health = await api('/health/ollama');
    if (health.ok && health.model_available !== false) {
      status.classList.remove('is-down');
      $('ollama-title').textContent = 'Ollama conectado';
      $('ollama-detail').textContent = `${health.model} · ${health.url.replace(/^https?:\/\//, '')}`;
      clearBanner('ollama');
      return true;
    }
    // Reachable but the model is missing is still a blocking state.
    status.classList.add('is-down');
    $('ollama-title').textContent = health.ok ? 'Modelo no disponible' : 'Ollama no disponible';
    $('ollama-detail').textContent = health.model || '—';
    showBanner('ollama', {
      variant: 'error',
      icon: ICON_ERROR,
      title: health.ok ? 'El modelo no está en el servidor' : 'Ollama no disponible',
      // health.message is the client's own human-facing diagnosis.
      body: escapeHtml(health.message),
      actions: [
        { label: 'Reintentar', className: 'btn--danger', onClick: refreshOllama },
        { label: 'Ver diagnóstico', onClick: () => switchView('scenarios') },
      ],
    });
    return false;
  } catch (err) {
    status.classList.add('is-down');
    $('ollama-title').textContent = 'API no disponible';
    $('ollama-detail').textContent = 'localhost:8000';
    showBanner('ollama', {
      variant: 'error',
      icon: ICON_ERROR,
      title: 'No hay backend',
      body: `No pude hablar con la API: ${escapeHtml(err.message)}. ¿Está corriendo <code>uvicorn api.main:app</code>?`,
      actions: [{ label: 'Reintentar', className: 'btn--danger', onClick: refreshOllama }],
    });
    return false;
  }
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}

/* --------------------------------------------------------------- graph */

let network = null;

async function loadGraph() {
  let data;
  try {
    data = await api('/graph/export');
  } catch (err) {
    // 400 = no estate yet. Artboard 3-D.
    $('graph-sub').textContent = 'Sin estate generado';
    showBanner('estate', {
      variant: 'empty',
      icon: ICON_INFO,
      title: 'Sin escenario activo',
      body: 'Elige un escenario e inyéctalo para iniciar la investigación.',
      actions: [{ label: 'Inyectar', className: 'btn--primary', onClick: injectScenario }],
    });
    return;
  }
  clearBanner('estate');
  drawGraph(data);
}

const NODE_COLORS = {
  Company: '#3781C2',
  BankAccount: '#0D2A43',
  Invoice: '#8FA8BC',
  Person: '#1B9E8F',
};

function drawGraph(data, highlightIds = []) {
  const highlight = new Set(highlightIds);
  state.graphNodes = data.nodes.length;
  $('graph-sub').textContent =
    `${data.nodes.length.toLocaleString('es-MX')} nodos · ${data.edges.length.toLocaleString('es-MX')} aristas` +
    (highlight.size ? ' · rastro resaltado por el agente' : '');

  const nodes = data.nodes.map((node) => {
    const hot = highlight.has(node.id);
    const blacklisted = node.attributes && node.attributes.blacklist_status
      && node.attributes.blacklist_status !== 'none';
    return {
      id: node.id,
      label: node.label && node.label.length > 26 ? node.label.slice(0, 24) + '…' : node.label,
      title: `${node.type} · ${node.id}`,
      shape: 'dot',
      size: hot ? 18 : (node.type === 'Invoice' ? 6 : 11),
      color: {
        background: hot ? '#fff' : (NODE_COLORS[node.type] || '#C2CCD6'),
        border: hot || blacklisted ? '#D93B41' : '#C2CCD6',
        highlight: { background: '#fff', border: '#D93B41' },
      },
      borderWidth: hot ? 4 : (blacklisted ? 3 : 1),
      font: { size: hot ? 15 : 11, color: hot ? '#B4262C' : '#5E7488', face: 'IBM Plex Sans' },
    };
  });

  const edges = data.edges.map((edge) => {
    const hot = highlight.has(edge.source) && highlight.has(edge.target);
    return {
      id: edge.id,
      from: edge.source,
      to: edge.target,
      title: edge.type,
      color: { color: hot ? '#D93B41' : '#D3DBE3', highlight: '#D93B41' },
      width: hot ? 4.5 : 1.2,
      arrows: { to: { enabled: true, scaleFactor: hot ? 0.7 : 0.4 } },
    };
  });

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
  network = new vis.Network($('graph'), { nodes, edges }, options);
}

/* ------------------------------------------------------ investigation */

const STEP_KIND = {
  thought: { title: 'Razonando' },
  action: { title: 'Herramienta' },
  observation: { title: 'Observación' },
  lead_dropped: { title: 'Pista descartada', className: 'step--dropped' },
  conclusion: { title: 'Conclusión', className: 'step--critical' },
};

function appendStep(step) {
  const body = $('log-body');
  if (state.steps === 0) body.innerHTML = '';

  // The previous "typing" step is now settled.
  const active = body.querySelector('.step--active');
  if (active) active.classList.remove('step--active');

  const kind = STEP_KIND[step.type] || { title: step.type };
  const wrap = el('div', `step ${kind.className || ''}`);

  const rail = el('div', 'step__rail');
  rail.append(el('span', 'step__dot'), el('span', 'step__line'));

  const content = el('div', 'step__body');
  content.append(el('span', 'step__time', clockNow()));
  content.append(el('span', 'step__title', kind.title));

  const detail = el('span', 'step__detail', step.content || '');
  if (step.type === 'action' || step.type === 'observation') {
    detail.classList.add('step__detail--mono');
    if ((step.content || '').length > 220) {
      detail.textContent = step.content.slice(0, 220) + '…';
      detail.title = step.content;
    }
  }
  content.append(detail);

  wrap.append(rail, content);
  body.append(wrap);
  body.scrollTop = body.scrollHeight;

  state.steps += 1;
  if (step.type === 'lead_dropped') state.dropped += 1;
  $('log-meta').textContent = `stream · ${state.steps} paso${state.steps === 1 ? '' : 's'}`;
}

function setRunning(running) {
  state.running = running;
  $('btn-investigate').hidden = running;
  $('btn-stop').hidden = !running;
  $('btn-inject').disabled = running;
  $('log-beacon').classList.toggle('is-idle', !running);
  $('log-beacon').classList.toggle('pulse', running);

  const pill = $('run-state');
  pill.className = `pill ${running ? 'pill--live' : 'pill--idle'}`;
  $('run-state-text').textContent = running ? 'Investigando' : 'En espera';

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
    response = await fetch(API + '/investigate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hint }),
    });
  } catch (err) {
    setRunning(false);
    showBanner('stream', {
      variant: 'error', icon: ICON_ERROR, title: 'No pude iniciar la investigación',
      body: escapeHtml(err.message),
      actions: [{ label: 'Reintentar', className: 'btn--danger', onClick: investigate }],
    });
    return;
  }

  if (!response.ok) {
    setRunning(false);
    showBanner('estate', {
      variant: 'empty', icon: ICON_INFO, title: 'Sin escenario activo',
      body: 'Genera el estate antes de investigar.',
      actions: [{ label: 'Inyectar', className: 'btn--primary', onClick: injectScenario }],
    });
    return;
  }

  // Hand-rolled SSE reader: /investigate is a POST, and EventSource only
  // does GET.
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let finished = false;

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const frames = buffer.split('\n\n');
      buffer = frames.pop();                      // keep the partial frame
      for (const frame of frames) {
        const line = frame.trim();
        if (!line.startsWith('data: ')) continue;
        let payload;
        try { payload = JSON.parse(line.slice(6)); } catch (_) { continue; }

        if (payload.type === 'step') {
          appendStep(payload.data);
        } else if (payload.type === 'done') {
          finished = true;
          state.investigationId = payload.investigation_id;
          $('btn-open-case').disabled = false;
          await loadCaseFile(payload.investigation_id, { silent: true });
        }
      }
    }
  } catch (err) {
    // Artboard 3-B: the connection died mid-verdict.
    showBanner('stream', {
      variant: 'warn', icon: ICON_WARN,
      title: `Stream interrumpido en el paso ${state.steps}`,
      body: `La conexión se cortó mientras llegaba la respuesta. Los pasos 1‑${state.steps} quedaron guardados.`,
      actions: [{ label: 'Reintentar', className: 'btn--dark', onClick: investigate }],
    });
  } finally {
    setRunning(false);
  }

  if (!finished) {
    showBanner('stream', {
      variant: 'warn', icon: ICON_WARN,
      title: 'La investigación terminó sin veredicto',
      body: 'El stream se cerró antes del evento final. Revisa el panel de razonamiento.',
      actions: [{ label: 'Reintentar', className: 'btn--dark', onClick: investigate }],
    });
  }
}

async function stopInvestigation() {
  try {
    await api('/investigate/cancel', { method: 'POST' });
    $('run-state-text').textContent = 'Cancelando…';
  } catch (err) {
    console.error(err);
  }
}

/* ------------------------------------------------------------ scenario */

async function injectScenario() {
  const pattern = $('scenario').value;
  $('btn-inject').disabled = true;
  try {
    // /estate/inject-scenario needs an estate; generating first is
    // idempotent enough for a demo and removes an ordering footgun.
    await api('/estate/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ seed: Number($('cfg-seed').value) || 42 }),
    });
    await api('/estate/inject-scenario', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pattern, params: {} }),
    });
    clearBanner('estate');
    await loadGraph();
  } catch (err) {
    showBanner('estate', {
      variant: 'error', icon: ICON_ERROR, title: 'No pude inyectar el escenario',
      body: escapeHtml(err.message),
    });
  } finally {
    $('btn-inject').disabled = false;
  }
}

/* ------------------------------------------------------------ casefile */

function severityOf(caseFile) {
  const amount = caseFile.total_amount_at_risk || 0;
  if (!caseFile.implicated_suppliers.length) return null;
  if (amount >= 1e6) return { label: 'SEVERIDAD ALTA', className: 'pill pill--live' };
  return { label: 'SEVERIDAD MEDIA', className: 'pill pill--idle' };
}

async function loadCaseFile(id, { silent = false } = {}) {
  let caseFile;
  try {
    caseFile = await api(`/case-file/${id}`);
  } catch (err) {
    if (!silent) alert(`No pude abrir el expediente: ${err.message}`);
    return;
  }

  const claims = caseFile.implicated_suppliers || [];
  const trail = caseFile.evidence_trail || { nodes: [], edges: [] };
  state.claims = claims.length;

  $('case-id').textContent = `Expediente ${id.slice(0, 8).toUpperCase()}`;
  $('case-date').textContent = new Date().toLocaleString('es-MX',
    { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });

  const severity = severityOf(caseFile);
  const badge = $('case-severity');
  badge.hidden = !severity;
  if (severity) { badge.className = severity.className; badge.textContent = severity.label; }

  const guilty = claims.length > 0;
  $('verdict').classList.toggle('verdict--clean', !guilty);
  $('verdict-headline').textContent = guilty ? 'Fraude probable' : 'Sin fraude probado';
  $('verdict-narrative').textContent = caseFile.scheme_narrative || '—';

  // Honest stand-in for the design's mocked "confianza": the share of
  // the agent's accusations that survived the evidence guardrail.
  const attempted = claims.length + state.dropped;
  const verified = attempted ? claims.length / attempted : 0;
  $('verdict-conf').textContent = attempted ? `${Math.round(verified * 100)}%` : '—';
  setArc('verdict-arc', attempted ? verified : 0);

  $('m-amount').textContent = money(caseFile.total_amount_at_risk);
  $('m-suppliers').textContent = String(claims.length);
  $('m-evidence').textContent = String(trail.edges.length);
  $('m-time').textContent = state.elapsed ? `${Math.round(state.elapsed)} s` : '—';

  // Gauges on the dashboard track the same run.
  setArc('g1-arc', state.graphNodes ? trail.nodes.length / state.graphNodes : 0);
  $('g1-value').textContent = String(trail.nodes.length);
  $('g1-unit').textContent = `/ ${state.graphNodes}`;
  $('g1-note').textContent = 'Nodos en el rastro de evidencia';

  $('g2-value').textContent = attempted ? `${Math.round(verified * 100)}%` : '—';
  setArc('g2-arc', verified);
  $('g2-note').textContent = state.dropped
    ? `${state.dropped} acusación(es) rechazadas por el guardrail`
    : 'Acusaciones que pasan el guardrail';

  $('g3-value').textContent = String(trail.edges.length);
  setArc('g3-arc', trail.edges.length ? Math.min(1, trail.edges.length / 20) : 0);

  renderTrail(claims, trail);

  // Highlight what the agent actually cited, on the live graph.
  const cited = new Set();
  for (const claim of claims) {
    for (const edgeId of claim.evidence_edge_ids || []) {
      const edge = (trail.edges || []).find((e) => e.id === edgeId);
      if (edge) { cited.add(edge.source); cited.add(edge.target); }
    }
  }
  if (cited.size) {
    try { drawGraph(await api('/graph/export'), [...cited]); } catch (_) { /* keep the old graph */ }
  }
}

function renderTrail(claims, trail) {
  const rows = $('trail-rows');
  rows.innerHTML = '';
  $('trail-meta').textContent =
    `${claims.length} acusación(es) · ${trail.edges.length} aristas · ${trail.nodes.length} nodos`;

  if (!claims.length) {
    rows.append(el('p', 'empty-note', 'El agente no sostuvo ninguna acusación con evidencia.'));
    return;
  }

  claims.forEach((claim, index) => {
    const row = el('div', 'trail__row');
    if (index === 0) row.classList.add('is-hot');
    row.append(el('span', 'trail__n', String(index + 1)));
    row.append(el('span', 'trail__id', claim.supplier_rfc));
    row.append(el('span', 'trail__type', claim.rule_broken));
    row.append(el('span', 'trail__weight', money(claim.peso_amount)));
    rows.append(row);
  });
}

/* ----------------------------------------------------------------- ask */

async function ask(question) {
  if (!state.investigationId) { alert('Abre un expediente primero.'); return; }
  const text = (question || $('ask-input').value || '').trim();
  if (!text) return;

  const box = $('answer');
  box.innerHTML = '';
  const q = el('div', 'answer__q');
  q.append(el('span', 'avatar avatar--judge', 'J'), el('span', 'answer__q-text', text));
  box.append(q);

  const pending = el('p', 'empty-note', 'Preguntando al agente…');
  box.append(pending);

  const started = performance.now();
  let answer;
  try {
    answer = await api(`/case-file/${state.investigationId}/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: text }),
    });
  } catch (err) {
    pending.textContent = `No pude preguntar: ${err.message}`;
    return;
  }
  pending.remove();

  const wrap = el('div', 'answer__a');
  const avatar = el('span', 'avatar avatar--agent');
  avatar.innerHTML = '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"></circle><path d="M20 20l-4.2-4.2"></path></svg>';
  const body = el('div', 'answer__body');
  body.append(el('p', 'answer__text', answer.answer || '—'));

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

/* -------------------------------------------------------------- views */

function switchView(name) {
  for (const view of document.querySelectorAll('.view')) {
    view.hidden = view.id !== `view-${name}`;
  }
  for (const item of document.querySelectorAll('.nav__item')) {
    item.classList.toggle('is-active', item.dataset.view === name);
  }
}

/* --------------------------------------------------------------- init */

function wire() {
  $('btn-inject').addEventListener('click', injectScenario);
  $('btn-investigate').addEventListener('click', investigate);
  $('btn-stop').addEventListener('click', stopInvestigation);
  $('btn-open-case').addEventListener('click', () => {
    if (state.investigationId) switchView('casefile');
  });
  $('btn-back').addEventListener('click', () => switchView('dashboard'));
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
      const info = await api('/estate/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seed: Number($('cfg-seed').value) || 42 }),
      });
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

  for (const item of document.querySelectorAll('.nav__item')) {
    item.addEventListener('click', () => switchView(item.dataset.view));
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
  }
  return pinned;
}

async function init() {
  wire();
  const health = await refreshOllama();
  if (health) {
    try {
      const cfg = await api('/config/ollama');
      $('cfg-url').value = cfg.settings.url;
      $('cfg-model').append(new Option(cfg.settings.model, cfg.settings.model));
      applyEnvOverrides(cfg);
    } catch (_) { /* the banner already says what's wrong */ }
  }
  await loadGraph();
  // The server is another laptop on the wifi: keep checking quietly.
  setInterval(refreshOllama, 20000);
}

init();
