# Media intelligence license audit (Work 12)

Audit date: **2026-09-29**. Scope: every optional media-intelligence backend
Work 12 may invoke, plus the ffmpeg-based local providers.

**Method.** Code licenses were read from each repository's own `LICENSE` /
`COPYING` file (SPDX identifier + URL). Model/weights terms were read from the
model card / hub metadata — never inferred from the host project's code
license. Anything not confirmable from an authoritative source is recorded as
`UNVERIFIED` and blocks commercial clearance. This file is the human-readable
record; the machine-readable values live in each adapter's `license_info()` and
are enforced by `engine/intel/registry.py` (commercial mode refuses any
provider whose `commercial_use != "PERMITTED"`).

**Enforcement rules (contracts §1.4).**

1. `code_license` and `model_license` are reported separately. A permissive
   code license never implies permissive weights.
2. `commercial_use ∈ PERMITTED | REVIEW_REQUIRED | PROHIBITED | UNVERIFIED`.
3. A workspace in commercial mode (`settings.commercial_mode = true`) cannot
   resolve a provider whose `commercial_use != PERMITTED`; the reason is
   surfaced to the API and UI rather than silently ignored.
4. `PROHIBITED` is a hard block in every mode (non-commercial terms may not be
   used in a commercial product at all).
5. Gated models are an *operational* dependency: access needs a click-through
   acceptance, an HF token, and (pyannote) sharing contact information. CI
   without a token must degrade honestly, never fake it.

## Verdicts

