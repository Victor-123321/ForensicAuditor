# HackMTY 2026 — Infosys Track: Research Notes

_Compiled 2026-09-11, updated 2026-09-12. Source: "HackMTY_2026_Infosys_Challenge_Tracks" doc (pages 3–4, shared as screenshots) + public web research._

## 1. HackMTY — event scope and scale

HackMTY is billed as the largest student hackathon in Latin America / Mexico, hosted by Tec de Monterrey (QS 5-star, top ~40 global, top 10 LatAm). It's free to enter, in-person, student-focused, teams of up to 4.

Known historical scale points:
- 2015 (first editions): 162 participants
- 2024: held Sept 14–15 at the Monterrey campus
- 2025 (most recent completed edition): **821 registered participants**, Oct 24–26, 2025, at Arena Borregos, Monterrey. Sponsors included MLH, Capital One, GateGroup, Oracle, Banorte, Chubb, the Nuevo León state government, and OCV Monterrey. Tracks covered fintech, logistics/predictive analytics, smart cities/sustainability, and open AI-finance tools.

I could not find a public page for the **2026 edition** yet (no Devpost/MLH listing indexed) — the challenge doc is likely pre-release / distributed directly to prospective participants or track sponsors ahead of public registration opening. Infosys does not appear in the 2025 sponsor list, so the Infosys track looks like it's new for 2026.

## 2. The Infosys track — "The Forensic Auditor" (full brief)

**Sponsor context:** Infosys — global tech/consulting firm, deep finance/accounting/risk experience; Monterrey center has done bookkeeping, compliance, and fraud-investigation work for Americas clients since 2007.

