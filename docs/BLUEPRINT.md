# AI Support Triage — Production Blueprint

> **Status:** originally written when the repo was empty. `master` now contains an early
> agent tool-execution safety layer (no triage code yet) — see
> [CODE_REVIEW.md](./CODE_REVIEW.md) for the review of that code and how it fits this
> design. This document remains the target architecture, written to be executed
> top-to-bottom.

---

## 1. Project overview

**Intent:** An AI support triage platform that ingests support tickets from any channel
(email, web form, chat, API), then automatically:

1. **Classifies** the ticket (category, product area, intent).
2. **Detects urgency/priority** (SLA tier, sentiment, churn/security risk signals).
3. **Routes** it to the right team, queue, or automated workflow.
4. **Drafts a response** or next-step suggestion, grounded in the knowledge base and past
   resolved tickets, with citations.
5. **Escalates to humans** whenever confidence is low, the customer is high-risk, or the
   topic is on a "never automate" list (billing disputes, legal, security incidents).

**What exists today:** nothing. The repository has no code. Everything below is the plan to
get from zero to production.

---

## 2. Current gaps

The single biggest gap: **there is no codebase.** Concretely, the following must be built
from scratch — listed here because each one is a gap a naive prototype would also have:

- **No ingestion layer** — no webhook receivers, no email parsing, no channel adapters, no
  dedupe of re-sent/forwarded tickets.
- **No data model** — tickets, classifications, routing decisions, drafts, human reviews,
  and feedback need durable, auditable storage from day one.
- **No schema-constrained LLM outputs** — free-text classification is the #1 source of
  silent triage bugs; outputs must be validated Pydantic/JSON-Schema objects.
- **No confidence scoring or fallback path** — without calibrated confidence + a human
  queue, misclassifications ship straight to customers.
- **No retrieval layer** — response drafting without RAG over the KB and resolved tickets
  guarantees hallucinated answers.
- **No hallucination controls** — no citation requirement, no groundedness check, no
  "never state a refund/credit/policy amount not present in a retrieved doc" rule.
- **No human-in-the-loop workflow** — no review queue, no approve/edit/reject capture, so
  no learning signal.
- **No evaluation harness** — no golden dataset, no prompt regression tests, no way to know
  if a prompt or model change made triage better or worse.
- **No observability** — no tracing of LLM calls, token cost, latency, or per-stage failure
  rates.
- **No security/compliance posture** — support tickets are dense with PII; redaction,
  retention, and access control must be designed in, not bolted on.
- **No async/event-driven backbone** — synchronous LLM calls in a request handler will fall
  over at real ticket volume and make retries/ordering unmanageable.

---

## 3. Best improvements (prioritized build order)

1. **Ticket data model + ingestion API** (FastAPI + Postgres). Everything hangs off this.
2. **Schema-constrained classification pipeline** — one LLM call returning a validated
   object: `{category, subcategory, intent, urgency, sentiment, language, confidence,
   reasoning}`. Use Claude structured outputs / tool-use with a Pydantic schema.
3. **Confidence-gated routing** — deterministic rules on top of the classification, with a
   threshold below which tickets go to a human triage queue instead of auto-routing.
4. **Golden dataset + eval harness before scaling prompts** — 200–500 labeled tickets,
   promptfoo or pytest-based evals, run in CI. This is what separates a demo from a product.
5. **RAG-grounded response drafting with citations** — pgvector + reranking; drafts are
   suggestions for agents first, auto-send only later and only for whitelisted intents.
6. **Human review queue UI** — approve/edit/reject with reasons; every action stored as
   training/eval data.
7. **Observability** — Langfuse (or OpenTelemetry + a tracing backend) on every LLM call:
   prompt version, model, tokens, cost, latency, outcome.
8. **Async pipeline** — move classification/drafting to a worker queue (Redis + arq/Celery,
   or Postgres-backed like Procrastinate) so ingestion is never blocked by model latency.
9. **PII redaction + retention policy** — Presidio-based scrubbing before anything is
   embedded or logged.
10. **Model tiering for cost** — Haiku-class model for classification, Sonnet-class for
    drafting, escalate to a frontier model only on low confidence or complex tickets.

---

## 4. Feature roadmap

### Immediate (weeks 1–3) — the credible core
- FastAPI service: `POST /tickets` (ingest), `GET /tickets/{id}` (status + triage result).
- Postgres schema: `tickets`, `classifications`, `routing_decisions`, `drafts`,
  `human_reviews`, `events` (append-only audit log).
- Classification chain: normalize → redact PII → classify (structured output) → validate →
  confidence gate → route or enqueue-for-human.
- Deterministic routing table (YAML/DB): category × urgency → team/queue, with override
  rules (VIP customer, security keywords → always human).
- Golden dataset v1 (seed with a public dataset like Kaggle customer-support tickets or
  synthetic tickets, then replace with real ones) + eval harness in CI.
