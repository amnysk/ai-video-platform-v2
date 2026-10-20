# JP-MAS-003 — Multi-Channel Agent Studio, Model Registry, Creative Intelligence

Status: Proposed / design only (2026-10-20). Base: claude/agent-tools at 2fb3b41e. **No runtime deployment or feature activation.**

## Goal
Allow a platform operator to configure each channel's agents, prompts, model selections and production hypothesis via UI; inspect the execution evidence and reuse vetted creative lessons across channels. Maintain PostgreSQL as source of truth; preserve Temporal replay, existing immutable artifacts, private-only upload, expense gates and channel-local privacy.

## Scope and architecture
- Shared core: Temporal, FastAPI, PostgreSQL, MinIO, OpenSearch, existing application operation catalog, ToolGateway, intervention commands.
- Channels: canonical channels and scoped episodes, plans, analytics, hypotheses, artifacts, reservations, audit events. Every read/write resolves authenticated actor's channel grant server-side. Backfill existing historical rows to Japan's Past; never create unscoped rows.
- Registry: **code-registered handler/schema/tool definitions**, versus **database-stored configuration revisions** (editable prompts, approved model binding, conservative bounds). Published config has immutable version + SHA-256 hash. Every episode pins channel, config_version, compiled workflow plan, prompt text hash, model ID and provider adapter version. Do not mutate existing AgentSpec manifests, Template registrations, or pinned episode artifacts.
- Operator UI: channel switcher, Agent Studio, Model Registry, Hypothesis & Episode Inspector, Knowledge Hub, Budget/Schedule/OAuth settings. First iteration extends current static ES-module /ui and application service; only later evaluate a frontend migration. Never place domain policy in UI.
- Execution: server compiles a proposed channel config against registered handler schema, tool allowlist, feature flags, cost gate, approved provider and tenant grants. Publish is human-only, revision-checked, audited, and future-episode-only. No arbitrary Python imports or code from DB.
- Prompt editor: separate immutable platform guardrails from editable task instructions. A preview produces a **dry-run** with fake/mock provider, no paid calls. Paid evaluations require separate human authorization, bounded budget and reservation records.
- Models: provider adapters isolated behind existing AgentLlm port. Registry entries contain provider, exact model ID, capability flags (structured output/tools/vision), explicit credentials reference, pricing-source and currency, lifecycle status (discovered/validated/approved/retired), adapter version, validation evidence. Discovery is not activation; missing prices are unknown, not zero. Never allow arbitrary URL or SSRF. No dynamic imports.
- Hypotheses: pre-register target audience, independent variable (e.g. opening question), predicted mechanism, quantitative success criterion, comparator, sample/exposure constraints, creation time and scene-function timeline (hook/context/reveal/payoff). Capture prompt and model refs and agent's inspectable reason/evidence summary; do not expose hidden chain-of-thought.
- Learning: immutable measurement windows 48h/7d/28d; preserve unavailable vs zero, public vs private, Shorts vs long-form, exposure/time confounders, alternative explanations, sample limitations. Label findings supported/unsupported/inconclusive; observations do not establish causality. Reuse existing CreativeBrief, StrategyBrief, Lesson and DecisionRecord rather than duplicating artifacts.
- Knowledge Hub: source channel owns all raw analytics, prompts and media. New shareable Creative Insight is an **explicitly approved, sanitized creative pattern** with source provenance, generalized wording, approval and revocation metadata. No cross-channel raw YouTube Analytics aggregation by default. A destination channel adoption is a separate experiment, not proof of portability. Review YouTube API policies before any cross-owner metrics flow.
- Observability: event name, channel_id, episode_id, configuration version, immutable prompt hash, provider reference, run ID, cost_status, tool invocation IDs; never secrets or full sensitive content. OpenSearch remains projection; PostgreSQL stores audit as truth.

## Default Japan's Past recommendation
Machine-readable draft preset: `config/presets/japans-past-agent-studio-v1.json`. It is **not** an active config. Target: American English, US ages 20-29 (assumed until validated by Analytics), one vertical Short/day at 06:00 Asia/Tokyo, private only, skip overlapping runs, 60-minute catchup, max one successful publication/day. Historian fact-check with citation/uncertainty, strong opening question and payoff, explicit hypothesis & success comparator, late learning when public metrics valid, 7/28 day comparisons, shared insight opt-in. Monthly fal ceiling JPY 30,000 globally; one channel initially (no fixed channel split until others exist). Local models can be trialed but never silently replace production models. No unmeasured spend treated as zero. Human review of prompt/model promotions.

## OSS decisions
1. **Keep Temporal + custom Agent Registry + ToolGateway**: already enforce replay, reservations, immutable manifests; replacing with LangGraph, CrewAI or AutoGen adds overlapping orchestrators and unpredictable state.
2. **Promptfoo**: primary candidate for offline prompt/model regression and golden-set comparisons, as isolated CI/developer tool, no actual paid provider calls in normal CI. Confirm current release/license and security review before pin.
3. **LiteLLM**: candidate as an optional provider adapter for OpenAI, Anthropic, Gemini, Ollama. Start with isolated SDK proof of concept, not an exposed production Proxy. Enforce allowlist, structured JSON behavior, per-model pricing, timeout, logging redaction and tool compatibility. Review version/license/security before adoption.
4. **Ollama**: optional local model provider for experiments, CPU/GPU capacity constrained; benchmark accuracy vs cloud models on English hooks and Japanese history grounding. Do not make it production default.
5. **Phoenix**: optional specialized traces/evals experiment only if existing OpenSearch and DB inspector inadequate; assess Elastic License 2.0 service restrictions.
6. **Langfuse**: defer due extra ClickHouse/Redis operational stack and overlapping tracing, inspect precise self-host version/license if revisited.
7. **MLflow**: defer until experiment inventory & model registry exceed native tables' need; avoid a second prompt registry source of truth.

## Proposed API (application operations, not yet implemented)
- GET /v1/channels/{channel_id}/agents
- PUT /v1/channels/{channel_id}/agents/{role}/draft (optimistic expected_version)
- POST /v1/channels/{channel_id}/agents/{role}/validate
- POST /v1/channels/{channel_id}/agents/{role}/publish (human-only, audited)
- GET/POST /v1/model-providers and /v1/models
- POST /v1/models/{id}/validate (dry-run first)
- GET /v1/episodes/{episode_id}/intelligence, /execution-trace
- GET/POST /v1/insights, POST /v1/insights/{id}/approve, /adopt

## Rollout and acceptance
P0: accepted ADR / contract/invariant tests for channels, immutable config and per-tenant access. P1: migrate/backfill existing channel, guard existing APIs, implement agent config read-model & draft without activating new routes. P2: admin-only publish UI, fake model registry/validation and live provider adapters behind flag. P3: hypothesis/scene-function capture + inspector. P4: consented sanitized knowledge sharing, lesson adoption. P5: optional OSS evaluator, only after isolated comparison. Cover stale version rejection, cross-channel IDOR, OAuth channel mismatch, prompt-injection content, content-policy refusal, private-only upload, double charge/replay, unknown external spend, rollback and private Analytics suppression. Never enable production or spend money without owner approval.