**Problem:** Invoice fraud in Mexico isn't a clean flagged-row problem — it's hidden inside real books: fake supplier billing for work never done, kickbacks routed through shell companies, or sales faked to inflate numbers. SAT (Mexico's tax authority) publishes the Article 69-B blacklist of fake-invoice companies (EFOS), but by the time a supplier lands on it, the company has already claimed the deductions and is on the hook. Existing tools flag anomalies one at a time and don't connect them into a case; honest suppliers who just look odd can get wrongly accused.

**The big challenge:** Given a company's books and only a hint that something's wrong, can an AI agent find the fraud, follow the money, and prove it — without accusing anyone it can't back up with evidence?

**Suggested approaches (pick one or combine):**
1. **Investigate step by step** — form a theory, search the ledger/invoices/bank records, follow a lead, change course when it dead-ends.
2. **Simple anomaly detectors** — blacklisted suppliers, payments that don't match invoices, money moving in a circle — to point the agent at what's worth digging into.
3. **Evidence-trail builder** — for every accusation, cite the rule broken and the peso amount; refuse to name a supplier without backing evidence.

**What to build:** A forensic agent that investigates records it hasn't seen and hands over a case file: the scheme, the suppliers involved, the evidence trail, and the peso amount, plus a short list of leads it chose *not* to chase and why. Deliverable = working code + a 3-minute live demo where judges hide a fresh fraud scheme in the data and the agent traces the money on screen, then answers a surprise question about its reasoning live.

**Why it matters (per the sponsor):** every peso lost to fake invoices comes out of an honest business and the public purse, and every wrongly-accused supplier loses a customer for no reason. An agent that *investigates and proves*, rather than just flags, is what separates real forensic auditing (what Infosys does at scale) from a guesser.

**Judging criteria (confirmed from the full brief):**

| Criterion | What it means |
|---|---|
| **Results** | On records it has never seen, how much hidden fraud does the agent find and *correctly prove*? |
| **Judgment** | Does it refuse to accuse suppliers it can't back up, and can it defend a finding when a judge asks? |
| **Feasibility** | Could a real finance or audit team trust and use this? |
| **Clarity** | Is the case file easy to follow, with a clear money trail? |

## 3. Technical resources — feasibility check

| Resource | What it is | Status |
|---|---|---|
| **SAT Art. 69-B list (EFOS)** | Mexico's official blacklist of companies presumed/confirmed to issue fake invoices (CFF Art. 69-B), 4 status categories: Presunto, Definitivo, Desvirtuado, Sentencia Favorable | **Confirmed real & free.** Official CSV: `omawww.sat.gob.mx/cifras_sat/Documents/Listado_Completo_69-B.csv`. Also mirrored/searchable at datospublicos.mx/efos (~14k companies) and 69b.mx, but the SAT CSV is the authoritative free source — use that directly rather than a paid mirror. |
| **CFDI 4.0 schema** | Mexico's standard electronic invoice XML format (Comprobante Fiscal Digital por Internet) | Well-documented, XSD schemas published by SAT; synthetic CFDI-shaped invoices are the natural "ledger" format for this challenge. |
| **IBM AMLSim** | Open-source multi-agent simulator that generates synthetic banking transactions + known money-laundering patterns (fan-out, cycles/round-tripping, bipartite, gather-scatter) as CSV, plus ground-truth labels | **Confirmed, active on GitHub (IBM/AMLSim)**, MIT-ish research license. Good fit for generating the "money-flow rings" / round-tripping / shell-company payment patterns the brief mentions. There's also a plain `IBM/AML-Data` synthetic transaction dataset if you want pre-generated data instead of running the simulator. |
| **IEEE-CIS Fraud Detection (Kaggle)** | Large real-world-derived transaction fraud dataset (train/test, hundreds of anonymized features) used as an ML benchmark | Confirmed, available on Kaggle (`kaggle competitions ieee-fraud-detection`) and IEEE DataPort. Useful mainly as a baseline for anomaly-scoring features/tuning, less directly shaped like Mexican invoice fraud — would need adapting or treating as a secondary signal source rather than primary data. |
| **Ollama (local LLM)** | Local model runtime | Sponsor's own suggestion: since the agent architecture implies many LLM calls per investigation (multi-step agentic loop), a local model with caching avoids hitting the free-tier Gemini API's daily limits during dev/testing. Worth setting up early. |

## 4. Decision: Proposal 1 selected

The team compared three architectures (see `propuestas-infosys-track.md`) and **selected Proposal 1 — the graph-native investigative agent**: a relationship graph (companies, people, bank accounts, invoices, payments) with deterministic graph algorithms (cycle detection, shared-attribute clustering, centrality) as the detector layer, and an LLM agent that traverses the graph as its investigation tool. The extracted subgraph doubles as the evidence trail. Full technical spec: see the SRS doc `srs-forensic-auditor-agent.md`.

## 5. Open items to resolve

- Confirm HackMTY 2026 official dates/venue once registration opens (not yet publicly listed as of Sept 2026).
- Acquire and stage the actual data sources per the SRS's Data Acquisition plan: download the live SAT 69-B CSV, decide AMLSim (run vs. reimplement patterns), pull a CFDI 4.0 sample schema.
- Finalize LLM stack budget (Ollama model choice + which cloud model, if any, for final synthesis) once local hardware for the demo machine is known.

## Sources
- [HackMty 2025 Devpost](https://hackmty2025.devpost.com/)
- [HackMTY Devpost (main/history)](https://hackmty.devpost.com/)
- [HackMTY MLH event page](https://events.mlh.io/events/11458-hackmty)
- [SAT Art. 69-B listado completo (official CSV)](http://omawww.sat.gob.mx/cifras_sat/Documents/Listado_Completo_69-B.csv)
- [SAT Art. 69-B landing page](http://omawww.sat.gob.mx/cifras_sat/paginas/datos/vinculo.html?page=ListCompleta69.html)
- [datospublicos.mx EFOS search](https://datospublicos.mx/efos)
- [IBM/AMLSim on GitHub](https://github.com/IBM/AMLSim)
- [IBM/AML-Data on GitHub](https://github.com/IBM/AML-Data)
- [IEEE-CIS Fraud Detection on Kaggle](https://www.kaggle.com/competitions/ieee-fraud-detection/data)
