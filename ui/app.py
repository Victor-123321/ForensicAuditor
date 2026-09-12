"""
Streamlit demo UI (FR-20 - FR-22). Talks to the FastAPI backend over
plain HTTP + SSE.

Run alongside the API:
    uvicorn api.main:app --reload --port 8000   # terminal 1
    streamlit run ui/app.py                     # terminal 2
or: bash scripts/run_dev.sh
"""
from __future__ import annotations

import json

import requests
import streamlit as st
from pyvis.network import Network

API_BASE = "http://localhost:8000"

SERVER_HELP = """
**En el equipo que sirve el modelo** (el que tiene Ollama y la GPU):

1. Ponlo a escuchar en la red, no solo en localhost:
   - Windows: `setx /m OLLAMA_HOST 0.0.0.0:11434` (variable de *sistema*,
     para que el servicio la vea) y reinicia Ollama.
   - Linux (systemd): `sudo systemctl edit ollama.service` →
     `Environment="OLLAMA_HOST=0.0.0.0"` en `[Service]`, luego
     `sudo systemctl daemon-reload && sudo systemctl restart ollama`.
   - macOS: `launchctl setenv OLLAMA_HOST 0.0.0.0` y reinicia la app.
2. Descarga el modelo: `ollama pull qwen2.5:7b` (compruébalo con `ollama list`).
3. Abre el **11434/tcp** en su firewall, solo para el perfil de red privada
   y acotado a tu subred (p. ej. `192.168.1.0/24`), nunca a internet.
4. Evita que se suspenda a media demo:
   `powercfg /change standby-timeout-ac 0` (Windows).
5. Saca su IP local (`ipconfig` / `ip addr` / `ifconfig`).

**En este equipo**: escribe `http://<ip-del-servidor>:11434` arriba y pulsa
**Buscar modelos**.

⚠️ **Ollama no tiene autenticación**: cualquiera que alcance ese puerto puede
usar el modelo. Úsalo solo en la red local, nunca expuesto a internet.
La IP cambia al reconectar — pide una reserva DHCP en el router o usa el
nombre `.local` del equipo. Si el wifi tiene *aislamiento de clientes*
(típico en redes de invitados y de eventos), las laptops no se ven entre
sí aunque estén en la misma red: levanta un hotspot propio.
"""


def _ollama_settings_panel() -> None:
    """Server/model picker for the LAN Ollama (see docs/ollama-red-local.md).

    Everything goes through the API so the setting lands on the process
    that actually calls the model -- the backend, which may not be this
    machine either.
    """
    st.header("Modelo local (Ollama)")
    try:
        cfg = requests.get(f"{API_BASE}/config/ollama", timeout=5).json()
    except requests.RequestException as exc:
        st.error(f"No pude hablar con la API en {API_BASE}: {exc}")
        return

    settings = cfg["settings"]
    pinned = cfg.get("env_overrides", {})

    url = st.text_input("Servidor", value=settings["url"],
                        help="IP o nombre del equipo que corre Ollama. "
                             "Acepta 192.168.1.50, localhost:11434 o una URL completa.")

    if st.button("Buscar modelos", use_container_width=True):
        probe = requests.get(f"{API_BASE}/config/ollama/models",
                             params={"url": url}, timeout=20).json()
        st.session_state.ollama_probe = probe

    probe = st.session_state.get("ollama_probe")
    options = probe["models"] if probe and probe.get("models") else []
    if settings["model"] and settings["model"] not in options:
        options = [settings["model"], *options]

    model = st.selectbox("Modelo", options or [settings["model"]],
                         index=options.index(settings["model"]) if settings["model"] in options else 0)

    if probe:
        (st.success if probe["ok"] else st.error)(probe["message"])

    if st.button("Guardar", type="primary", use_container_width=True):
        body = {**settings, "url": url, "model": model}
        saved = requests.put(f"{API_BASE}/config/ollama", json=body, timeout=10).json()
        st.success(f"Guardado en {saved['config_file']}")
        st.session_state.ollama_probe = None

    if pinned:
        st.warning("Estas claves vienen del entorno (.env) y mandan sobre lo que guardes "
                   f"aquí: {', '.join(f'{k}={v}' for k, v in pinned.items())}")

    with st.expander("Cómo conectar con el equipo que tiene Ollama"):
        st.markdown(SERVER_HELP)