- Langfuse tracing wired into every model call.

### Short term (months 1–3) — the differentiators
- RAG drafting: ingest KB articles + resolved tickets into pgvector; hybrid search
  (BM25 + vector) → rerank (Cohere Rerank or a cross-encoder) → draft with mandatory
  citations → groundedness self-check pass before surfacing.
- Human review dashboard (Next.js): triage queue sorted by urgency×confidence, one-click
  approve/edit/reject, inline diff of agent edits vs. AI draft.
- Duplicate/near-duplicate detection via embedding similarity at ingest (link tickets,
  detect incident spikes: "34 tickets in 20 min matching 'checkout failing' → page on-call").
- Channel adapters: email (inbound parse), Zendesk/Intercom/Front webhooks, Slack.
- SLA engine: per-tier clocks, breach prediction, auto-escalation before breach.
- Feedback loop: human corrections automatically become eval cases; weekly eval report.
- Streaming draft generation in the agent UI (SSE) so agents see drafts in <1s TTFT.

### Long term (months 3–6+) — the moat
- Auto-resolution for whitelisted intents (password reset, invoice copy, plan questions)
  with agentic tool calling (look up order status, check subscription state) and hard
  guardrails + one-click customer escape hatch ("talk to a human").
- Fine-tuned small classifier (distill the LLM classifier onto a fine-tuned Haiku or an
  open model like a fine-tuned ModernBERT) for near-zero-cost, low-latency first-pass
  classification; LLM only for low-confidence tail.
- Analytics product: emerging-issue clustering, deflection rate, cost-per-ticket, agent
  assist adoption, misroute rate — this is what support leaders buy.
- Multi-tenant SaaS hardening: per-tenant encryption, data residency, SOC 2 controls,
  audit exports.
- Active-learning loop: uncertainty sampling picks tickets for human labeling to grow the
  golden set where the model is weakest.

---

## 5. AI/ML architecture

### Pipeline (event-driven, multi-stage)

```
Channel webhooks ─▶ Ingest API ─▶ [queue] ─▶ Normalize + language detect
                                              │
                                              ▶ PII redaction (Presidio)
                                              │
                                              ▶ Dedupe (embedding sim > 0.92 → link)
                                              │
                                              ▶ Stage 1: Classify (Haiku, structured output)
                                              │     conf ≥ τ_class ──▶ Route (deterministic table)
                                              │     conf <  τ_class ─▶ Stage 2: Re-classify (Sonnet,
                                              │                        w/ top-k similar resolved tickets
                                              │                        as few-shot context)
                                              │     still low ───────▶ Human triage queue
                                              │
                                              ▶ Stage 3: Draft (Sonnet + RAG)
                                              │     retrieve (hybrid) → rerank → generate w/ citations
                                              │     → groundedness check (LLM-as-judge, cheap model)
                                              │     → policy lint (regex/rules: no promises, no amounts)
                                              │
                                              ▶ conf ≥ τ_draft AND intent ∈ whitelist ─▶ auto-send
                                                else ─▶ agent-assist draft in review UI
```

### Model strategy
- **Classification:** `claude-haiku-4-5` with tool-use-forced structured output. ~$0.001–
  0.003/ticket. Escalate <τ to `claude-sonnet-5`.
- **Drafting:** `claude-sonnet-5` with retrieved context; prompt caching for the static
  system prompt + policy block (large cost saver at volume).
