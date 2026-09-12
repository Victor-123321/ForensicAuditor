# `api/` — backend contract

Owner: Diego. Run it with `uvicorn api.main:app --port 8000` — **no
`--reload`** during a demo: `api/state.py` is memory, and a reload wipes
the estate, the graph and the running investigation.

This one process also serves the dashboard: `/` redirects to `/app/`.

## Endpoints

| Endpoint | Response | Errors |
|---|---|---|
| `POST /estate/generate` | `{seed, num_companies, num_invoices, num_payments}` | 400 on a bad value; 409 while investigating |
| `POST /estate/inject-scenario` | `{pattern, num_companies}` | 400 without an estate or on an unknown pattern (the detail lists the valid ones); 409 while investigating |
| `GET /graph/export` | `{nodes[{id,type,label,attributes}], edges[{id,source,target,type}]}` | 400 without an estate |
| `POST /investigate` | SSE — see below | 400 without an estate; **409 if one is already running** |
| `GET /investigate/status` | `{running, run_id, case_files}` | — |
| `POST /investigate/cancel` | `{cancelled, was_running}` | — |
| `GET /case-files` | `[{investigation_id, num_implicated_suppliers, total_amount_at_risk, narrative_preview}]`, biggest exposure first | — |
| `GET /case-file/{id}` | the full case file (FR-18) | 404 |
| `POST /case-file/{id}/ask` | `{answer, referenced_ids}` | 404; 422 if the question is empty or over 2000 chars |
| `GET /health` | `{status:"ok"}` | — |
| `GET /health/ollama` | `{ollama_reachable, ok, model, model_available, models, message, url, cloud_fallback}` | never fails — a dead server is a 200 with `ollama_reachable:false` |
| `GET /config/ollama` · `PUT` | `{settings, env_overrides, config_file}` | — |
| `GET /config/ollama/models?url=` | `{ok, models, message}` | never fails |

## The `/investigate` stream

`data: {json}` lines separated by a blank line. Three shapes:

```
data: {"type":"step","data":{"investigation_id","step_index","type","content","referenced_ids"}}
data: {"type":"done","investigation_id":"..."}
data: {"type":"error","message":"RuntimeError: ..."}
```

`step.data.type` ∈ `thought` · `action` · `observation` · `lead_dropped`
· `conclusion`.

**Never wait for `done`.** If the worker thread raises, you get `error`
and then the stream closes. Handle it: without that branch the UI spins
forever on a stream that already ended.

## Things worth knowing before you build against it

- **One investigation at a time.** `/investigate` answers 409 otherwise,
  and so do the two `/estate/*` endpoints while a run is live — mutating
  the graph mid-run made the case file cite edges the UI no longer had.
- **`/investigate/cancel` frees the slot immediately.** The cancel flag
  is only checked between streamed lines, so a run waiting on the first
  byte of a cold 7B would otherwise hold the slot for the whole
  `OLLAMA_TIMEOUT` (300s with our `.env`).
- **`total_amount_at_risk` always equals the sum of the surviving
  `peso_amount`s.** Safe to print large. But see the open issue below.
- **An empty case file is a valid result**, not an error: the agent
  refuses to accuse without evidence. Render it as "no fraud proven".
- **`blacklist_status` lives in `node.attributes`**, not at the node
  root. Only `presunto` and `definitivo` are accusable — `desvirtuado`
  and `sentencia_favorable` mean SAT cleared the company.
- **Case files survive a restart** (`case_files/`, reloaded in the
  lifespan). After one, `/case-file/{id}` and `/ask` work but
  `state.graph` is `None` until someone regenerates the estate. Don't
  assume a graph exists just because a case file does.
- **`env_overrides`**: anything in `.env` beats what the settings panel
  saves. Disable those fields instead of pretending the save took.

## Open issues that are NOT in `api/`

- **`agent/guardrail.py`** never compares `peso_amount` against the
  money on the edges the claim cites. Reproduced: a claim of
  $99,000,000 backed by a single $78,891.61 payment passes with no
  rejections, and the one real case file here claims $187,413.65 while
  its four cited payments total $220,413.74. The big number on screen is
  not checked against its own evidence. Angel's call.
- **`agent/qa.py`** returns the same `referenced_ids` regardless of the
  question, so an honest "that isn't in the evidence" still ships five
  "sources"; and a cancel truncates an in-flight `/ask`, serving the
  partial text as the answer.
- **`ui/`** ignores the `{"type":"error"}` event entirely, so the only
  message that explains a failed investigation is dropped. Victor's.
- The one snapshot in `case_files/` predates the guardrail issue above —
  regenerate it once that's fixed before relying on it as the demo's
  offline fallback.
