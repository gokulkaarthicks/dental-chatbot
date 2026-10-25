---
kind: external_dependency
name: DeepSeek LLM (OpenAI-compatible chat API)
slug: deepseek
category: external_dependency
category_hints:
    - vendor_identity
    - sdk_real_api
scope:
    - '**'
---

### Identity
DeepSeek is the only external AI model provider used by this repository. It is configured via `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL` (default `deepseek-flash`), and `DEEPSEEK_BASE_URL=https://api.deepseek.com` in `.env`.

### Role
Provides natural-language interpretation and tool selection for the dental receptionist conversation; all scheduling, identity, authorization, and database writes remain deterministic Python/SQLite — the model cannot assert identity, invent policy, or confirm a booking directly.

### Integration shape
- Client lives in `backend/app/ai/deepseek.py` and talks to DeepSeek through its **OpenAI-compatible chat interface** (not the native DeepSeek SDK). This is the durable integration contract: swap base URL / model name without rewriting the call site.
- Conversation history (full chronological transcript) is sent on every turn; no summarization or automatic trimming is implemented.
- Timeouts and transient 5xx responses use bounded retries; continued failure returns a degraded fallback rather than claiming success.

### Verification
Confirm exact request/response shapes against the official DeepSeek OpenAI-compatible docs referenced in the assessment prompt.