# Linkup Deep Research vs Parallel Task Ultra

A reproducible benchmark pitting **Linkup Research** against **Parallel's Task API (`ultra`
processor)** on deep-research GTM workloads — company enrichment, competitive intelligence,
and sales-outreach research. Both providers answer the same 100 real-customer queries; a blind
LLM judge (**Claude Fable 5**) scores every answer on 7 dimensions, including a
dashboard-usability (conciseness) lens.

## Results (100 paired queries)

Scores are on a **0–5** scale. The two are near-level on the typical query — median total 3.7 vs
3.8, with 54 of 100 within ±0.25 points. Linkup runs **~5× faster** (199s vs 871s median, server-
side) at ~20% lower cost ($0.25 vs $0.30/query) and scores higher on source quality.


|                     | Linkup Research S | Parallel Task ultra |
| ------------------- | ----------------- | ------------------- |
| Mean / median total | 3.60 / 3.7        | 3.77 / 3.8          |
| Latency (median)    | 199s (~3.3 min)   | 871s (~14.5 min)    |
| Price / query       | $0.25             | $0.30               |


**Per-dimension** (0–5, median):


| Dimension       | Linkup  | Parallel |
| --------------- | ------- | -------- |
| signal_to_noise | 3.0     | 3.0      |
| source_quality  | **4.0** | 3.5      |
| accuracy        | 4.5     | 4.5      |
| completeness    | 4.5     | 4.5      |
| gtm_value       | 4.0     | 4.0      |
| specificity     | 4.5     | 4.5      |
| conciseness     | **2.0** | 1.5      |


## Structure

```
bench.py            single CLI: run | judge | report
data/queries.jsonl  the 100 deep-research queries
results/            answers/, grades.jsonl, results.json (this run's data)
requirements.txt    httpx
```

## Setup

```bash
pip install -r requirements.txt        # httpx
export LINKUP_API_KEY=...               # for: run linkup
export PARALLEL_API_KEYS=k1,k2,k3       # for: run parallel (sharded round-robin)
```

The judge uses the `claude` CLI (model `claude-fable-5`), which must be installed and authenticated.

## Run

```bash
python bench.py run linkup --depth S             # Linkup Research, all queries
python bench.py run parallel --processor ultra   # Parallel Task API, all queries
python bench.py judge                            # score every answer with Claude Fable 5
python bench.py report                           # print the comparison
```

Every command is **resumable** — answers and grades are checkpointed per item. If a run is
interrupted (or the machine sleeps), re-run the same command and it skips finished work; the
provider jobs run server-side, so a `run` can be stopped and resumed freely.

`results/` already contains this run's 200 answers and grades, so `python bench.py report`
reproduces the comparison above immediately.

## Methodology

- **Configs:** Linkup Research `reasoningDepth=S`, `sourcedAnswer` vs Parallel Task API `ultra`.
- **Queries:** 100 real customer deep-research queries from production logs. Anonymized archetypes:
competitive profiles (20), CRM enrichment (25), sales-outreach research (25), company
profiles (15), ownership classification (10), plus 5 originals.
- **Judge:** Claude Fable 5, blind, 7 dimensions (0–5): accuracy, completeness, gtm_value,
specificity, source_quality, signal_to_noise, conciseness. Verbatim answers are persisted, so
they can be re-judged without re-querying.
- **Fairness:** identical query set both sides; comparison computed only on queries both
providers completed.