| Component (provider key) | Code license | Model/weights terms | Gated | `commercial_use` | Source |
|---|---|---|---|---|---|
| `whisperx` (alignment) — code | **BSD-2-Clause** | per-language aligner, see below | no | `REVIEW_REQUIRED` (bundled VAD weights `UNVERIFIED`) | [LICENSE](https://raw.githubusercontent.com/m-bain/whisperX/main/LICENSE) |
| ↳ aligner `en` = torchaudio `WAV2VEC2_ASR_BASE_960H` | BSD-3 (torchaudio) | **MIT** — "the license applies to the pre-trained models as well" | no | `PERMITTED` | [impl.py](https://github.com/pytorch/audio/blob/main/src/torchaudio/pipelines/_wav2vec2/impl.py), [fairseq](https://github.com/pytorch/fairseq/blob/main/README.md) |
| ↳ aligners `fr,de,es,it` = torchaudio `VOXPOPULI_ASR_BASE_10K_*` | BSD-3 | **CC BY-NC 4.0 — NON-COMMERCIAL** | no | **`PROHIBITED`** for commercial use | [impl.py](https://github.com/pytorch/audio/blob/main/src/torchaudio/pipelines/_wav2vec2/impl.py) |
| ↳ aligner, other languages (HF `DEFAULT_ALIGN_MODELS_HF`) | per-model | per-model; only `jonatasgrosman/wav2vec2-large-xlsr-53-japanese` checked = `apache-2.0` | no | `UNVERIFIED` per language — audit each language that ships | [whisperX alignment.py](https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py) |
| ↳ whisperX default VAD `assets/pytorch_model.bin` | BSD-2 | **`UNVERIFIED`** — binary committed with no model card; a BSD-2 code repo does not license its weights | bundled | `UNVERIFIED` | [vads/pyannote.py](https://github.com/m-bain/whisperX/blob/main/whisperx/vads/pyannote.py) |
| ↳ whisperX ASR (openai-whisper / faster-whisper) | MIT | **MIT** — "code and model weights are released under the MIT License" | no | `PERMITTED` | [whisper README](https://github.com/openai/whisper/blob/main/README.md) |
| `pyannote` (diarization) — code | **MIT** | see below | yes (all) | `REVIEW_REQUIRED` (component mix not fully verifiable) | [LICENSE](https://raw.githubusercontent.com/pyannote/pyannote-audio/develop/LICENSE) |
| ↳ `speaker-diarization-3.1` / `-3.0` + `segmentation-3.0` | MIT | **MIT**; gated: accept terms + share contact info | yes | `REVIEW_REQUIRED` (gated; `config.yaml` contents unverified) | [3.1 card](https://huggingface.co/pyannote/speaker-diarization-3.1), [segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0) |
| ↳ 3.x embedding `wespeaker-voxceleb-resnet34-LM` | MIT | **CC-BY-4.0** — attribution required | no | attribution obligation | [wespeaker card](https://huggingface.co/pyannote/wespeaker-voxceleb-resnet34-LM) |
| ↳ `speaker-diarization-community-1` (whisperX default) | MIT | **CC-BY-4.0** — attribution required | yes | attribution obligation; sub-model terms inferred only | [community-1 card](https://huggingface.co/pyannote/speaker-diarization-community-1) |
| ↳ legacy `pyannote/embedding` | MIT | **MIT** | yes | gated | [card](https://huggingface.co/pyannote/embedding) |
| `mediapipe` (face tracking) — code | **Apache-2.0** | `face_landmarker.task` / `.tflite` bundles: **`UNVERIFIED`** — no terms published at the GCS download URL; Kaggle's official MediaPipe org lists Apache-2.0 for the TF.js face models (secondary source only) | no | `REVIEW_REQUIRED` | [LICENSE](https://raw.githubusercontent.com/google-ai-edge/mediapipe/master/LICENSE), [Kaggle face-detection](https://www.kaggle.com/models/mediapipe/face-detection) |
| `sam2` (segmentation) | **Apache-2.0** | **Apache-2.0** — "SAM 2 model checkpoints … licensed under Apache 2.0"; HF `facebook/sam2-hiera-*` cards `apache-2.0`, `gated: false` | no | **`PERMITTED`** (fully cleared) | [LICENSE](https://raw.githubusercontent.com/facebookresearch/sam2/main/LICENSE), [README](https://github.com/facebookresearch/sam2/blob/main/README.md), [HF card](https://huggingface.co/facebook/sam2-hiera-tiny) |
| `rnnoise` (denoise) | **BSD-3-Clause** | weights are compiled into `src/rnn_data.c` and covered by the same `COPYING` (no separate affirmative weights statement) | no | `PERMITTED` (note: no standalone weights grant) | [COPYING](https://github.com/xiph/rnnoise/blob/master/COPYING) |
| `silero-vad` (optional VAD) | **MIT** | **MIT** — weights ship in-repo; ⚠ README badge alt-text wrongly says CC BY-NC 4.0 while the badge, link and LICENSE say MIT (LICENSE governs) | no | `PERMITTED` | [LICENSE](https://github.com/snakers4/silero-vad/blob/master/LICENSE) |
| `webrtcvad` (optional VAD) | MIT wrapper + **BSD-3** WebRTC | no ML model | no | `PERMITTED` | [LICENSE](https://github.com/wiseman/py-webrtcvad/blob/master/LICENSE) |
| ffmpeg-based local providers (`ffmpeg` speech-activity, enhancement, motion/reframe) | LGPL-2.1-or-later core; build-dependent components may be **GPL-2.0-or-later** (this host uses a *full* build) | n/a | no | `REVIEW_REQUIRED` — invoked as an **external process** (not linked), so the operator's own build/distribution obligations apply; confirm before redistributing a binary | ffmpeg upstream |
| `opencv` Haar cascades (considered, **not used**) | **per-file embedded terms**, *not* Apache-2.0 (`haarcascade_frontalface_alt.xml` = Intel license; `haarcascade_smile.xml` = contributor CLA); OpenCV warns some classifiers are "special license" | cascades ship with those terms | no | `REVIEW_REQUIRED` per file — reason Work 12 does **not** use a Haar fallback | [data/readme.txt](https://github.com/opencv/opencv/blob/4.x/data/readme.txt) |
| `opencv-python` wheels (considered, **not used**) | wrapper MIT; OpenCV Apache-2.0; wheels bundle **LGPLv2.1** FFmpeg (Qt LGPLv3/GPLv3 in non-headless) | n/a | no | `REVIEW_REQUIRED` for redistribution | [README §Licensing](https://github.com/opencv/opencv-python/blob/master/README.md) |
| numpy / scipy / librosa / soundfile (not installed, not used) | BSD-3 / BSD-3 / ISC / BSD-3 | n/a | no | n/a — Work 12 adds **no** dependency | PyPI metadata |

## Findings that change engineering decisions

1. **Non-commercial blocker.** whisperX's *default* aligners for `fr, de, es,
   it` are `CC BY-NC 4.0`; only `en` is MIT. A commercial deployment must
   refuse those languages, or replace the aligner. The adapter therefore
   carries a **per-language aligner license map** and the registry blocks
   `PROHIBITED` languages in commercial mode.
2. **Gated ≠ free to ignore.** Every pyannote weight is `gated: auto`
   (click-through + contact info), which makes headless CI token-dependent and
   turns gating into a contract obligation, not a download hurdle.
3. **CC-BY-4.0 is a shipping obligation.** `community-1` and
   `wespeaker-voxceleb-resnet34-LM` allow commercial use *with mandatory
   attribution* — credits/notice obligations in the product and in distributed
   content.
4. **Bundled-binary trap.** whisperX's default VAD weights have no documented
   provenance or license; a permissive code repo does not license them.
5. **Model files are not uniform even inside one permissive project.** MediaPipe
   code is Apache-2.0 while its `.task` bundles publish no terms; OpenCV is
   Apache-2.0 while its cascade XMLs carry Intel/contributor terms.
6. **SAM2 is the only component cleared end-to-end** (code + ungated
   Apache-2.0 checkpoints) — a usable commercial segmentation path.
7. **RNNoise** weights are covered by inclusion under BSD-3 (`rnn_data.c`) with
   no separate affirmative grant; low risk, but noted rather than smoothed over.
8. **CI installs none of these.** The Work 12 venv has zero ML packages, so
   every provider is exercised through its honest-unavailable path; no license
   gate is bypassed by tests.

## Unverified — must be closed before commercial use

| # | Item | Action |
|---|---|---|
| 1 | whisperX bundled VAD `assets/pytorch_model.bin` (origin + license) | ask upstream (m-bain) for provenance; sha256-pin the file; or switch the VAD to silero (MIT) |
| 2 | `pyannote/speaker-diarization-3.0/3.1` gated `config.yaml` component list | accept the gate with an org account, read `config.yaml`, re-verify each component's terms |
| 3 | `community-1` sub-model licenses (segmentation / embedding / plda) | read the gated repo metadata directly instead of relying on repo-level `license:` |
| 4 | MediaPipe `face_landmarker.task` / `face_detection_*.tflite` | obtain written terms from Google's model page; the Kaggle TF.js listing is a secondary source |
| 5 | Every non-en whisperX aligner that a deployment will actually use | audit per language (`DEFAULT_ALIGN_MODELS_HF`), not as a set |
| 6 | ffmpeg build license obligations for the operator's distribution | operator decision; record the build's `--enable-gpl`/LGPL configuration |
| 7 | OpenCV cascade XML headers (not used by Work 12) | read each file's header if cascades are ever added |

## Clearance log

- 2026-09-29 — initial audit. `PERMITTED`: SAM2 (code+weights), whisperX `en`
  aligner, whisper ASR weights, RNNoise (+weights-by-inclusion), Silero VAD,
  webrtcvad. `REVIEW_REQUIRED`: whisperX (bundled VAD unverified), pyannote
  (gated + partially unverified component mix), MediaPipe (`.task` bundle
  unverified), all ffmpeg-based local providers (operator distribution
  decision). `PROHIBITED` for commercial use: whisperX `fr/de/es/it` VoxPopuli
  aligners (CC BY-NC 4.0).
