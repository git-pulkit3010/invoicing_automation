# Timings

Measured on 20 Nov 2026 in a Linux cloud workspace (Python 3.13, PyMuPDF 1.28.2), with a
clean virtual environment built only from `requirements.txt`. Times are wall-clock for one
run of each command. Agent times are from the agent tool's own duration and vary run to run
(roughly ±10–15 s for full-agent runs).

## Real batch: October 2026, 8 vendor invoices

8 invoices from 8 vendors (7 with a text layer, 1 scanned), 8 work orders, and an ATT Time
Report with 133 contractors and 2,763 daily rows. Every approach reached the same payment
decisions: 3 MATCH (go ahead). The rest went to a person (REVIEW_REQUIRED under Pulkit's rules,
which this branch now follows): 4 because their work orders had expired, 1 because it was a scan.

| Approach | Rules (no AI) | Agent | Decide | Total | Invoices sent to an agent |
| --- | --- | --- | --- | --- | --- |
| **Combined: rules first, verified, agent only for the rest** (this branch) | 0.66 s | 11.9 s (2 Haiku agents, parallel: 7.7 s and 11.9 s) | 0.30 s | **≈ 13 s** | 2 of 8 |
| Agent extracts every invoice, code verifies (first version) | 0.43 s (convert only) | 47.0 s (1 agent, all 8, sequential) | 0.26 s | 55.7 s | 8 of 8 |
| Rule-based skill v1, agent orchestrates and fixes misreads | 0.7 s | 79.4 s | – | 79.4 s | 8 of 8 (orchestration) |
| Rule-based skill v2 (installer + run_pipeline), same | 0.7 s | 92.4 s | – | 92.4 s | 8 of 8 (orchestration) |

One-time reference data load, needed again only when a new timesheet or work order arrives:
`ts-load` 0.45 s, `wo-ingest` (8 work orders) 0.59 s.

In the combined run, the rules settled 6 of the 8 invoices. The other two went to an agent:
the scanned invoice, and one whose table wraps its cells, where the rule-based reading
produced numbers that are not on the invoice. The verification step caught that misread.

Measured from the orchestrator's side, including the time to launch the two agents, the agent
step took 25 s. Called directly from a script with an API key, that step would be a few seconds.

## Scale: 200 generated invoices (invented data)

200 invoices, 200 consultants and 200 work orders, all clean layouts:

| Step | Time |
| --- | --- |
| `ts-load` (200 contractors) | 0.73 s |
| `wo-ingest` (200 work orders) | 1.35 s |
| `fast` (convert + rule-based extraction + verification, 200 invoices) | 2.94 s |
| `decide` | 0.37 s |
| **Monthly run total (`fast` + `decide`)** | **3.3 s**, about 16 ms per invoice, 0 agent calls |

After aligning the verdicts with Pulkit's skill (expired work order = REVIEW_REQUIRED, hours x
rate compared with the subtotal), the October run gives exactly his skill's verdicts: 3 MATCH and
5 REVIEW_REQUIRED. The timings are unchanged: rules 0.79 s, decide 0.31 s.

## Tests

`python -m pytest` runs 62 tests in about 2.7 s, all on invented data:

- 32 tests for the decision rules and document handling, including subtotal vs total, the
  review policy and duplicate days
- 16 for the database, linking and lookups
- 14 for the combined pipeline, including end-to-end runs through the command line with real PDFs

The tests were checked by mutation: each of four safety checks was disabled in turn, and the
suite failed every time (2 or 3 tests each). The four checks:
- the misread gate
- "every number must appear on the invoice"
- "total = sum of the hours lines"
- work order dates