- **Judging/groundedness:** Haiku-class judge with a binary rubric ("every factual claim in
  the draft is supported by a cited passage: yes/no + failing claims").
- **Fine-tuning vs. prompting:** start prompt-only. Fine-tune only when (a) you have 5k+
  human-corrected labels and (b) classification cost/latency is a proven bottleneck.
  Fine-tuning the *drafter* is rarely worth it; fine-tuning/distilling the *classifier* is.

### Confidence logic (do not trust raw self-reported confidence)
- Ask the model for a confidence label, but **calibrate** it: on the golden set, bucket
  predictions by stated confidence and measure actual accuracy per bucket; map stated →
  empirical. Recalibrate on every prompt/model change.
- Signals combined into the gate: stated confidence, agreement between Stage-1 and Stage-2
  classifiers when both ran, margin between top-2 category scores, retrieval score for
  drafting, and hard business rules (VIP, security terms, legal terms → always human).
- Thresholds are per-category (auto-routing "billing" wrong is worse than "how-to").

### Retrieval strategy
- **Corpus:** KB articles (chunked ~500 tokens with headers preserved), resolved tickets
  (question + accepted answer pairs), macros/canned responses, product changelogs.
- **Hybrid search:** Postgres FTS (BM25-ish) + pgvector cosine, reciprocal-rank fusion,
  then cross-encoder rerank to top 4–6 passages.
- **Embeddings:** voyage-3 / text-embedding-3-large class; store model+version per row so
  re-embedding is a migration, not a mystery.
- **Freshness:** KB sync job with content hashing; stale-doc detection (article cited in
  drafts that agents keep editing → flag for KB team).

### Evaluation setup
- **Golden set:** stratified by category/urgency/language, versioned in the repo (redacted).
- **Classification evals:** accuracy, macro-F1, per-class confusion, calibration curve.
- **Routing evals:** misroute rate on golden set + shadow-mode comparison vs. human triage.
- **Draft evals:** LLM-as-judge rubric (groundedness, tone, resolution-likelihood) +
  citation-precision check (every citation actually supports the sentence) + human spot
  audits weekly.
- **CI gate:** prompt or model changes run the eval suite; regressions beyond tolerance
  block merge. Track eval scores over time in Langfuse/Braintrust.
- **Online:** log agent edit-distance on drafts, approve/reject rates, customer reopens —
  these are the real metrics.

---

## 6. Modern tools and libraries

| Layer | Recommendation | Why |
|---|---|---|
| Backend | **Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy/Alembic** | Async-native, schema-first, boring in the good way |
| Queue/async | **Redis + arq** (simple) or **Temporal** (if workflows get long-running/retry-heavy) | Ticket pipelines are multi-step with retries; don't hand-roll |
| DB | **Postgres 16 + pgvector** | One database for OLTP, FTS, and vectors until well past 1M tickets |
| LLM SDK | **anthropic** SDK directly + **Instructor** (or native structured outputs) | Direct SDK + Pydantic validation beats heavyweight frameworks for a pipeline this shaped |
| Guardrails | Pydantic validators + custom policy lints; **Presidio** for PII; optional **NeMo Guardrails/LLM Guard** for input scanning | Validation layers you can unit test |
| Observability | **Langfuse** (LLM tracing, prompt versions, cost) + **OpenTelemetry** + Prometheus/Grafana for the service itself | Separate LLM observability from infra observability |
| Evals | **promptfoo** or **Braintrust**; pytest for pipeline tests; **deepeval** if you want RAG metrics off the shelf | Evals in CI are non-negotiable |
| Frontend | **Next.js 15 + TypeScript + shadcn/ui + TanStack Query**; SSE for streaming drafts | Fast to build a credible review dashboard |
| Infra | Docker Compose for dev; Fly.io/Railway/ECS for prod; GitHub Actions CI running lint + tests + evals | Keep it deployable by one person |

Avoid: LangChain-style mega-frameworks for the core pipeline (this system is a pipeline
with 3 model calls, not a free-form agent), building a separate vector DB service before
pgvector is actually a bottleneck, and fine-tuning before evals exist.

---

## 7. Real-world value

- **Slow first response** → instant classification + agent-assist drafts cut first-response
  time from hours to minutes; auto-resolution handles the top repetitive intents entirely.
- **Misrouted tickets** → consistent, evaluated routing beats tired-human triage; every
  misroute is logged and becomes an eval case.
- **Poor prioritization** → urgency detection + SLA breach prediction surfaces the
  angry-enterprise-customer-about-to-churn ticket above the 40 password resets ahead of it.
- **Duplicate storms** → embedding-based clustering turns 200 identical outage tickets into
  one incident with one status page update, instead of 200 handled conversations.
- **Repetitive first-response work / burnout** → agents review and edit instead of writing
  from scratch; measured by draft edit-distance trending down.
- **KB inefficiency** → citation analytics show which articles resolve tickets and which
  get edited around — a feedback loop most KB teams have never had.
- **Missed SLAs** → per-tier clocks with predictive escalation, not after-the-fact reports.

Support automation is a proven budget line (Zendesk AI, Intercom Fin, Forethought,
Decagon). A well-instrumented open implementation with real evals and HITL is both a strong
portfolio piece and a viable internal tool for any 5–50-agent support org.

---

## 8. Final recommendation

Build in this exact order, and resist reordering:

1. **Week 1:** FastAPI + Postgres ticket model + `POST /tickets`; classification with
   schema-constrained Haiku output; deterministic routing table; Langfuse tracing.
2. **Week 2:** Golden dataset (start synthetic/public, ~300 tickets) + promptfoo eval suite
   in CI; confidence calibration; human triage queue (even a bare table view).
3. **Week 3:** pgvector + KB ingestion + RAG drafting with citations + groundedness judge;
   drafts go to the review UI, never auto-send yet.
4. **Then:** channel adapters, dedupe/incident clustering, SLA engine, streaming UI,
   auto-send for whitelisted intents behind the confidence gate.

The differentiator to showcase is not "an LLM reads tickets" — it's the **eval harness,
calibrated confidence gating, and human-in-the-loop feedback loop**. That triad is what
production AI systems have and demos don't.
