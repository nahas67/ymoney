# MPT Technology Matrix

**Donor:** MoneyPrinterTurbo 1.3.7 (`MoneyPrinterTurbo-1.3.7.zip`, 134.11 MB; operator-supplied archive, not required for release tests)
**Recipient:** YMONEY, post-Work-15 (2225 tests green, 0 failed)
**Rule:** MPT is a technology **donor**. YMONEY remains authoritative for the data model, ContentTimeline, editor, campaigns, planner, BrandDNA, DecisionEngine/ModelRouter, memory graph, publishing, analytics, community, RBAC, CompletionVerifier, and durable workflow state.

## Inspection scope

| Measure | Count |
|---|---|
| Python files in archive | 116 |
| Production Python (excluded `test/`) | 55 files, ~19,000 lines |
| Test Python | 61 files, ~19,000 lines |
| WebUI | `webui/Main.py` — 6,993 lines (single Streamlit file) |
| Frontend pages in donor | 1 (Streamlit) |
| Largest subsystems inspected | `material.py` 2118 · `voice.py` 2625 · `task.py` 1532 · `video.py` 1461 · `cli.py` 1449 · `llm.py` 1035 · `loomloom.py` 860 |

Every production source file under `app/`, plus `cli.py`, `main.py`, `docs/skill/mpt_agent.py`, `pyproject.toml`, `config.example.toml`, `Dockerfile*`, `docker-compose*.yml`, and `.github/**` was read. See [the provenance ledger](oss/MONEYPRINTERTURBO_INTEGRATION.md) for per-component attribution.

## Classification vocabulary

| Code | Meaning |
|---|---|
| `KEEP_YMONEY` | YMONEY already does this, and does it better. Do not touch. |
| `PORT_MPT` | Copy a concrete, self-contained piece of code/algorithm into YMONEY. |
| `MERGE` | Add an MPT provider or feature into an existing YMONEY interface. |
| `REIMPLEMENT` | The idea is right; write it fresh against YMONEY interfaces. |
| `IGNORE` | No value, or donor architecture is explicitly out of scope. |
| `BLOCKED_LICENSE` | Licence, weight, or SDK obligation prevents use. |

---

## 1. LLM providers

| Finding | Source | Class | Rationale |
|---|---|---|---|
| `<think>`/`</think>` + **unclosed**-block stripping | `llm.py:162-184` | **PORT_MPT** | YMONEY routes reasoning models in `HIGH_QUALITY`/`PREMIUM` tiers with no stripping anywhere; the leak would reach subtitles and voice |
| URL-userinfo + sensitive-query redaction of exception text | `llm.py:27-33, 187-199` | **PORT_MPT** | YMONEY has `redact_secrets` but applies it only to the routing log, not to `providers/llm.py:127` warnings or raised `LLMError` text |
| 30-provider registry (per-provider key/base_url/model, regional endpoints, deprecated-model migration) | `llm_provider.py:195-457` | **MERGE** | YMONEY's single biggest LLM gap: one `llm.api_key`/`llm.base_url` per workspace |
| Never persist a registry default (`normalize_provider_override`) | `llm_provider.py:469-480` | **REIMPLEMENT** | Required the moment the registry lands — YMONEY persists credentials to DB, so a stale default can freeze |
| Container/gateway-aware Ollama base URL | `config.py:311-419` | **REIMPLEMENT** | YMONEY `LOCAL_ONLY`/`PRIVATE` tiers have no local-model resolution |
| Config coercion that preserves `0` / `False` | `llm.py:132-144` | **REIMPLEMENT** | `raw or default` rewrites a legal `0` into the default; YMONEY's `get_credential` has the same class of bug |
| Subprocess env scrubbing by prefix class | `llm.py:84-159` | **REIMPLEMENT** | Generalisable credential-hygiene idea |
| Claude-Code CLI adapter | `llm.py:473-585` | **IGNORE** | YMONEY has an SDK-based Claude provider; no subscription-login use case |
| `ModelRouter` tier routing, health flip, fallback chain, `PrivacyRefusal` | `router.py:255-537` | **KEEP_YMONEY** | Strictly more capable than MPT's `if adapter ==` chain |
| Model discovery | `api/v1/connections.py:161-173` | **KEEP_YMONEY** | MPT has none |
| Cost / token accounting | `providers/llm.py:109-123` | **KEEP_YMONEY** | MPT tracks nothing |
| `test_connection` | `llm.py:637-660` | **KEEP_YMONEY** | YMONEY's validates the model exists |
| Errors returned as `"Error: "` strings | `llm.py:633-634` | **IGNORE** | Donor's own comments call it a bug; importing it would defeat `LLMError` + `step_failed` |
| Bare 5× retry, no backoff | `llm.py:781` | **IGNORE** | YMONEY needs backoff, not this |
| `dashscope` / `google-genai` / `litellm` SDKs | `pyproject.toml` | **BLOCKED_LICENSE** | Heavy optional SDKs for zero value over `httpx` |
| Referral/affiliate URLs in `api_key_url` | `llm_provider.py:206-231` | **IGNORE** | Marketing |

