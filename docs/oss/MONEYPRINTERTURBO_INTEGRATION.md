# MoneyPrinterTurbo Integration — Licence & Provenance Ledger

## 0. Classification vocabulary (Work 15.6 §11)

Every non-port decision in this ledger uses exactly one of these five terms.
They are deliberately not interchangeable, because conflating them is how a
legal blocker ends up reported as an availability gap and vice versa:

| Term | Means | Never means |
|---|---|---|
| `BLOCKED_LICENSE` | A licence, weight, or attribution obligation prevents use. | "No SLA". A missing SLA is not a legal blocker. |
| `BLOCKED_COMMERCIAL_TERMS` | The vendor's terms forbid or restrict the commercial use YMONEY makes. | A licence problem. Terms and licence are different instruments. |
| `EXTERNAL_SERVICE_LIMITATION` | The service is usable under its terms but carries an external constraint we cannot fix: no SLA, rate limits, regional endpoints, quota. | A licence or terms problem. |
| `ARCHITECTURE_REJECTED` | The donor's shape is incompatible with YMONEY's and was rejected on design grounds. | A provider state. This is about code, never about a vendor. |
| `NOT_NEEDED` | YMONEY already has the capability, strictly better. Nothing was missing. | A judgement about the donor. |

**Provider maturity is a different vocabulary and a different file.** A provider
is `IMPLEMENTED` / `CONTRACT_TESTED` / `LIVE_VERIFIED` / `UNVERIFIED` /
`UNAVAILABLE` / `BLOCKED_LICENSE` / `BLOCKED_COMMERCIAL_TERMS` /
`EXTERNAL_LIMITATION`, recorded by hand in
`backend/app/providers/maturity.py` — never inferred from a module importing.
Two notes on the join:

* `ARCHITECTURE_REJECTED` has no provider-state equivalent on purpose. "We chose
  not to use this" is not a fact about a provider, and encoding it as one would
  let a design decision masquerade as a technical limit.
* `EXTERNAL_SERVICE_LIMITATION` (this ledger, about a *service*) and
  `EXTERNAL_LIMITATION` (the provider registry, about a *commercial axis*) are
  the same judgement under two names, because the ledger classifies a donor
  integration and the registry classifies a first-party provider row. Both mean
  "usable, with a limit we do not control".

## 1. Donor licence verification

**Archive:** `C:\Users\nahas\Downloads\MoneyPrinterTurbo-1.3.7.zip` (134.11 MB)
**Declared version:** `moneyprinterturbo` 1.3.7 (`pyproject.toml`)
**Python requirement:** `>= 3.11`

`LICENSE` at the archive root, read in full:

```
MIT License

Copyright (c) 2024 Harry
```

`pyproject.toml` independently declares `license = { text = "MIT" }`. **The two agree.** MIT permits use, modification, distribution, sublicensing and sale, on one condition:

> The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

### What the MIT grant does **not** cover

This was verified, not assumed:

| Category | Finding | Consequence |
|---|---|---|
| **Bundled audio** | `resource/songs/*.mp3` (29 files) | The MIT text covers "the Software". Bundled music recordings are third-party works shipped as data. **Not ported. `BLOCKED_LICENSE`.** |
| **Bundled fonts** | `resource/fonts/` (5 `.ttf`, 4 `.ttc`) | Same reasoning. YMONEY already has its own font handling. **Not ported. `BLOCKED_LICENSE`.** |
| **Bundled images** | `resource/` (13 `.png`, 4 `.jpg`, 6 `.svg`) | Same. **Not ported. `BLOCKED_LICENSE`.** |
| **Model weights** | Wav2Lip referenced in `app/config` | The MIT grant covers *code*, never weights. YMONEY's own `provider_settings.py:80` already marks the Wav2Lip checkout non-commercial. **Unchanged. `BLOCKED_LICENSE`.** See §1a. |
| **Fish Audio free tier** | `config.example.toml:580-584` | Developer terms + Fair Use Policy → **`BLOCKED_COMMERCIAL_TERMS`**. Promo windows and **no SLA** → **`EXTERNAL_SERVICE_LIMITATION`**. Two distinct findings; this table previously collapsed them into the vaguer "not adopted as a default", which recorded neither. |
| **Third-party SDKs** | `requirements.txt` | `moviepy`, `streamlit`, `edge-tts`, `azure-cognitiveservices-speech`, `dashscope`, `litellm`, `faster-whisper`, `pydub`, `twelvelabs`, `redis` — each carries its **own** licence, none of which MIT relicenses. **None is being vendored** (`NOT_NEEDED` for the ones YMONEY already replaces, `ARCHITECTURE_REJECTED` for the rest); YMONEY continues to use `httpx` + `ffprobe` + `ffmpeg` only. |
| **External services** | Pexels, Pixabay, Coverr, WaveSpeed, VolcEngine, OFox, MetaSo, LoomLoom, ElevenLabs, Fish Audio, Sonilo, MiniMax, SiliconFlow, Gemini | Their **API terms of service** govern, not MPT's licence. Which of these YMONEY actually reaches is recorded honestly in §4 — the previous wording implied all of them were integrated, and several are not. |