st.set_page_config(page_title="The Forensic Auditor", layout="wide")
st.title("The Forensic Auditor")

if "investigation_id" not in st.session_state:
    st.session_state.investigation_id = None
if "steps" not in st.session_state:
    st.session_state.steps = []

with st.sidebar:
    st.header("Data estate")
    seed = st.number_input("Seed", value=42)
    if st.button("Generate estate"):
        resp = requests.post(f"{API_BASE}/estate/generate", json={"seed": seed})
        st.session_state.last_estate_info = resp.json()
        st.success(str(resp.json()))

    st.divider()
    st.header("Judge: inject a scenario")
    pattern = st.selectbox("Pattern", ["fake_billing", "kickback_shell", "round_tripping", "inflated_sales"])
    if st.button("Inject fresh scheme"):
        requests.post(f"{API_BASE}/estate/inject-scenario", json={"pattern": pattern, "params": {}})
        st.success(f"Injected '{pattern}' -- the agent has not seen this yet")

    st.divider()
    st.header("Start investigation")
    hint = st.text_input("Hint", value="Something looks off in this quarter's supplier payments")
    run_col, stop_col = st.columns(2)
    run_clicked = run_col.button("Investigate", use_container_width=True)
    if stop_col.button("Detener", use_container_width=True):
        # The agent loop checks this once per streamed line, so it stops
        # within a token or two and returns an honest cancelled case file.
        requests.post(f"{API_BASE}/investigate/cancel", timeout=5)
        st.warning("Cancelando la generación en curso...")

    st.divider()
    _ollama_settings_panel()

col_graph, col_case = st.columns([3, 2])

with col_graph:
    st.subheader("Relationship graph")
    try:
        graph_data = requests.get(f"{API_BASE}/graph/export").json()
        net = Network(height="500px", width="100%", directed=True, bgcolor="#0e1117", font_color="white")
        for node in graph_data.get("nodes", []):
            net.add_node(node["id"], label=node["label"], title=node["type"])
        for edge in graph_data.get("edges", []):
            net.add_edge(edge["source"], edge["target"], title=edge["type"])
        net.save_graph("/tmp/forensic_auditor_graph.html")
        st.components.v1.html(open("/tmp/forensic_auditor_graph.html").read(), height=520)
    except Exception as e:  # noqa: BLE001
        st.info(f"Generate an estate first. ({e})")

with col_case:
    st.subheader("Investigation trace")
    trace_placeholder = st.empty()

    if run_clicked:
        st.session_state.steps = []
        with requests.post(f"{API_BASE}/investigate", json={"hint": hint}, stream=True) as resp:
            for line in resp.iter_lines():
                if not line or not line.startswith(b"data: "):
                    continue
                payload = json.loads(line[len(b"data: "):])
                if payload.get("type") == "step":
                    st.session_state.steps.append(payload["data"])
                    trace_placeholder.write(st.session_state.steps)
                elif payload.get("type") == "done":
                    st.session_state.investigation_id = payload["investigation_id"]

    if st.session_state.investigation_id:
        st.subheader("Case file")
        cf = requests.get(f"{API_BASE}/case-file/{st.session_state.investigation_id}").json()
        st.markdown(f"**Scheme:** {cf['scheme_narrative']}")
        st.markdown(f"**Total at risk:** ${cf['total_amount_at_risk']:,.2f} MXN")
        for claim in cf["implicated_suppliers"]:
            st.warning(f"**{claim['supplier_rfc']}** -- {claim['rule_broken']} -- "
                       f"${claim['peso_amount']:,.2f} MXN")
        with st.expander("Leads not pursued"):
            for lead in cf["leads_not_pursued"]:
                st.write(f"- {', '.join(lead['entity_ids'])}: {lead['reason']}")

        st.divider()
        st.subheader("Judge's question")
        question = st.text_input("Ask the agent to defend a finding")
        if st.button("Ask"):
            ans = requests.post(
                f"{API_BASE}/case-file/{st.session_state.investigation_id}/ask",
                json={"question": question},
            ).json()
            st.info(ans["answer"])
