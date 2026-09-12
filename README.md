# The Forensic Auditor

HackMTY 2026 — Infosys track ("The Forensic Auditor"). An AI agent that
investigates a synthetic company's financial records, follows the money
through a relationship graph, and produces a defensible fraud case file.

Full spec: [`docs/srs-forensic-auditor-agent.md`](docs/srs-forensic-auditor-agent.md).
Architecture comparison behind the graph-based choice:
[`docs/propuestas-infosys-track.md`](docs/propuestas-infosys-track.md).
Challenge brief + HackMTY research notes:
[`docs/hackmty-2026-infosys-track-brief.md`](docs/hackmty-2026-infosys-track-brief.md).

## Who's doing what

| Person | Role | Branch | Owns |
|---|---|---|---|
| **Aldo** | Data & Graph Engineer | `data-graph` | `data/generator/` (synthetic estate, SAT blacklist ingestion, fraud-pattern injectors), `graph/` (graph builder + all 5 detectors) |
| **Angel** | Agent Engineer | `agent` | `agent/` (ReAct loop, tools, prompts, evidence guardrail, Q&A grounding) |
| **Diego** | Backend/API Engineer | `backend` | `api/` (FastAPI app, SSE streaming, case-file assembly, in-memory state) |
| **Victor** | Frontend/Demo Engineer | `frontend` | `ui/` (Streamlit app: live graph view, case file, scenario injector, Q&A box) |

`shared/schemas.py` is the frozen contract everyone imports from — don't
change it without telling the other three first. Full role details,
dependencies, and an hour-by-hour milestone plan for each person are in
the SRS, [section 8](docs/srs-forensic-auditor-agent.md#8-work-breakdown-for-the-team).

Assignment follows the order names were given (Aldo→Data/Graph,
Angel→Agent, Diego→Backend/API, Victor→Frontend/Demo) — nobody stated a
preference, so swap freely between yourselves if someone wants a
different module. The interfaces in `shared/schemas.py` and the API
contract below don't care whose name is on a branch.

## Repo layout

| Path | Owner | Purpose |
|---|---|---|
| `shared/schemas.py` | everyone (frozen contract) | Pydantic models used across all modules |
| `shared/config.py` | everyone | Ollama connection settings (env / user config file / defaults) |
| `agent/ollama_client.py` | Angel | Client for the LAN Ollama: probe, streaming, cancel, error translation |
| `scripts/check_ollama.py` | everyone | One-command "can I reach the model?" check |
| `data/generator/` | Aldo | Synthetic estate generator, SAT blacklist ingestion, fraud pattern injectors |
| `graph/` | Aldo | Graph builder + deterministic detectors |
| `agent/` | Angel | ReAct investigation loop, tools, prompts, evidence guardrail |
| `api/` | Diego | FastAPI backend, SSE streaming, in-memory state |
| `ui/web/` | Victor | **The demo dashboard** — HTML/JS served by the API at `/`, built from the Claude Design canvas |
| `ui/app.py` | Victor | Older Streamlit UI, kept as a fallback; not the one demoed |
| `tests/` | everyone | pytest suite |

## Branches

- `main` — integration branch, and the one that works
- `data-graph` — Aldo
- `agent` — Angel
- `backend` — Diego
- `frontend` — Victor

Work on your own branch and open a PR back into `main` when a module is
ready to integrate. **`git pull` before you start**: after every
integration `main` is fast-forwarded into all four branches, so a branch
you haven't pulled is stale — that is how an evening got spent building
the UI against defaults `main` had already replaced.

## Setup

On Windows (which is what the team is demoing from), use PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env

python -m scripts.check_ollama        # can I reach the team's model?
python -m pytest -q

# The API also serves the dashboard at http://localhost:8000/
uvicorn api.main:app --port 8000
```

On macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Aldo, first task: download the real SAT blacklist
python -m data.generator.download_sat_blacklist

# Sanity check: everything imports and the detectors/generator/guardrail work
pytest

# Run the full stack (needs an Ollama reachable -- see "The local model" below)
bash scripts/run_dev.sh
```

## The local model

The agent's loop runs on [Ollama](https://ollama.com), and it does
**not** have to be on your machine: one laptop serves `qwen2.5:7b` to
the whole team over the LAN, so nobody else needs a GPU or a 7 GB
download.

```bash
cp .env.example .env        # then set OLLAMA_URL to the server's IP
python -m scripts.check_ollama     # "can I reach the model?" in one command
```

`OLLAMA_URL` takes whatever shape you were handed — `192.168.1.50`,
`192.168.1.50:11434`, or `http://192.168.1.50:11434/api/generate` — the
client normalizes it. You can also set it from the UI's **Modelo local
(Ollama)** panel (server field + "Buscar modelos" button), which saves to
`~/.forensic_auditor/config.json`; a `.env` variable overrides that file.

Serving the model to the LAN takes three things on the server side
(`OLLAMA_HOST=0.0.0.0`, port 11434 open to the subnet only, and the
machine not falling asleep) — all of it, plus a troubleshooting table and
the client-isolation trap that breaks this on guest wifi, is in
[`docs/ollama-red-local.md`](docs/ollama-red-local.md).

> Ollama has no authentication. Keep it on the local network, scoped to
> your subnet in the firewall, and never port-forwarded to the internet.

With no `.env` at all, everything falls back to `http://localhost:11434`,
so a solo `ollama pull qwen2.5:7b` still works.

Fill in a cloud-model key in `.env` once Angel wires up the reserved
cloud calls (SRS FR-15, FR-19); the client will also use it as a
one-shot fallback if the LAN server disappears mid-demo.

## Status

Initial scaffold: every module has real, runnable logic behind it (not
just empty stubs) — the estate generator produces a working synthetic
data estate with 4 fraud patterns, the graph builder and 5 detectors
run and are unit-tested, the ReAct loop calls Ollama and enforces the
evidence guardrail, the API streams real SSE events, and the UI renders
a live graph and case file against it. Each module still has explicit
`TODO`s marking where the SRS's fuller requirements need to be built
out further (search the codebase for `TODO`). Start from your branch,
your module (table above), and the milestone plan in the SRS
(section 8.2).

**Run this first, or the star detector finds nothing:**

```bash
python -m data.generator.download_sat_blacklist
```

The real Article 69-B list (14,055 RFCs) is not in the repo —
`data/raw/` is gitignored — and without it `num_blacklisted` produces no
listed companies at all. It now warns loudly instead of failing
silently, but the list still has to be downloaded on each machine. It
also changes what `seed=42` generates, so two laptops only agree on the
data once both have it.

Known gaps worth knowing about before you start:
- `fake_billing` fires **no** detector (verified across 5 seeds), so if
  a judge picks that scenario the agent has no way in. Either give the
  pattern a tell an existing detector catches (`patterns.py`) or add a
  sixth detector for a newly-incorporated supplier with an outsized
  invoice.
- `generate_clean_control()` is only clean in ~86% of seeds: two clean
  suppliers can collide on a random address and trip
  `shared_attribute_cluster`.
- No detector fills `supporting_edge_ids`, so a detector-only
  accusation reaches the guardrail with no edges to cite and gets
  dropped. The prompt now tells the agent to call `get_neighbors` or
  `trace_payment_path` for a real edge id before accusing, but filling
  the field in `graph/detectors.py` is the proper fix.
- The cloud model (FR-15/FR-19) needs `CLOUD_LLM_API_KEY` in `.env`.
  Without it the narrative polish is skipped and `/ask` answers with the
  LAN model instead — both degrade cleanly, neither is as good.
