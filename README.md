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
| **Aldo** | Data & Graph Engineer | `feature/data-graph` | `data/generator/` (synthetic estate, SAT blacklist ingestion, fraud-pattern injectors), `graph/` (graph builder + all 5 detectors) |
| **Angel** | Agent Engineer | `feature/agent` | `agent/` (ReAct loop, tools, prompts, evidence guardrail, Q&A grounding) |
| **Diego** | Backend/API Engineer | `feature/backend-api` | `api/` (FastAPI app, SSE streaming, case-file assembly, in-memory state) |
| **Victor** | Frontend/Demo Engineer | `feature/frontend-demo` | `ui/` (Streamlit app: live graph view, case file, scenario injector, Q&A box) |

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
| `ui/` | Victor | Streamlit demo app |
| `tests/` | everyone | pytest suite |

## Branches

- `main` — integration branch (this scaffold)
- `feature/data-graph` — Aldo
- `feature/agent` — Angel
- `feature/backend-api` — Diego
- `feature/frontend-demo` — Victor

Work on your own branch and open a PR back into `main` when a module is
ready to integrate — avoid committing directly to `main` after this
initial scaffold, so nobody's in-progress work blocks anybody else's.

## Setup

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

Known gaps worth knowing about before you start:
- The evidence trail in a case file currently exports the *whole*
  graph rather than just the subgraph the agent actually cited
  (`agent/loop.py::_build_case_file`) — fine for early testing, but
  Angel should tighten this before the demo since the guardrail's
  edge-id checks are only as meaningful as the trail they check against.
- `/case-file/{id}/ask` (FR-19) is stubbed in `api/main.py` — it
  returns the right response shape but doesn't call a model yet; that's
  Angel's to wire up, Diego's endpoint just needs the real answer.
- SAT's real CSV column layout should be double-checked by Aldo against
  the actual downloaded file (`data/generator/download_sat_blacklist.py`)
  — this sandbox's network couldn't reach SAT's server to verify it
  directly, so treat the parser's column assumptions as unverified
  until someone runs it from a normal connection.