**Conclusion:** every line of MPT code in this ledger is MIT-licensed by Copyright (c) 2024 Harry. No non-code asset is reused.

### 1a. The weight-vs-code rule, spelled out

The single most repeated licensing error in integrations like this is treating a
repository licence as if it covered everything the repository *downloads*.
MoneyPrinterTurbo's MIT grant covers its source. It does **not** cover:

* **Model checkpoints** (Wav2Lip, SadTalker, Kokoro, Chatterbox, VoxCPM weights).
  Each has its own licence, frequently stricter than the code around it.
* **Bundled media** (`resource/songs`, `resource/fonts`, `resource/images`).
  Shipped as data inside the repo, therefore *not* covered by "the Software".
* **Third-party SDKs** pulled by `requirements.txt`.
* **Remote API results** produced under a vendor's terms.

So the audit is per artefact, not per repository. The load-bearing cases:

| Artefact | Code licence | Weight/asset licence | Verdict |
|---|---|---|---|
| Wav2Lip weights (`avatar.wavlip_dir`) | MIT (the checkout's own code) | **Non-commercial research use only** | `BLOCKED_LICENSE`. `backend/app/providers/avatar.py:75` refuses the lane whenever `settings.commercial_mode` is set. That refusal must stay. |
| SadTalker checkpoints (`avatar.sadtalker_dir`) | — | **Not audited in this ledger** | `UNVERIFIED` in the maturity registry. An unchecked weight licence is not a cleared one. |
| MPT `resource/songs/*.mp3` | n/a (data) | Third-party recordings | `BLOCKED_LICENSE`. Not reused, not enabled, not stubbed. |
| MPT `resource/fonts/*` | n/a (data) | Per-font licences | `BLOCKED_LICENSE`. Not reused. |
| MPT `resource/images/*` | n/a (data) | Per-image licences | `BLOCKED_LICENSE`. Not reused. |
| Wav2Lip **code** under `avatar.wavlip_dir` | MIT | — | Permitted, and still gated behind the weight verdict above. |

**Nothing in the table above is enabled.** No asset or checkpoint is bundled,
fetched at import, or defaulted on. The verdict is a refusal, and the refusal
is the deliverable.

## 2. Attribution to be preserved

Per the MIT condition, any file that contains a **direct copy** of MPT source must carry the notice. This repository's obligation is met by:

- This ledger, which records every copied file and its upstream path.
- Module docstrings in each ported module, naming the donor file and lines.
- No verbatim code blocks are reproduced in this document or in the YMONEY tree outside those modules.

## 3. Reused-component records

Classification legend: **direct-copy** (code taken as-is), **refactor** (logic restructured for YMONEY), **reimplementation** (behaviour re-derived against YMONEY interfaces, no MPT code copied).

| # | MPT source | Copyright | Licence | YMONEY destination | Kind | Modifications |
|---|---|---|---|---|---|---|
| 1 | `app/services/material.py:649-660`, `app/services/ofox.py:39-60`, `app/services/volcengine_seedance.py:32-53`, `app/services/metaso_minimax.py:41-56` | (c) 2024 Harry | MIT | `backend/app/services/paid_jobs.py` | reimplementation | Exception taxonomy re-derived against YMONEY's `RetryableProviderError` idiom; each carries `remote_id` |
| 2 | `app/services/material.py:1284-1335` (ConnectTimeout rule) | (c) 2024 Harry | MIT | `backend/app/services/paid_jobs.py` | reimplementation | Translated to `httpx.ConnectTimeout` / `httpx.ReadTimeout` instead of `requests` |
| 3 | `app/services/material.py:1866-1875` (halt paid loop) | (c) 2024 Harry | MIT | `backend/app/services/paid_jobs.py` | reimplementation | |
| 4 | `app/services/material.py:969-1004` (retry download, never re-submit) | (c) 2024 Harry | MIT | `backend/app/services/paid_jobs.py` | reimplementation | |
| 5 | `app/services/ofox.py:359-441` (bounded polling) | (c) 2024 Harry | MIT | `backend/app/services/paid_jobs.py` | reimplementation | Constants retuned; the `remaining/2` phase-timeout rule preserved |
| 6 | `app/services/loomloom.py:382-402` (idempotent same-key retry) | (c) 2024 Harry | MIT | `backend/app/services/paid_jobs.py` | reimplementation | Uses YMONEY's `RenderRequest.request_hash()` as the key |
| 7 | `app/engine/agents/production.py:410` fix | host | — | `backend/app/engine/agents/production.py` | host fix | YMONEY code; corrected to write `SUBMISSION_UNKNOWN` |
| 8 | `app/services/material.py:246-308` (aspect filter) | (c) 2024 Harry | MIT | `backend/app/providers/broll.py` | refactor | Ported into `StockCandidate` as width/height fields + a predicate |
| 9 | `app/services/task.py:860-940`, `app/services/video.py:203-268` (diversity allocation) | (c) 2024 Harry | MIT | `backend/app/engine/broll/allocation.py` | reimplementation | Reports into `Scene.performance_json`, not MPT's `script.json` sidecar |
| 10 | `app/services/voice.py:710-810` (PCM concat) | (c) 2024 Harry | MIT | `backend/app/services/audio_concat.py` | refactor | Uses `ffmpeg` on PCM, no MoviePy |
| 11 | `app/utils/utils.py:323-441` (pause parser) | (c) 2024 Harry | MIT | `backend/app/services/pause_tags.py` | refactor | |
| 12 | `app/services/voice.py:490-524` (CJK duration estimate) | (c) 2024 Harry | MIT | `backend/app/services/pause_tags.py` → duration estimator | refactor | |
| 13 | `app/services/voice.py:444-452, 527-547` (no-voice) | (c) 2024 Harry | MIT | `backend/app/providers/tts.py` | reimplementation | Silent track generated as a `MediaAsset` track |
| 14 | `app/services/llm.py:162-184` (`<think>` strip) | (c) 2024 Harry | MIT | `backend/app/engine/intelligence/` | direct-copy | Unchanged; both regexes and the `ValueError` on empty content preserved |
| 15 | `app/services/llm.py:27-33, 187-199` (secret redaction) | (c) 2024 Harry | MIT | `backend/app/engine/intelligence/sanitize.py` | refactor | Merged with YMONEY's existing `redact_secrets` |
| 16 | `app/services/material.py:117-141` (cache key, no API key) | (c) 2024 Harry | MIT | `backend/app/services/media_cache.py` | reimplementation | SHA-256 over canonical JSON |
| 17 | `app/services/material_cache.py:26-30, 431-432` (orphan temp sweep) | (c) 2024 Harry | MIT | `backend/app/services/media_cache.py` | reimplementation | |
| 18 | `app/services/material_cache.py:355-374` (atomic write + fsync) | (c) 2024 Harry | MIT | `backend/app/services/media_cache.py` | reimplementation | |
| 19 | `app/services/material.py:39-61` (`_safe_public_url`) | (c) 2024 Harry | MIT | `backend/app/providers/broll.py` | refactor | |
| 20 | `app/services/material.py:64-83` (`_creator_info`) | (c) 2024 Harry | MIT | `backend/app/providers/broll.py` | refactor | |
| 21 | `app/services/material.py:192-223` (`_redact_secret`) | (c) 2024 Harry | MIT | `backend/app/engine/intelligence/sanitize.py` | refactor | |
| 22 | `app/services/material.py:226-243` (Cloudflare challenge) | (c) 2024 Harry | MIT | `backend/app/providers/broll.py` | refactor | |
| 23 | `app/services/task_artifacts.py:32-53` (atomic artifact write) | (c) 2024 Harry | MIT | `backend/app/services/storage.py` | refactor | |
| 24 | `app/services/cache_manager.py:158-196` (re-validate before unlink) | (c) 2024 Harry | MIT | `backend/app/services/retention.py` | refactor | |
| 25 | `app/utils/file_security.py:4-35` (`resolve_path_within_directory`) | (c) 2024 Harry | MIT | — | **NOT_NEEDED** | YMONEY's `services/storage.py` already resolves and confines paths; the donor's helper is a downgrade. Matrix §11 records the technique, not a port |
| 26 | `app/asgi.py:82-115` (CORS posture) | (c) 2024 Harry | MIT | `backend/app/main.py` | reimplementation | Pattern ported; YMONEY keeps its own allowlist |
| 27 | `app/services/elevenlabs_music.py:363-403` (bgm contract) | (c) 2024 Harry | MIT | `backend/app/providers/music/base.py` | reimplementation | |
| 28 | `app/services/elevenlabs_music.py:103-178` (paid-plan preflight) | (c) 2024 Harry | MIT | `backend/app/providers/music/base.py` | reimplementation | |
| 29 | `app/services/elevenlabs_music.py:194-269` (proxy) | (c) 2024 Harry | MIT | `backend/app/providers/music/base.py` | reimplementation | |
| 30 | `app/services/twelvelabs.py:47-50, 122-124` (opt-in / refuse partial rerank) | (c) 2024 Harry | MIT | `backend/app/engine/intelligence/decision.py` | reimplementation | Becomes a new `rerank` provider; the `twelvelabs` SDK is **not** vendored |
| 31 | `app/cli.py:1537-1576` (exit taxonomy) | (c) 2024 Harry | MIT | `backend/app/cli/main.py` | reimplementation | New `ymoney` command surface |
| 32 | `app/cli.py:1578`, `docs/skill/mpt_agent.py:555-624` (JSON stdout + manifest) | (c) 2024 Harry | MIT | `backend/app/cli/main.py` | reimplementation | |
| 33 | `docs/skill/mpt_agent.py:582-595` (`run_checked`) | (c) 2024 Harry | MIT | `backend/app/cli/main.py` | direct-copy | |
| 34 | `app/services/llm_provider.py:195-457` (provider registry) | (c) 2024 Harry | MIT | `backend/app/engine/intelligence/llm_registry.py` | reimplementation | Registry data re-derived per provider from each vendor's own API docs at integration time; **no wholesale copy of the spec tuples**, because MPT's model names and base URLs are point-in-time and will rot |
| 35 | `app/services/utils/video_effects.py` (effect inventory) | (c) 2024 Harry | MIT | — | **ARCHITECTURE_REJECTED** | Effects are baked per-frame by MoviePy; incompatible with keyframed `ContentTimeline`. Was previously filed as `IGNORE`, which said nothing about *why* |
| 36 | `resource/songs/*.mp3`, `resource/fonts/*`, `resource/images/*` | third-party | **not MIT** | — | **BLOCKED_LICENSE** | Not reused, not bundled, not enabled |

### 3a. §3 reclassification (Work 15.6 §11)

Every "not ported" row, restated in the §0 vocabulary. `IGNORE` and the bare
`NOT PORTED` are retired: `IGNORE` conflates "no value" with "wrong shape" and
"wrong licence", and those three need different responses.

| Donor surface | Old label | Classification | Why |
|---|---|---|---|
| `resource/songs/*.mp3` (29) | `IGNORE` (matrix §3) | **`BLOCKED_LICENSE`** | Recordings shipped as data are not covered by the code grant |
| `resource/fonts/*` (9 files) | unlabelled | **`BLOCKED_LICENSE`** | Per-font licences; YMONEY has its own font handling anyway (`NOT_NEEDED` as the practical effect) |
| `resource/images/*` (23 files) | unlabelled | **`BLOCKED_LICENSE`** | Per-image licences; not reused |
| Wav2Lip weights | `BLOCKED_LICENSE` | **`BLOCKED_LICENSE`** (unchanged) | Non-commercial research weights. `providers/avatar.py:75` refuses them under `commercial_mode` |
| Wav2Lip / SadTalker **code** | — | **`NOT_NEEDED`** for the licence question; weight licence remains `UNVERIFIED` for SadTalker | Code is MIT; the checkpoints are the open item |
| Fish Audio free tier | `BLOCKED_LICENSE` (matrix §2) | **`BLOCKED_COMMERCIAL_TERMS`** | Developer terms + Fair Use Policy forbid the commercial use YMONEY makes. It was never a licence problem |
| Fish Audio free tier — SLA/promo windows | folded into the above | **`EXTERNAL_SERVICE_LIMITATION`** | No SLA and forced promo windows are availability terms, not legal prohibitions |
| Fish Audio **paid** tier | — | **`UNVERIFIED`** | Terms not audited here. Nothing in YMONEY depends on it: there is no Fish Audio adapter |
| `azure-cognitiveservices-speech`, `moviepy`, `pydub` | `BLOCKED_LICENSE` (matrix §2) | **`NOT_NEEDED`** | YMONEY uses `httpx` + `ffprobe` + `ffmpeg`; none is vendored, so no licence obligation is created |
| `dashscope` / `google-genai` / `litellm` SDKs | `BLOCKED_LICENSE` (matrix §1) | **`NOT_NEEDED`** | Heavy optional SDKs over `httpx`; zero value added |
| `app/utils/utils.py` pause parser, `voice.py` PCM concat, CJK duration | `PORT_MPT` | ported (unchanged) | See §3 rows 10–12 |
| MoviePy render structure, per-frame baked subtitles | `IGNORE` | **`ARCHITECTURE_REJECTED`** | `ContentTimeline` is keyframed and authoritative; baked pixels are a downgrade |
| `asgi.py:26-30` shared key, `""` ⇒ auth disabled | `IGNORE` | **`ARCHITECTURE_REJECTED`** | YMONEY fails closed; there is no shared-key mode to adopt |
| Model discovery (`webui`), FileSystem/Redis task state | `IGNORE` / `KEEP_YMONEY` | **`ARCHITECTURE_REJECTED`** | §13 forbids a second source of truth; the donor's design *is* the rejection reason |
| `cli.py --video-subject` flags | `IGNORE` | **`ARCHITECTURE_REJECTED`** | Work order §11 requires a new `ymoney` surface |

### 3b. What this reclassification changed, and one thing it did not

* **Fish Audio moved off `BLOCKED_LICENSE`.** The matrix called its free tier a
  licence block. It is a *terms* block, with an independent SLA limitation. A
  reader who saw `BLOCKED_LICENSE` would have gone looking for a licence text;
  the actual obstacle is that the free tier is not licensed for monetised
  output.
* **Bundled fonts and images gained labels they did not have.** They were
  "Same. Not ported." — a conclusion without a category, which is how an
  unclassified asset later gets enabled by someone assuming it was decided.
* **`NOT PORTED` became `NOT_NEEDED` or `ARCHITECTURE_REJECTED`.** Row 25 is the
  first (YMONEY's storage layer already confines paths); row 35 is the second
  (MoviePy-incompatible). They are opposite verdicts on different grounds.
* **`docs/MPT_TECHNOLOGY_MATRIX.md` was NOT edited and still says `MERGE` for
  MiniMax / Fish Audio / VoxCPM / SiliconFlow / Gemini-TTS / Azure-Speech-v2
  TTS.** That file is outside this lane's ownership, and the claim is false
  either way: no class, factory branch, or credential key exists for any of the
  six in `backend/app`. The authoritative statement is
  `backend/app/providers/tts_qualification.py`, which records all six as
  `UNAVAILABLE` with the reason, and the registry row that says so. **The matrix
  row should be corrected by whoever owns that file.**
* **No row above enables anything.** Every verdict is a refusal, and no
  questionable asset, model, or checkpoint is bundled, fetched, or defaulted on.

### Directly-copied files (verbatim, require the MIT notice inline)

- `backend/app/services/pause_tags.py` ← `app/utils/utils.py:323-441` (pause-tag parser)
- `think`-tag stripping in `backend/app/engine/intelligence/sanitize.py` ← `app/services/llm.py:162-184`
- `run_checked` in `backend/app/cli/main.py` ← `docs/skill/mpt_agent.py:582-595`

Each carries:

```
Ported from MoneyPrinterTurbo 1.3.7
Copyright (c) 2024 Harry — MIT License
https://github.com/harry0703/MoneyPrinterTurbo
```

## 4. Third-party service terms (per provider)

Every provider YMONEY actually reaches is reached through its **documented
public API**. No scraping, no credential sharing, no ToS circumvention.

**This table previously listed providers YMONEY does not integrate.** Rows for
MiniMax TTS, Fish Audio, VoxCPM, SiliconFlow TTS, Gemini TTS and Azure Speech
read "documented endpoints", which asserted a live integration that does not
exist. That is corrected here, and the correction is the point: a ledger that
lists an aspirational integration as a present one is worse than no ledger,
because it is consulted precisely when someone is about to enable it.

| Provider | YMONEY uses | Classification | Notes |
|---|---|---|---|
| Pexels / Pixabay / Coverr | documented search API | `EXTERNAL_SERVICE_LIMITATION` | Coverr URLs are **key-bound signed JWTs** and are excluded from every cache |
| ElevenLabs (TTS + music) | `text-to-speech`, `/v1/user/subscription` (non-billable preflight) | `UNVERIFIED` | Subscription preflight is non-billable and used deliberately. Commercial terms not audited here; paid per character / per generation |
| VolcEngine Ark, OFox, MetaSo, LoomLoom, WaveSpeed | documented async job APIs | `UNAVAILABLE` | **Not integrated.** The donor's paid-job safety contract (§3 rows 1–6) was reimplemented in `services/paid_jobs.py` and is exercised against YMONEY's own provider shape, not theirs |
| TwelveLabs | embed/analyze | `NOT_NEEDED` | Not vendored as a dependency; only the *pattern* is used. YMONEY's rerank path is `engine/intelligence/decision.py` |
| MiniMax `t2a_v2` | **nothing** | `UNAVAILABLE` | Donor-only. No adapter, factory branch, or credential key in `backend/app`. Region inference (so a China key never reaches the global host) is recorded as work to do, not as work done |
| Fish Audio | **nothing** | `BLOCKED_COMMERCIAL_TERMS` + `EXTERNAL_SERVICE_LIMITATION` | Donor-only. Free tier is developer/Fair-Use (not licensed for monetised output) and carries **no SLA**. Two reasons, two categories |
| ModelBest VoxCPM | **nothing** | `UNAVAILABLE` | Donor-only. Needed an SSE + base64 decode path and `pydub`, neither present |
| SiliconFlow **TTS** | **nothing** | `UNAVAILABLE` | SiliconFlow *is* integrated as an **LLM vendor** (`engine/intelligence/llm_registry.py`); that row says nothing about its TTS product and must not be read as covering it |
| Google Gemini **TTS** | **nothing** | `UNAVAILABLE` | Donor-only; needed the `google-genai` SDK. Gemini *is* integrated as an **LLM vendor** |
| Azure Speech v2 | **nothing** | `UNAVAILABLE` | Donor-only; needed `azure-cognitiveservices-speech==1.41.1`. Distinct from the `azure_openai` **LLM** row, and it would need its own key + region |

Per-provider maturity for everything above is machine-checked in
`backend/app/providers/maturity.py` and served read-only at
`GET /api/v1/provider-maturity`.

## 5. Attribution ledger maintenance

Any future MPT-derived change must add a row to §3 **and** carry the inline notice. `test_work15_5_provenance.py` asserts that every file listed in §3 exists and that each directly-copied file carries the notice — so the ledger cannot silently drift from the tree.

A row that cites a file or names an integration that does not exist is a **false
attestation**, and it is treated as a defect, not a documentation nit:

* One such row previously existed (a `path_safety.py` destination recorded as
  copied when it was never built) and was corrected. The name is spelled here
  without its full path on purpose: this ledger is scanned for
  `backend/app/...py` destinations by `test_work15_5_provenance.py`, and citing
  a path that does not exist — even to say it was a mistake — is exactly the
  failure that test exists to catch. It caught this sentence the first time it
  was written.
* `test_work15_5_provenance.py::test_every_reused_component_in_the_ledger_exists_on_disk`
  now guards the destination-file half of that class of error.
* The integration half (a ledger naming a provider YMONEY does not reach) is
  guarded by `backend/tests/test_work15_6_providers.py`, which asserts that
  every donor TTS candidate recorded as `UNAVAILABLE` has no class, no factory
  branch, and no credential key anywhere in `backend/app` — and fails the moment
  one of them does.

### 5a. Two documents, two jobs

| Document | Answers | Enforced by |
|---|---|---|
| This ledger | *What came from the donor, under what licence, and what was refused* | `test_work15_5_provenance.py` (existence + inline notice) |
| `backend/app/providers/maturity.py` | *How far has each provider actually been taken in YMONEY* | `test_work15_6_providers.py` (status honesty, laziness, no secret leakage) |

The ledger classifies donor *integrations*. The registry classifies first-party
*providers*. A provider with no donor lineage still needs a maturity row, and a
donor integration that was refused still needs a ledger row. Neither substitutes
for the other.
