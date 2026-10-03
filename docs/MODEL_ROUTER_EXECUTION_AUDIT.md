# ModelRouter & LLM Execution Audit

**Scope (Work 15.8):** the exact execution flow of every path that can reach a
billable LLM, documented *before* any change, so the change can be judged
against what was actually there.

**Method:** every claim below was verified by reading the code and, where it
mattered, by executing it. Line numbers are from the pre-Work-15.8 tree.

## Executive summary — three findings that change the work

1. **`ModelRouter.complete` has ZERO production callers.** It is exported
   (`intelligence/__init__.py:25`) and exercised by tests, but no agent, route
   or pipeline calls it. Its two defects are therefore *latent, not live*.
2. **The real multiplication is in `providers/llm.py`, not the router.**
   Work 15.7 cut `complete()` to one POST per ambiguous outcome. What remains is
   *multiplier* multiplication at the callers: loops over subtopics, batches,
   script variants and platforms.
3. **The DecisionEngine's router integration is a silent no-op.**
   `providers/llm_provider.py:54` calls `ModelRouter(self.workspace_id)`, but
   `ModelRouter.__init__` (`router.py:258`) takes a *registry*. A string lands
   in `self.registry`, `.route()` raises `AttributeError`, the `except` at
   `:65` swallows it, and `_router_model()` always returns `""`.

Finding 3 means "the router picks the model" has never actually happened.

## Entry points and maximum paid POSTs

Multipliers marked **×N** are loops in the *caller*, not in the provider.

| Entry point | Tier | POSTs/call | Multiplier | Max | Budget gate |
|---|---|---|---|---|---|
| `agents/creation.py:112` research | cheap | 2–4 | ×5 subtopics (`longform/stages_early.py:79`) | **30** | no |
| `agents/creation.py:429` write_script | reasoning | 1–2 | ×3 variants, ×2 on retry (`:522`) | **12** | no |
| `longform/stages_early.py:53` `llm_json_or_none` | default | 3–6 | 2 sites | **12** | no |
| `agents/creation.py:258` strategize | reasoning | 3–6 | 1 | 6 | no |
| `agents/creation.py:351` plan_from_reference | reasoning | 3–6 | 1 | 6 | no |
| `agents/production.py:767` `_llm_quality` | verification | 3–6 | 1 | 6 | no |
| `agents/distribution.py:111` SEO | cheap | 3–6 | 1 (batched) | 6 | no |
| `providers/broll.py:890` scene prompts | cheap | 3–6 | 1 | 6 | no |
| `providers/clips.py:352` moment rank | cheap | 3–6 | 1 | 6 | no |
| `community/draft.py:258` `_llm_reply` | cheap | 1–2 | ×2 | 4 | no |
| `intelligence/providers/llm_provider.py:102` | cheap | 3–6 | 1 | 6 | no |
| `providers/dubbing.py:243` translate | cheap | 3–6 | **×ceil(N/20)** batches | 6·ceil(N/20) | no |
| `agents/discovery.py:244` | default | 3–6 | **dead — no callers** | 0 | — |
| `router.py:522` | all tiers | 24 (6×4) | **dead — tests only** | 0 | — |

Realistic worst cases: **research 30**, **script variants 12**, **localization
3·ceil(N/20)·languages** (and `localization/pipeline.py:351` passes
`workspace_id=""`, so its cost lands on no workspace at all).

`providers/llm.py` POST count per `complete()`: **1** when
`llm_fallback_model == ""` (the default, `config.py:64`), **2** with it.
`complete_json` sets `json_mode=True`, doubling to **2/4**. `run_completion`
advances only on `verdict_for(record) is RETRY` (`llm_paid.py:701`), so an
ambiguous leg raises before the re-ask.

## Failure taxonomy

`router.complete` catches bare `Exception` at `router.py:534` and advances.
What actually arrives there:

| Condition | Origin | Safe to fall through? |
|---|---|---|
| provider outage / connect failure | `llm.py` | **yes** — nothing was delivered |
| 4xx known rejection | `llm.py:135` | yes, but see the note below |
| **ambiguous paid submission** | `llm_paid.py:343` `LLMCompletionUnknown` | **NO — must stop** |
| billed-but-unusable 2xx | `llm_paid.py:362` `LLMCompletionUnusable` | **NO — already billed** |
| "provider not configured" | `llm.py:135` | yes, but it repeats across every tier |
| semantic / quality downgrade | — | **not an exception**; there is no reasoning-based tier downgrade anywhere. The tier walk is purely failure-driven. |
| local model unavailable | — | **cannot happen today**: `LOCAL_ONLY` still POSTs (see below), so "local unavailable" surfaces as a *remote* 4xx/5xx. |

