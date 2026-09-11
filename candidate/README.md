# Candidate fixtures

The two seed documents the agent starts from. Both are prose on purpose: Cognee extracts the
typed entities (DESIGN.md §6), so anything pre-structured here skips the step we are proving.

| File | Becomes | Used by |
| --- | --- | --- |
| `resume.example.md` | one `Claim {status: verified, source_doc}` per bullet | apply-pack; the `claim_id` validator (P5) |
| `preferences.example.md` | `Preference` rules | `predict_fit`, digest ranking (G3) |

Both are sized against `jobs.public.tech_jobs_release` (1,886 US/Canada jobs, 19 companies).
A persona that does not intersect the corpus produces a run with no keeps, and with no keeps
there are no signals, no inferred rules, and nothing for the compounding chart to show.

## The preferences are deliberately incomplete

Seeded rules narrow 1,886 jobs to **345**. That is the right size: small enough that ranking
matters, large enough that the human keeps some and skips some.

**One rule is left out so the agent can infer it** (P2 — "after 3 rejections sharing a reason,
propose a rule; confirming it visibly reorders the next digest"):

> **prefers companies under about 2,000 people** — 296 of the 345 seeded matches are Databricks,
> OpenAI, Anthropic, Stripe, Cloudflare, Reddit and the like. Repeated skips of those cluster on
> a reason no column carries, and confirming the rule takes the shortlist **345 → 57**.

Company size is not a field in the corpus. The rule has to come from the human confirming a
pattern in their own skips, which is the point. If you add it to `preferences.example.md`, the
agent has nothing left to learn.

A second latent rule would be nice and this corpus does not really support one. Measured inside
the 345: adtech titles are 8 rows, customer-facing titles 14, descriptions mentioning on-call 28,
clearance 7. None reorders a shortlist visibly. Do not pad the persona with a rule that cannot
pay off on screen — one confirmed rule that moves 345 to 57 is the stronger demo.

## The resume is deliberately incomplete too

`resume.example.md` covers evaluation tooling, Terraform, batch infrastructure, retrieval
serving, data contracts and mentoring. Against the 404 ML and infrastructure jobs in the corpus:

- **151** ask for evaluation work — the candidate's strongest claim
- **34** ask for Terraform — covered
- **75** ask for Kubernetes — the cluster bullet says "Kubernetes-free" on purpose
- **73** ask for CUDA, GPUs, PyTorch or distributed training — no claim covers this

Those last two are the gaps an apply-pack must **show rather than paper over** (P5). Removing
them makes every pack look complete and removes the proof.

## Replacing the persona

`.example.md` is the committed fixture. Drop a real `resume.md` and `preferences.md` beside
them and point the loader at those; the examples stay as the offline test input.

If you change the target companies, retarget `WAVES` in `scripts/build-release-table.py` to
match and rebuild — the hiring-wave insight is worthless aimed at companies the candidate does
not track. It currently fires for Perplexity, Ramp and Replit, all of which sit inside the
stated preferences and average under two roles a day, so a six-role day is a real spike.