## 2. TTS / voice

| Finding | Source | Class | Rationale |
|---|---|---|---|
| **PCM-decode concatenation** — decode every segment to 24 kHz 16-bit mono, encode **once** | `voice.py:710-810` | **PORT_MPT** | YMONEY `agents/voice.py:198-201` uses `ffmpeg -c:a copy` over MP3 parts — the exact cumulative encoder-delay drift MPT's docstring names. Also propagates into `motion/transitions.py:187` crossfades |
| **Pause-tag parser** — 18-locale keywords, malformed-tag stripping, clamping, consecutive-merge | `utils.py:323-441` | **PORT_MPT** | YMONEY has **zero** pause support (grep: 0 matches for `[pause`/`parse_script_with_pauses`) |
| Subtitle offsets from **decoded sample count**, not wall time | `voice.py:938-939` | **PORT_MPT** | Follows from the concat fix; guarantees caption sync in segmented narration |
| CJK / non-ASCII duration estimate (4.2 char/s CJK, 4.0 other, 2.7 word/s ASCII) | `voice.py:490-524` | **PORT_MPT** | YMONEY `_estimate_duration` (`ugc/voice.py:40`) is English-words-only; YMONEY targets zh/ru/ar |
| **No-voice / silent-track mode** | `voice.py:82-86, 444-452, 527-547` | **PORT_MPT** | New capability. Load-bearing rule: `""` must NOT read as silent, or a config error masquerades as success |
| Atomic write + real decode validation before replacing a file | `voice.py:2163-2181, 1922-1954` | **REIMPLEMENT** | YMONEY returns bytes from `synthesize()`; the helper belongs at the write site |
| Non-retryable error classification (401/403/422, 402 insufficient credit) | `voice.py:2060-2090, 2382-2399` | **REIMPLEMENT** | YMONEY has **zero** TTS retries; needs classification before backoff |
| Real backoff `(1.0, 2.0)` on `RequestException` only | `voice.py:81, 2588-2591` | **REIMPLEMENT** | The only real backoff in MPT's TTS |
| Edge-TTS stream deadline via daemon thread + queue | `voice.py:1190-1240` | **REIMPLEMENT** | `EdgeTTSProvider._run` can hang on `communicate.stream()` with no deadline |
| Regional key↔endpoint inference (China key must not hit the global host) | `voice.py:1813-1840` | **REIMPLEMENT** | Cheap; generalises to every dual-region provider |
| MiniMax, Fish Audio, VoxCPM, Azure-v2, SiliconFlow, Gemini-TTS providers | `voice.py:1957-2596` | **MERGE** | All fit `BaseTTSProvider` (`providers/tts.py:55`) unchanged |
| `BaseTTSProvider` interface, structured dict catalog, cost tracking, ffprobe duration | `providers/tts.py:55-70`, `ugc/voice.py:80-86` | **KEEP_YMONEY** | Strictly better than MPT's prefixed-string catalog |
| `voice:<id>:<label>` prefixed-string catalog | `voice.py:109-349` | **IGNORE** | YMONEY returns `{id, gender, locale}` |
| MoviePy / `SpeechSDK` / `pydub` SDKs | `requirements.txt` | **BLOCKED_LICENSE** | YMONEY uses `httpx` + `ffprobe` exclusively |
| Fish Audio `s2.1-pro-free` default | `config.example.toml:580-584` | **BLOCKED_LICENSE** | Developer terms, Fair Use, no SLA |
| Wav2Lip weights | `provider_settings.py:80` | **BLOCKED_LICENSE** | Non-commercial, already flagged in YMONEY's registry |

