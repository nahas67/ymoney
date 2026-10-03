# YMONEY Paid Provider Matrix

**Scope (Work 15.7, completed in Work 15.9 §9):** every external operation in
this repository that can incur real cost, and what protects it.

The authoritative, machine-checked version of this table is
`backend/app/services/paid_jobs_audit.py` (`PAID_PATHS`). This document
explains it; the audit **enforces** it. A test asserts the audit reports no
disagreement with the source, and another asserts the totals below are the
computed ones, so this prose cannot drift far from the code without a failure.

## The invariant

```
billable submit
  -> was acceptance definitely known?
       YES                -> continue/poll the SAME remote job
       DEFINITELY REJECTED -> the caller's safe-retry policy applies
       UNKNOWN            -> SUBMISSION_UNKNOWN  ->  DO NOT RESUBMIT
```

Implemented once in `app/services/paid_executor.py`, and the *money* around it
(reservation, attempt record, timeout classification, execution outcome, cost
outcome, remote-id persistence, audit event) once in
`app/services/paid_provider.py`. A provider keeps its own request building,
polling, response parsing, cancellation and error semantics; it does not
re-derive any of the above.

## Current totals

| Measure | Count |
|---|---|
| Audited operations | 46 |
| Billable | 31 |
| Not billable (local CPU / free reads) | 15 |
| Billable and covered | **31** |
| Billable and uncovered | **0** |
| `verify_against_source()` disagreements | **0** |
| Discovered money markers with no row | **0** |

**What "covered" means here, precisely.** It is a *safety classification*: the
behaviour on every exit is decided in code, named, and pinned by a test. It is
**not** a claim that a submission can be reconciled afterwards, because for a
chat completion it cannot be and never will be. `llm.complete` is COVERED *and*
UNRECONCILABLE, and both halves of that sentence are true at once — the row
says so rather than collapsing them.

Idempotency: **no vendor YMONEY bills publishes an idempotency header.** Every
row is `IDEMPOTENCY_UNSUPPORTED` or `IDEMPOTENCY_UNVERIFIED`, verified against
vendor documentation rather than assumed. Inventing a header would waste a
round trip and teach a reader that it means something. YMONEY keeps a local
`idempotency_key` for correlation only, and never sends it as a guarantee.

## Billable operations

| Operation | Component | Remote job id | Timeout semantics | Retry behaviour | Status |
|---|---|---|---|---|---|
| `video_engine.mpt.submit` | MPT render engine | yes (`data.task_id`) | POST 60s; read timeout → ambiguous | **never** resubmits; poll/reconcile only | COVERED |
| `video_engine.ffmpeg_avatar.submit` | composite: paid TTS + ≤8 paid images | inherited from leaves | inherited | inherited | COVERED (composite) |
| `images.xkiro.generate` | xKiro | yes (`job.id`) | 30s default | poll + download retry; no resubmit | COVERED |
| `images.openai_compat.generate` | OpenAI-compatible | no | 120s | download retry only | COVERED |
| `tts.elevenlabs.synthesize` | ElevenLabs TTS | no (streams bytes) | 120s | none in provider | COVERED |
| `tts.kokoro` / `qwen3` / `chatterbox` | operator or vendor speech | no | 180s | none | COVERED |
| `avatar.server_render` | avatar server | no (`video_url` is an artifact) | **1800s** | download retry only | COVERED |
| `broll.ai_server.generate` | B-roll server | no | **1800s** | download retry only | COVERED |
| `lipsync.external.submit` | external lip-sync | yes (`job_id`) | 60s | **was** blind re-POST; now one submit | COVERED |
| `music.elevenlabs.video_to_music` | ElevenLabs Music | no (streams audio) | (15s, 600s) | none; must-not-resubmit | COVERED |
| `semantic_rerank.twelve_labs.embed` | TwelveLabs | no | split connect/read | degrades, never retries | COVERED |
| `llm.completion_leg` | metered LLM, the POST itself | **no, and none possible** | 120s | **was** ≤4 POSTs; now 1 per ambiguity | COVERED |
| `llm.complete` | metered LLM (composite over the leg) | inherited | inherited | inherited | COVERED, UNRECONCILABLE |
| `providers.dubbing.translate_segments` | metered LLM, batched | inherited | per batch | inherits llm.complete | COVERED |
| agent loops (voice, avatar, broll, dubbing, localization, longform, ugc) | orchestration | inherited | inherited | inherited | COVERED |
| API routes (preview, content, connections, maturity probe) | HTTP surface | inherited | inherited | inherited | COVERED |

### The irreducible limits, stated rather than hidden

None of these is a double-charge path. Each is a fact about a vendor protocol
that no amount of local bookkeeping can change:

1. **A chat completion cannot be reconciled.** It is synchronous: the generated
   text *is* the response body, so the protocol returns no request id, no job id
   and no status endpoint. `llm_reconciliation_capability()` reports
   `UNRECONCILABLE`. A lost completion is priced as an unknown exposure and
   surfaced; a retry needs an explicit, audited `FallbackPolicy` naming an
   approver.
2. **No vendor publishes an idempotency header.** For every row, the protection
   against a second purchase is the local key plus the refusal to resubmit from
   `SUBMISSION_UNKNOWN`.
3. **xKiro, the operator TTS servers, the avatar server and the B-roll server
   report no price.** An accepted render is booked as `UNKNOWN_EXPOSURE` on a
   real reservation row — never `$0`, which `cost.track_cost` would drop.

## The ownerless question (Work 15.9 §1)

