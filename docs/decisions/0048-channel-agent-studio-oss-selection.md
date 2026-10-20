# ADR-0048 — Channel Agent Studio & OSS selection (Proposed)

2026-10-20. **Proposed, not Accepted.** Research + recommended *inactive* draft only; no production migration, new runtime dependency, credentials, or feature flags enabled.

Context: ADR-0043 and INV-51 require immutable AgentSpec/Template manifest hashes; ADR-0044/45 provide application API and static UI with token auth; ADR-0047 Tool Gateway supports bounded tools. Today ModelPolicy providers are only fake and codex_cli. Daily legacy path must remain replay-safe.

Decision proposed:
- Maintain owned application-level config, model catalog and creative intelligence records in PG with strict channel authorization and immutable revisions.
- Preserve code-managed handler registration/schema/tool allowlists; do not store executable handlers or arbitrary URLs in DB.
- Expose allowed prompt/model selection to channel admins; public version affects **new** episodes only, with pin/hash and audit.
- Start OSS with a separate Promptfoo **offline eval** trial; LiteLLM **SDK adapter** proof of concept, optional Ollama for local comparison. No adoption of LangGraph/CrewAI replacing Temporal.
- Do not deploy Langfuse self-hosting yet; Phoenix only if current DB + OpenSearch traces insufficient. MLflow later only if experiment scale justifies it.
- Check precise pinned versions' licenses, security advisories, model support and resource requirements at implementation time, especially any API aggregation/redistribution rights and YouTube Analytics terms.

Reject: immediately replacing orchestration, dynamically loading third-party tools from user configuration, direct UI→provider calls, secret leakage into logs, cross-channel raw Analytics aggregation, automatic public uploads, enabling agentic tool workflows in production with unverified Research provider, assigning measured cost zero when unknown.

Mandatory tests before implementation merge: immutable hashes for legacy versions, Temporal replay, invalid/unauthorized cross-channel access, optimistic concurrency, model capability mismatch, fake-only default, no paid calls in CI, prompt injection / sanitization, OAuth wrong-channel upload rejection, budget unknown blocking, consent and revocation of shared insights.

See full design: docs/design/JP-MAS-003-agent-studio-knowledge-hub.md; data: config/presets/japans-past-agent-studio-v1.json.