## 3. Material / stock

| Finding | Source | Class | Rationale |
|---|---|---|---|
| **Batch material diversity allocation** — longest-clip-first, new-before-reuse, round-robin by keyword group, reuse tracking, failed clips don't count as used | `task.py:860-940`, `video.py:203-268` | **PORT_MPT** | YMONEY `broll.py:339` is `kws[i % len(kws)]` — with 2 keywords and 8 scenes that is two queries repeated four times, no reuse tracking |
| Aspect/orientation re-verification | `material.py:246-308` | **PORT_MPT** | YMONEY `StockCandidate` carries no width/height and `_best_mp4` (`broll.py:153-161`) picks by height with **no orientation check** — a 9:16 request can silently fetch 16:9 |
| On-demand paid generation: accumulate until `>=` target duration (not `>`), with NaN/Inf loop-bound guards | `material.py:1839-1908, 1924-1954` | **PORT_MPT** | YMONEY `generate_clip` has no accumulation, no duration budget, no stop rule |
| `_safe_public_url` — strip query, reject userinfo | `material.py:39-61` | **PORT_MPT** | `broll.py:148` writes `page_url` raw; signed URLs must not enter provenance |
| `_creator_info` unification → `{id, name, profile_page}` | `material.py:64-83` | **PORT_MPT** | `StockCandidate` (`broll.py:112-118`) keeps only `author: str` |
| Secret redaction, incl. `quote_plus` form and every proxy URL | `material.py:192-223` | **PORT_MPT** | `broll.py:245` returns `resp.text[:200]` unredacted |
| Cloudflare-challenge detection before `.json()` | `material.py:226-243` | **PORT_MPT** | `broll.py:137-139` `raise_for_status()` then `.json()` |
| Download validate: exists, size>0, real decode, else delete and return `""` | `material.py:1007-1064` | **PORT_MPT** | A 200-with-garbage currently reaches storage |
| Whitelist provenance reconstruction (only `Path(local_path).name`) | `material.py:86-124` | **MERGE** | Discipline ported onto `MediaAsset.meta_json` |
| Upload: content-vs-extension cross-check, reject image-disguised-as-video | `material_upload.py:91-168` | **REIMPLEMENT** | YMONEY has the primitive (`services/storage.py:96 probe_metadata`) but no gate |
| Atomic `.part` write + `replace` | `broll.py:194-196` | **KEEP_YMONEY** | Better than MPT's direct write (`material.py:1029`) |
| `MediaAsset` lineage / `derivation_json` / workspace-scoped `storage_key` | `models/assets.py:50-59` | **KEEP_YMONEY** | Already stronger |
| Bundled `resource/songs/*.mp3` | `resource/` | **BLOCKED_LICENSE** | Not covered by the MIT grant (code only) |

## 4. Paid-job safety — **highest priority**

MPT has **no** submission state machine. Its safety is a 3-way exception taxonomy inside one process. YMONEY has **no** taxonomy either, and currently writes a deterministic `FAILED` on an ambiguous state.

