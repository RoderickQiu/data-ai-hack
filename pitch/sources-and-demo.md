# Sources, evidence and demo preparation

Research date: September 11, 2026. This is a pitch about a prototype and a
proposed three-hour MVP, not an audited product comparison or investment model.

## Event requirements

`../SPEC.pdf`, pages 1–5:

- Official event: eight-hour build sprint, September 11, 2026.
- All five sponsor technologies must do meaningful, repeated work.
- Demonstrate structure, durable memory, analytical insight, action and replay.
- Show that repeated work becomes cheaper, more reliable or more effective.
- Snyk findings can reduce the score.

`../DESIGN.md`, sections 2 and 4–9: candidate-side job-search loop, verified
claims, confirmed preferences, deterministic prediction, workflow replay,
application packs and measured autonomy. The user requested a three-hour build
scope, so the pitch compresses scope rather than changing the event rules.

## Market products

These official pages support the capabilities summarized on slide 3. They do
not establish that competitors lack memory, learning or agentic workflows.

- [Simplify Copilot](https://help.simplify.jobs/articles/2415391-using-copilot-to-autofill-applications): application autofill, saved-answer reuse and tracking.
- [Simplify AI Talent Agent](https://simplify.jobs/ai-talent-agent): broader matching, tailoring and application workflow positioning.
- [Teal product overview](https://www.tealhq.com/how-it-works): tailored resumes, matching tools and a job tracker.
- [Jobright AI Job Matcher](https://jobright.ai/ai-job-match): profile and preference-based matching with match scores.

Our positioning is a hypothesis: expose a verifiable record of learned
preferences, recorded workflow reuse, and permission changes. A ten-person
pilot and paid active-search subscription are proposals, not traction or revenue.
No speculative market-size number is used.

## Observed project evidence

- `../data/job_snapshots/20260911_initial/summary.json`
- `../data/job_snapshots/20260911_initial/hotdata_result.json`
- `../data/job_snapshots/20260911_initial/final_verification.json`
- Hotdata destination: `jobs.public.tech_jobs_20260911`
- 5,187 public postings fetched from 20 boards, 2,321 selected unique roles from
  19 companies, three ATS families. Wealthsimple returned zero postings.
- 2,321 rows and 2,321 unique job IDs verified in Hotdata. Descriptions and URLs
  from each ATS family matched their source records in the readback sample.
- 85 offline backend tests passed via
  `python3 -m unittest discover -s tests -v` during deck preparation.

The chart uses these exact groups: Software 902, AI/ML 347, Data 178,
Infrastructure 197, Product/design 242, Other technical 455. Total: 2,321.
“Other technical” includes solutions/support, security, technical program,
hardware/robotics and technical writing. This is a selected corpus, not an
estimate of market size. Open means listed at collection time.

## Technical details for questions

**How does ranking improve without extra inference?**
`agent/predict.py` scores verified requirement coverage, retrieval rank and
confirmed preference fit using configured weights 0.45, 0.30 and 0.25. Small
bounded contextual adjustments are possible. This is a deterministic baseline,
not a trained model or demonstrated optimal weighting.

**What does prediction_accuracy mean in the code?**
`agent/metrics.py` computes recall on human-skipped items: correct skip
predictions divided by all actual human skips. The deck uses that meaning,
rather than calling it overall accuracy. A missing skip denominator stays null.

**Is the entire demo offline?**
No. The corpus is frozen so no job-board fetch is required on stage. Hosted
memory, query and orchestration services still require connectivity. The
assigned release schedule is disclosed and kept separate from real source dates.

**Does the HydraDB cloud key execute Cypher?**
The current code supports cloud graph persistence with in-process named graph
evaluation. The optional Bolt backend runs real Cypher. Name the deployed mode
and demonstrate persistence/reload; do not claim unverified cloud Cypher.

**Does a replay-index test prove Rote worked live?**
No. A real Rote capture/replay trace is a separate acceptance gate. Show an exact
replay, and a partial match only if the live implementation actually performed it.

**Does the agent submit applications?**
No. It prepares application packs from verified claims. The job seeker controls
submission and permission changes. Unknown facts still require an answer.

## Three-hour demo gates

1. Freeze the input and record one baseline with one approved candidate.
2. Verify Cognee extraction and HydraDB persistence/readback.
3. Verify Hotdata queries and one RocketRide-orchestrated action.
4. Record a successful Rote path and demonstrate its actual replay.
5. Collect real human keep/skip feedback and confirm one learned preference.
6. Capture run metrics. Label a tool-call/returned-byte cost proxy if token usage
   is unavailable. Do not interpolate or manufacture missing chart points.
7. Produce a Snyk dependency/code report and rehearse the 75–90 second demo.

The 3-hour plan is a proposed scope. A 6–10 run target is useful only if live
latency permits; a smaller verified record is preferable to invented history.

## Cover visual

`assets/memory-path.png` was generated with the built-in image-generation tool.
It is an abstract illustration, not a product screenshot or a record of a real
event. All slide titles, body text, tables and chart data remain editable.

Final prompt:

> Create a sophisticated abstract editorial visual for a startup pitch deck cover about a job-search agent that compounds memory across repeated runs. Wide 16:9 composition. Midnight charcoal background (#0D1918), generous completely quiet dark negative space on the LEFT 52% for editable slide title. On the RIGHT half, fine luminous seafoam and warm ivory filament strands, initially sparse and wandering near the lower right, gathering into a beautifully woven, smooth single curved path in the upper right. Tactile fiber-optic material, elegant flowing shape, subtle depth of field, soft studio light, restrained mint glow, no cyberpunk, no circuit-board cliches, no literal brain, no icons, no human figures, no diagrams, no writing or text or numbers, no logos, no watermark. Premium visual suitable for an evidence-driven hackathon pitch. The image is an abstract illustration, not a screenshot of a real product.