Note on 4xx: a permanent 4xx (e.g. a bad model name) makes the loop walk the
whole chain, repeating the identical failure once per tier. That wastes
latency and, on a metered provider, may cost.

`LLMCompletionUnknown` subclasses `AmbiguousSubmission` → `PaidJobError` →
`Exception`, so the bare `except` treats a possibly-billed request exactly like
a transient outage. That is the defect Work 15.8 closes.

## Local vs remote routing

`REMOTE_TIERS = ("FAST","BALANCED","HIGH_QUALITY","PREMIUM")`,
`LOCAL_TIERS = ("LOCAL_ONLY","PRIVATE")` (`router.py:31-32`).

`_resolve_model` returns the **literal string `"local"`** for a local tier
(`router.py:424`), else `""`. `router.complete` then passes
`model=model or None` (`:526`); `"local"` is truthy, so it survives to
`llm.py:169`, which POSTs `{"model": "local"}` to
`{base_url}/chat/completions` — default `https://api.openai.com/v1`.

**The gate at `router.py:516` skips a tier when `entry.remote` is false.** So a
local tier is skipped *because it is local* — which is exactly the tier that
would have emitted `"local"`. The two conditions are independent, and the
combination is what makes the leak reachable:

- `route()` appends both local tiers as last-resort fallbacks (`router.py:292,352`)
- after four remote tiers fail, the chain reaches `LOCAL_ONLY`
- that entry has `remote=False`, so the privacy gate skips it — but the
  `continue` happens *before* `_resolve_model`, so in the current code the
  leak does not fire.

The reachable variants are:

1. A **registry entry whose `remote` is True but whose tier is a local tier**
   (possible via workspace capability overrides) → gate passes → `"local"` is
   POSTed to a remote gateway. This defeats `privacy_mode: private` and
   `remote_allowed: false` entirely.
2. **Workspace override `intelligence.models.local_only = "<remote-model>"`**
   (accepted at `router.py:66-70`) is returned at `:393-395` *before* any
   locality check → a remote model name is POSTed from a local-only tier.

3. `_resolve_model` returning `""` for a remote tier → `model or None` →
   `None` → `llm.py:144` substitutes `gpt-4o-mini`. **The configured cost tier
   (PREMIUM is 8.0× baseline) is silently discarded rather than failing.**

## Budget gates

**No LLM path is budget-gated.** `llm_paid._executor` builds a
`PaidProviderExecutor` without `submit_budget`, and
`paid_executor.check_budget` short-circuits to a debug log when it is `None`.

Every gated sibling for contrast: `images.py:189`, `broll.py:171`,
`avatar.py:149`, `tts.py:945`, `lipsync/external.py:138`,
`ugc/pipeline.py:638`. `production.py:155` gates the *render engine*, not the
LLM.

## Dubbing / translation

`providers/dubbing.py:239` loops `range(0, len(texts), 20)`, calling
`complete_json` per batch (`:243`).

- POSTs per run: `ceil(N/20) × (2–4)` plus a re-ask on unparseable output.
- **Ambiguous spends: `ceil(N/20) × 2`** worst case.
- **Zero budget gate.** Reached from `agents/dubbing.py:68`,
  `api/v1/content.py:1808`, `localization/pipeline.py:86`.
- Accounting must model the *request*, not the segment: the provider receives
  one combined request per batch, so a per-segment reservation would
  over-reserve 20×.

## What Work 15.8 changes, and what it deliberately does not

| Defect | Treatment |
|---|---|
| `router.complete` swallows ambiguity | stop the chain on `SUBMISSION_UNKNOWN`; fall through only on a proven-safe class |
| unbounded chain × paid tiers | `FallbackPolicy` with `max_paid_attempts`, default conservative; an explicit, audited override permits more |
| `model="local"` reaching a gateway | an explicit execution target (`LOCAL`/`REMOTE`/`AUTO`); `"local"` can never be sent to a remote provider |
| no LLM budget gate | gate every paid leg before the request |
| dubbing has no gate | reserve per **request** (batch), not per segment |
| silent router no-op | correct the constructor call; the router then actually routes |

Fallback semantics that are intentional and must survive: provider outage,
4xx rejection, and connect failure all still fall through. Semantic/quality
degradation remains a *routing choice*, not an exception path.