| Finding | Source | Class | Rationale |
|---|---|---|---|
| 3-way exception taxonomy (`Unconfirmed` / deterministic / `DownloadError`), all carrying the remote ID | `ofox.py:39-60`, `volcengine_seedance.py:32-53`, `metaso_minimax.py:41-56`, `material.py:649-660` | **PORT_MPT** | Maps 1:1 onto the required 7 states |
| **Never auto-retry the billable POST**; 5xx + timeout + unparseable → Unconfirmed; 4xx → deterministic | `ofox.py:258-301` | **PORT_MPT** | `"远端没有创建任务，不存在重复计费风险"` |
| ★ **ConnectTimeout is provably safe to retry; read-timeout and connection-drop are not** | `material.py:1284-1335` | **PORT_MPT** | The most precise statement of the whole problem: a connect timeout proves the request never reached the server |
| Same-`clientRequestId` retry with byte-identical payload (LoomLoom only) | `loomloom.py:382-402` | **PORT_MPT** | Server-side idempotency makes a bounded retry safe without re-buying |
| Halt the whole paid keyword loop on Unconfirmed | `material.py:1866-1875, 1964-1972, 2069-2077, 2182-2191` | **PORT_MPT** | One unconfirmed task must stop all further orders |
| Retry the **download**, never re-submit | `material.py:969-1004, 1202-1240` | **PORT_MPT** | `"重新生成一次远端任务的代价是再付一次费"` |
| Deadline-bounded polling, `remaining/2` phase timeout, linear backoff, counter reset on success | `ofox.py:359-441` | **PORT_MPT** | `MAX_POLL_RETRIES = 5` |
| Unknown status → Unconfirmed, never FAILED | `volcengine_seedance.py:429-433` | **PORT_MPT** | |
| Invalid cost-affecting config **raises**, never silently upcharges | `volcengine_seedance.py:89-100` | **PORT_MPT** | `"无效值不能静默回退到最高默认分辨率"` |
| Paid-plan preflight on a non-billable endpoint before the expensive pipeline | `elevenlabs_music.py:103-178` | **PORT_MPT** | Conclusive failures stop before LLM/TTS/material spend |
| Capability probe: fail closed if the default model is not in the eligible set | `loomloom.py:702-799` | **PORT_MPT** | Live `eligibleModels` read instead of a hardcoded allowlist |
| Streaming download with running byte cap + `os.replace` + explicit `close()` in `finally` | `loomloom.py:905-951` | **PORT_MPT** | Prevents a failed stream draining the connection pool |
| `Video.status` `RENDERING\|READY\|FAILED` → add `SUBMISSION_UNKNOWN` / `SUBMISSION_ATTEMPTED` | `models/content.py:175` | **REIMPLEMENT** | MPT has no DB to port; YMONEY needs the state to persist |
| `production.py:410-411` must not write `FAILED` on the ambiguous branch | `engine/agents/production.py:410` | **REIMPLEMENT** | 🔴 **The engine may have accepted and billed the job.** This is the single highest-value fix |
| `RenderRequest.request_hash()` promoted to the **outbound** idempotency key | `providers/video_engine/base.py:52-76` | **MERGE** | Already a solid key, but only used for a local `ready` check |
| `_resolve_existing` ready/reattach/adopt/fresh + tenant-scoped orphan adoption | `engine/agents/production.py:365-413` | **KEEP_YMONEY** | Genuinely better than anything in MPT |
| `services/jobs.py` idempotency dedupe, atomic claim, exp backoff→DEAD, `recover_orphans`, cancel | `services/jobs.py:76-490` | **KEEP_YMONEY** | Every MPT task primitive is a downgrade |
| `broll._server_generate` — billable POST with `timeout=1800` and zero ambiguity handling | `providers/broll.py:225-267` | **REIMPLEMENT** | An 1800 s timeout on a paid render is exactly the lost-response case |

## 5. Caching

