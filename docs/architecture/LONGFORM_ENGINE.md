# YMONEY Long-Form Engine (Work 03)

Dedicated `LongFormVideoPipeline` for 3–30 minute multi-chapter videos.
Short-form duration logic is never reused here: narrative, pacing, chapters,
continuity, and chunked rendering are long-form specific.

## Flow

```text
LongFormRequest → Opportunity/Topic → ResearchBundle → LongFormStrategy
→ ChapterPlan → LongFormScript → FactVerification → SceneGraph (Scene rows)
→ AssetPlan → Acquire → Voice (measured) → ContentTimeline → QC
→ Chunked Render → Final MP4 → QC → Thumbnails/Metadata
```

Every stage is a durable job (`longform.stage`): one stage runs, then the
next enqueues. No giant synchronous function, no while-loops in requests.
Stages are idempotent — resume re-runs only what's missing (pending-stage
resolution). REVIEW autonomy pauses at SCRIPT and RENDER; MANUAL advances
only via explicit API calls; CANCELLED stops the chain.

## Key decisions

- **Audio is authoritative.** TTS per segment (one voice, pronunciation dict
  applied), ffprobe-measured; captions, scene ranges, chapter boundaries,
  and the timeline all derive from measured durations.
- **Scenes are Work 01 Scene rows** (+chapter_id, +beats_json). Visual beats
  (2–6s shot units) live as beat metadata — never thousands of Scene rows.
- **Timeline output is canonical.** Voice/caption clips per segment, broll
  per beat, chapter title cards as editable text. Opens in `/editor/:id`.
  Manual-edit protection: regeneration snapshots a version first and requires
  explicit confirm when the timeline advanced since generation.
- **Assets resolve through `VisualAssetProvider`** (stock/local/AI-image/
  graphic). `ai_video` raises a descriptive error (Wan2.2 evaluated, no GPU
  here) so the fallback chain stays honest and recorded.
- **Render is chapter-chunked** with per-chunk retry, content-hash cache
  (resume without regenerating), and concat assembly. Finals use ORIGINAL
  assets; proxies (`ProxyMediaService`, 720p) are editor-only and linked via
  `meta.original_asset_id`.
- **Facts never lose provenance.** Claims carry status + source; CONTESTED /
  UNVERIFIED surface in the fact report and force QC to REQUIRES_REVIEW.
- **LLM is enhancement, not foundation.** Every stage builds a deterministic
  artifact first; LLM output is validated, garbage falls back (logged).

## Failure behavior

Provider failure → recorded fallback chain; TTS failure → segment marked
failed, QC gates (no silent filler); chunk failure → bounded retry, then the
stage fails loudly with resume intact; tight budgets → ECONOMY strategy
(stock/local/graphics).