Every billable operation has exactly one owner, and a missing workspace is not a
licence to spend. `WORKSPACE_OWNED` is the default and the only default that can
**refuse**: a billable call with an empty `workspace_id` raises
`OwnerlessSpendRefused` *before* the request leaves. The two legitimate
exceptions are declared, never inferred:

* `SYSTEM_OWNED` — an operator-run job on YMONEY's own money, and only against
  an explicitly configured `YMONEY_SYSTEM_BUDGET_USD`. Unset means system spend
  is refused too, because an unbounded system budget is the same hole with a
  nicer name.
* `EXPLICIT_NONBILLABLE` — a positive assertion by the caller that nothing is
  charged. Exactly one caller uses it: `providers/tts.py` for a **local**
  operator TTS server, where it distinguishes "free by design" from "remote and
  accidentally un-gated".

Before §1, four provider sites booked a `CostEntry` against an empty owner
(`images.py`, `tts.py`, `avatar.py`, `broll.py`). Those are gone: the
reservation and the money are now one row, and that row cannot be written
without an owner.

## Reattach accounting (Work 15.9 §5)

The invariant is **exactly-once ACCOUNTING, not exactly-once network**. A remote
job accepted before a crash has been purchased exactly once, so the recovery
path must not reserve, re-book or settle it again.

`PaidOperation.mark_accepted` writes the provider's remote id onto the
**reservation row** (not only the caller's row, which dies with the request that
wrote it), and `paid_provider.reattach_by_remote_id(remote_id)` finds that row
after a restart and returns an operation bound to it: `authorize()` reserves
nothing, `close_book()` settles nothing twice, and the ownership picture travels
with it. One remote id, one accounting identity.

**Known violation, reported not papered over:** the MoneyPrinterTurbo reattach
path in `engine/agents/production.py` skips the reservation when adopting an
existing engine task (correct — the work is already paid for) but books the
completion with `self.track_cost(...)` instead of adopting the original
reservation, so **every reattach adds a second `cost_entries` row for the same
remote task**. The fix is a call to `reattach_by_remote_id(engine_task_id)` in
that file, which is outside this worker's lane;
`backend/tests/test_work15_9_inventory.py` pins the violation so it cannot be
forgotten. The image, TTS, avatar and B-roll lanes have no reattach path at all
today, so there is nothing there to double-count.

## Deliberately not billable

## Deliberately not billable

Local CPU work and free reads are recorded as `NOT_BILLABLE` with a reason, so
"no paid contract" is never mistaken for "we forgot":

`tts.edge` (free Microsoft endpoint) · `avatar.sadtalker` / `avatar.wavlip`
(local weights; Wav2Lip is non-commercial and stays disabled) · `avatar.mock` ·
`broll.native` / `broll.synth` (ffmpeg) · `broll.pexels.search` (free search
API) · `video_engine.timeline_render` (ffmpeg) · `lipsync.musetalk` (local) ·
`motion.hyperframes` (ffmpeg) · `clips.yt_dlp` · `media_intel.local_providers`
· `publishers.platform.publish` · `analytics.snapshot` · `maturity.health_probe`
(`GET /user` is a read).

`broll.pexels.search` deserves a note: Pexels/Pixabay are free to search but
the *paid* risk is the signed download URL, which is handled by
`services/media_cache.py` refusing to cache credential-bound URLs.

## Money visibility

| Situation | Recorded as | Not recorded as |
|---|---|---|
| Provider accepted, amount known | `ACTUAL` | — |
| Provider accepted, amount unpriced | `ESTIMATED` | — |
| Ambiguous submit, money may be spent | **`UNKNOWN_EXPOSURE`** (`ledger_value is None`) | `$0` |
| Provably undelivered (connect failure) | `ESTIMATED` with `0.0` | a phantom charge |
| Cancelled before send | `CANCELLED` | `FAILED` |
| Provider acknowledged cancellation | `CANCELLED` (outcome known) | `FAILED` |

`cost.track_cost` returns early on `<= 0`, which is right for a free call and
wrong for a lost response — so unknown exposure is written by
`cost.book_unknown_exposure`, which invents no number.

Two of these are worth stating because they run in *opposite* directions, and
both are wrong if you get them backwards:

* A billed call whose response was lost must **not** book `$0`; that erases
  real spend.
* A connect failure proves nothing was delivered, so it must **not** claim an
  unknown exposure; that puts a phantom charge on the books.

## Reconciliation

`SUBMISSION_UNKNOWN` exposes four remedies, all audited, all requiring a named
operator:

| Action | Effect on state |
|---|---|
| `RECONCILE` | state stays `SUBMISSION_UNKNOWN` — it genuinely is unconfirmed |
| `RETRY_IF_CONFIRMED_SAFE` | marks retry safe; only honoured when proven |
| `MARK_FAILED` | closes the incident but **keeps the exposure visible** |
| `MANUAL_OVERRIDE` | never changes state; records operator + note |

## How this stays true

* `paid_jobs_audit.verify_against_source()` runs **both ways**: a row whose
  cited call site has vanished is `STALE`, and a money marker no row claims is
  `MISSING`. Work 15.6's version only checked one direction, so it could not
  detect its own worst error — a paid engine filed as free. That row
  (`ffmpeg_avatar`) is now composite and billable.
* `BillablePath.covered` is **computed** by reading the declared sites, so
  `covered=True` cannot be asserted by hand.
* Every uncovered billable path must carry a gap naming a `file:line`, or two
  tests fail.
* No provider may claim `IDEMPOTENCY_SUPPORTED` without vendor documentation.