| Finding | Source | Class | Rationale |
|---|---|---|---|
| Cache key = sha256 of canonical JSON, **API key deliberately excluded** | `material_cache.py:117-141` | **PORT_MPT** | `"API Key 只负责鉴权，不影响公开搜索结果"` |
| **Orphaned temp-file reclamation** | `material_cache.py:26-30, 431-432` | **PORT_MPT** | `Ctrl+C`/container-stop/power-loss skip Python cleanup; files accumulate forever. Highest-value item here |
| Atomic write: same-dir temp + `fsync` + `os.replace` | `material_cache.py:355-374` | **PORT_MPT** | |
| TTL on mtime, **negative age treated as invalid** (clock skew / copied file) | `material_cache.py:245-251` | **PORT_MPT** | |
| 256 lock shards + double-checked read → N tasks share one remote request | `material_cache.py:159-172`, `material.py:1624-1630` | **PORT_MPT** | Bounded memory instead of a lock per keyword |
| **Empty results are never cached** | `material.py:1575-1577` | **PORT_MPT** | `[]` overloads "no results" and "request failed" — caching a transient fault for a day is worse |
| Never cache a credential-bound signed URL (Coverr hard-disabled) | `material_cache.py:200-217` | **PORT_MPT** | |
| Re-validate immediately before `unlink`; re-scan at execution, never reuse the preview list | `cache_manager.py:158-196` | **PORT_MPT** | `services/storage.py` + `services/retention.py:377` already have a sweep to extend |
| `UNIQUE(workspace_id, cache_key)` + sha256 provenance key | `models/media_intel.py:179-198` | **KEEP_YMONEY** | Already stronger |

## 6. AI music

| Finding | Source | Class | Rationale |
|---|---|---|---|
| `generate_bgm(video_path, output_path, video_duration, prompt)` contract + clients | `elevenlabs_music.py:363-403`, `sonilo.py:323-357` | **PORT_MPT** | YMONEY has no `MusicIntelligenceProvider` and no generation contract |
| `brand_templates.music_preference` label becomes the `prompt` argument | `models/...brand_templates.py:100-188` | **MERGE** | It is a label today and is never used to select or generate anything |
| 1280 long-edge audio-stripped H.264 proxy, `-fs` capped, deleted in `finally` | `elevenlabs_music.py:194-269` | **PORT_MPT** | Analysis needs pixels, not the HD master |
| Stream → cap → `fsync` → **full ffmpeg decode** → `os.replace` | `elevenlabs_music.py:272-352` | **PORT_MPT** | Sonilo additionally requires a `complete` event so a truncated stream never publishes |
| `music` track kind, `_bgm_for` allowlist, loop+fade, `_music_style` features | `timeline.py:19`, `ffmpeg_avatar.py:301-595` | **KEEP_YMONEY** | Already adopted MPT's BGM technique |
| Graceful degradation (`bgm_file_override = ""` + warning, task survives) | `task.py:946-974` | **PORT_MPT** | |
| Stdin/symlink guards in BGM upload | `bgm.py:90-117, 291-293` | **PORT_MPT** | |
| Sonilo NDJSON multi-stream, pinned `stream_index=0` | `sonilo.py:245-247` | **PORT_MPT** | |

## 7. Semantic video intelligence (TwelveLabs)

| Finding | Source | Class | Rationale |
|---|---|---|---|
| Embed + rerank pattern: opt-in, degrade to input order, refuse a partial rerank | `twelvelabs.py:47-50, 84, 122-124` | **REIMPLEMENT** | Not search, not index management — only embed, rerank, QA. Port the *pattern*, not the SDK |
| Provider seam for rerank | — | **MERGE** | YMONEY `decision.py:342, 380` already has `rerank` / `rerank_batch` with `deterministic`/`local`/`llm_provider` backends |
| `lru_cache(512)` on per-term embeds (N+1 API calls) | `twelvelabs.py:89` | **IGNORE** | YMONEY's `rerank_batch` is the correct target; do not copy the cost shape |
| `media_intel.py:334` "no embedding" | host | **KEEP_YMONEY** | The absence is documented, not accidental |

## 8. Task manager / state

