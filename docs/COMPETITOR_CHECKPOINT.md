# Competitor Checkpoint — YMONEY vs the best platforms (Sept 2026)

**Prices verified:** OpusClip $15–29/mo (free: 60 min/mo, watermarked, clips expire in 3 days) ·
Pictory $19+/mo (3 free projects) · HeyGen $24+/mo (free: 3 videos/mo, watermarked)
· InVideo $15+/mo. Free LLM: OpenRouter `:free` ($0, no card, 50–200 req/day;
1,000/day after a $10 top-up) · Google AI Studio (Gemini Flash free tier).

## 1. Feature matrix (✅ have · ◐ partial · ❌ don't have)

| Capability | OpusClip $15+ | HeyGen $24+ | Pictory $19+ | InVideo $15+ | **YMONEY ($0 stack)** |
|---|---|---|---|---|---|
| Long → viral shorts + virality score | ✅ core | ❌ | ◐ highlights | ❌ | ✅ moments + score/hook/reason |
| Script → video (stock + voice) | ❌ | ✅ Video Agent | ✅ core | ✅ core | ✅ ffmpeg_avatar + MPT lane |
| AI avatars / talking heads | ❌ | ✅ best-in-class | ◐ clones | ❌ | ◐ SadTalker/server lanes (setup) |
| AI B-roll generation | ◐ image/stock | ✅ credits | ✅ credits | ✅ Sora/Veo | ◐ lane built (needs GPU/server) |
| Voice clone + TTS | ❌ | ✅ 175+ langs | ◐ ElevenLabs | ◐ | ✅ Chatterbox/Qwen3/edge/kokoro free |
| Dubbing / translation | ◐ 20+ langs | ✅ lip-synced | ◐ 29 langs | ❌ | ✅ pipeline (no lip-sync warp) |
| Animated captions | ✅ 20+ langs | ✅ | ✅ | ✅ | ◐ 3 styled presets (not word-pop) |
| Filler/silence removal | ✅ | ❌ | ❌ | ❌ | ◐ detect only, no strip |
| Auto-post TikTok/IG/YT/FB | ✅ | ❌ | ◐ Hootsuite | ❌ | ✅ direct APIs + relay |
| Scheduler + calendar | ✅ | ❌ | ❌ | ❌ | ✅ sweep + best-times + auto-fill |
| Trend discovery → produce | ❌ | ❌ | ❌ | ❌ | ✅ 8 sources + scoring + decision WHY |
| Learning loop (performance → strategy) | ❌ | ❌ | ❌ | ❌ | ✅ patterns + fatigue + memory |
| Brand kits | ✅ templates | ✅ | ✅ | ✅ 10k | ◐ white-label chrome + templates (no render-kit fonts) |
| Team seats / roles | ✅ | ✅ | ✅ | ✅ | ◐ roles exist, no invites |
| Public API / MCP | ◐ limited | ✅ | ✅ MCP | ❌ | ❌ |
| Mobile app | ✅ | ✅ | ❌ | ✅ | ❌ |
| Compliance gates (AI/finance disclosure, QC) | ❌ | ❌ | ❌ | ❌ | ✅ officer + audit export |
| Cost to create + upload | $15+/mo | $24+/mo | $19+/mo | $15+/mo | **$0 (below)** |

**Moat (nobody else has):** autonomous FIND→LEARN loop with explainable decisions,
learning + fatigue, persistent memory, compliance gates — all self-hosted.

## 2. HAVE vs DON'T HAVE checkpoint

### HAVE (production, tested)
Trend discovery (8 sources) · scoring v2 + virality · Decision WHY · research +
claims · strategy/scripts/variants/hook-rank · title A/B + comment packs ·
ffmpeg_avatar + MPT + motion cards + avatar/sadtalker/wavlip lanes · 13-dim QC ·
SEO/disclosures · YT/TikTok/FB/IG direct + relay (chunked, polling, quota) ·
4 analytics providers · learning + fatigue + memory · scheduler + best-times ·
assets/uploads/covers/thumbnails · captions presets · BGM allowlist ·
link-to-shorts + dubbing + broll + template registry · approval hold ·
compliance gate + audit export · SSE + Telegram · 4-platform OAuth ·
white-label brand kits · Redis/PG/GPU scale path · 22 agents.

### DON'T HAVE (honest gaps, ordered by value)
1. **Public API / MCP** — blocks integrations, n8n/Zapier parity.
2. **Word-pop animated captions** — presets are static styles.
3. **Silence/filler strip** — detected, never cut.
4. **Lip-synced dubbing warp** — dub replaces audio, mouth doesn't follow.
5. **Render brand kits** (fonts/colors/watermark burned in) — chrome-only today.
6. **Team invites** — roles exist, no invite flow.
7. **Mobile companion** — desktop-first responsive only.
8. **Bulk/CSV import, Spark-Ads hooks, affiliate links** — monetization extras.

## 3. The $0 stack — free to create and upload

Rule: **only the LLM may ever cost money — and even it has a $0 path.**
Everything else is free or optional, with paid tiers strictly opt-in.

| Step | $0 path (default) | Paid/advanced opt-in |
|---|---|---|
| LLM (scripts/research/SEO/QC) | OpenRouter `:free` / Google AI Studio / local Ollama via OpenAI-compatible base URL (Settings → Connections) | OpenAI/Anthropic keys, $10 OpenRouter top-up → 1,000 req/day |
| Trend discovery | Google Trends RSS, Reddit, HN, CoinGecko, Dev.to, channel RSS (keyless) | NewsData.io key, YouTube API key (cheap reads) |
| Voice | Edge TTS (free, no key), Kokoro local, Chatterbox/Qwen3 self-hosted | ElevenLabs, hosted TTS APIs |
| Visuals | Pexels free key, AI images via free endpoints, uploads | Stock subscriptions, GPU cloud for Wan/LTX |
| Render | ffmpeg_avatar local (CPU), motion cards (HyperFrames free) | GPU box, MPT service, HeyGen cloud |
| Captions/subtitles | faster-whisper local, SRT burn-in | — |
| Music | Local `data/bgm/` allowlist | Licensed libraries |
| Publish | Official free APIs (YT 100 uploads/day bucket, TikTok/FB/IG free) | Upload-Post paid tiers (free 10/mo), Hootsuite-style tools |
| Analytics | Platform free APIs | — |
| Hosting | Single VPS / home server, SQLite, local queue | Postgres + Redis + S3 + Sentry (all have free tiers/self-host) |

**What "advanced experience" unlocks (all optional):** GPU box (8 GB+: Wan B-roll,
SadTalker, Chatterbox-native) → 10× render quality/speed; $10 OpenRouter top-up →
20× LLM throughput; relay paid plan → hands-off multi-platform posting volume;
object storage + CDN → Instagram-direct without public base URL workarounds.
The autonomous loop itself — decide, produce, QC, publish, learn — runs fully
on the $0 column.
