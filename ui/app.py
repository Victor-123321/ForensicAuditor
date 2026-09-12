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
    run_clicked = st.button("Investigate")

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