| Finding | Source | Class | Rationale |
|---|---|---|---|
| Atomic artifact write: same-dir temp + `flush` + `fsync` + `os.replace` | `task_artifacts.py:32-53` | **PORT_MPT** | |
| Compare-and-set patch so a late writer cannot clobber a result | `state.py:81-90, 196-202` | **REIMPLEMENT** | Against the DB, not a dict |
| Admission cap with bounded queue + 429, slot reserved pre-thread, rollback on spawn failure | `base_manager.py:24-52` | **REIMPLEMENT** | |
| Threads-only task execution, process-local dict or Redis hash state | `manager/base_manager.py:55`, `services/state.py:46,172` | **IGNORE** | §13 explicit: filesystem/dict state must not become a second source of truth |
| Per-POST uuid with no idempotency | `controllers/v1/video.py:207` | **IGNORE** | |
| `ctypes OpenProcess` process-ownership liveness | `task.py:147-230` | **IGNORE** | Use a lease row |
| `ast.literal_eval` deserialization | `state.py:102-107, 207-228` | **IGNORE** | Antipattern |
| Startup background thread doing an unsigned remote version check | `version_checker.py:166` | **IGNORE** | Supply-chain risk |
| `ensure_project` downloading and executing an unverified GitHub zip | `docs/skill/mpt_agent.py:130-165` | **IGNORE** | Do not port under any circumstances |

## 9. Render / video

| Finding | Source | Class | Rationale |
|---|---|---|---|
| ffmpeg encoder probe + codec fallback | `video.py:303-404` | **PORT_MPT** | Technique only |
| Concat once via ffmpeg instead of per-segment re-encode | `video.py:480-516` | **PORT_MPT** | Same drift class as the TTS concat |
| Blocking-render heartbeat log | `video.py:454-480` | **PORT_MPT** | |
| Material resolution pre-gate before rendering | `video.py:192` | **PORT_MPT** | |
| `random.shuffle` clip choice, unrecorded | `video.py:256-257` | **IGNORE** | Non-reproducible; YMONEY's timeline is the document of record |
| MoviePy render structure | `video.py:18` | **IGNORE** | §13: must not replace `ContentTimeline` |
| Per-frame baked subtitle animation | `video.py:104-180` | **IGNORE** | Baked, not keyframed; `timeline.py:51-80` + `timeline_ops.py` is strictly better |
| `ContentTimeline` source/speed/volume/fade + 17 timeline ops | host `engine/timeline.py:51-80` | **KEEP_YMONEY** | Authoritative |

## 10. CLI + Agent Skill

| Finding | Source | Class | Rationale |
|---|---|---|---|
| Exit taxonomy: 0 ok / 1 task failure / 2 bad args-or-manifest | `cli.py:1537-1576` | **PORT_MPT** | Exactly right for CI |
| Single JSON object on stdout, logs strictly to stderr, UTF-8 forced | `cli.py:1578, 82-87, 1592-1599` | **PORT_MPT** | Keeps stdout parseable by an agent |
| Result-manifest envelope written before work and updated | `mpt_agent.py:555-624` | **PORT_MPT** | `{status, task_id, task_dir, log_file, video_files}` |
| `run_checked`: list args, no `shell=True`, quiet, last-30-lines on failure | `mpt_agent.py:582-595` | **PORT_MPT** | |
| Validators as argparse types | `cli.py:70-174` | **PORT_MPT** | |
| zip-slip guard `_safe_extract` | `mpt_agent.py:120-127` | **PORT_MPT** | |
| MPT command/flag names (`--video-subject`, `--voice-name`, `--stop_at`) | `cli.py` | **IGNORE** | Work order §11 requires a new `ymoney` surface |
| Flat 1449-line single command, no subcommands | `cli.py` | **IGNORE** | |

## 11. API / config / security

| Finding | Source | Class | Rationale |
|---|---|---|---|
| `resolve_path_within_directory` — realpath + `commonpath`, cross-drive safe | `file_security.py:4-35` | **KEEP_YMONEY** | YMONEY's `services/storage.py` already resolves and confines paths; the donor's helper is a downgrade |
| CORS deny-by-default, `allow_credentials = not allow_all_origins` | `asgi.py:82-115` | **PORT_MPT** | Avoids the `*` + credentials reflection footgun |
| Single shared API key; **empty ⇒ auth disabled with a warning** | `asgi.py:26-30` | **IGNORE** | YMONEY must fail closed |
| Key never logged — not even length or digest | `asgi.py:32` | **KEEP_YMONEY** | YMONEY already stronger |
| Config precedence: file-only, no env override except `REDIS_HOST` | `config.py:583-585` | **IGNORE** | YMONEY's `core/config.py` + `project_auth` is better |
| `/system/orphans`, completion verifier wiring | host | **KEEP_YMONEY** | |

## 12. UX

| Finding | Source | Class | Rationale |
|---|---|---|---|
| **No-narration 3-way voice mode** (`tts` / `upload` / `none`) | `Main.py:6355-6400` | **PORT_MPT** | Absent in YMONEY; silent video is a real short-form need |
| **Unmet-upload guard on restore** — a browser cannot repopulate a file input, so block submit until re-supplied | `Main.py:793-837, 7292-7314` | **PORT_MPT** | The best idea in the file; YMONEY has the restore pieces and none of the guard |
| Settings preset export/import with schema+version gate and local-path stripping | `Main.py:2840-2894` | **PORT_MPT** | Credentials excluded by design |
| Voice preview cache + duration estimate + autoplay control | `Main.py:5445-5468, 409-425, 5621-5764` | **PORT_MPT** | YMONEY has the endpoint (`SystemHealth.tsx:49-69`) and none of the intelligence |
| `tr()` fallback chain: active locale → English → raw key | `Main.py:727-734` | **PORT_MPT** | Cheap and prevents drift; 522 keys × 13 locales is a standing burden, so port the function, not the corpus |
| Task history, credential separation, confirm-before-destroy | `Main.py:921-1047, 256-290, 2647-2696` | **KEEP_YMONEY** | YMONEY's allowlist `_MANAGEABLE` is strictly safer than MPT's suffix heuristic |
| MPT settings surface (~50 keys, 20 of them credentials) | `Main.py:225-236` | **IGNORE** | Flattening it into YMONEY would be a regression |
| Batch input (multi-subject paste) | — | **IGNORE** | **Does not exist in MPT.** `video_count` is 1–5 variants of one subject |
| Key backup/restore | — | **IGNORE** | Security downgrade vs. encrypted per-workspace credentials |
| Streamlit architecture | `webui/Main.py` | **IGNORE** | §13 explicit |

## 13. Docker / CI

| Finding | Source | Class |
|---|---|---|
| Multi-stage Dockerfile, GPU variant, compose profiles | `Dockerfile*`, `docker-compose*.yml` | **IGNORE** — YMONEY has its own deployment; no evidence of a gap |
| `coverage fail_under = 70` | `pyproject.toml` | **IGNORE** — YMONEY is far above this |

---

## Summary

| Class | Count of findings |
|---|---|
| `KEEP_YMONEY` | 17 |
| `PORT_MPT` | 47 |
| `MERGE` | 8 |
| `REIMPLEMENT` | 19 |
| `IGNORE` | 22 |
| `BLOCKED_LICENSE` | 7 |

**Integration order chosen** (highest money-safety and latent-correctness value first, surface size last):

1. **Paid-job safety** — the only item that protects real money. New state machine + exception taxonomy + fix the `FAILED`-on-ambiguous branch.
2. **B-roll aspect verification + diversity allocation** — two latent correctness bugs, no schema change.
3. **TTS concat + pause tags + no-voice** — fixes measurable audio drift, adds two capabilities.
4. **LLM `<think>` stripping + redaction + provider registry** — correctness first, breadth second.
5. **Material cache + orphan sweeper + secret redaction.**
6. **AI music provider.**
7. **CLI + agent skill.**
8. **UX** (no-narration mode, restore guard, preset export, preview cache).
