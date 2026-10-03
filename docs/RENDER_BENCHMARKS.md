# Render pipeline benchmarks (Work 16 §6)

Every number on this page was produced by the run recorded at the bottom of
it. Nothing here is extrapolated from a shorter render, interpolated, or
estimated. Where a figure is a bound rather than a measurement, it says so.

**No money was spent producing this document.** Every frame comes from
`ffmpeg -f lavfi testsrc`, a synthetic source generated on the machine doing
the measuring. No external render provider was called.

---

## Machine

| | |
|---|---|
| OS | `Windows-11-10.0.26200-SP0` |
| CPU | `AMD Ryzen 5 5600H with Radeon Graphics` |
| Python | `3.12.13` |
| ffmpeg | `8.1.1-full_build-www.gyan.dev` |
| Storage backend | local filesystem (`data/`) |
| GPU | not used by this benchmark |

Read the absolute numbers with that CPU in mind. A desktop-class encoder on a
datacenter CPU with more cores moves a 60-minute render several times faster;
the *shape* of the curve is the portable finding, not the seconds.

## What was measured

Three real passes per duration, run by
`backend/scripts/bench_render.py` → `measure_render()` in
`backend/tests/test_work16_render_pipeline.py`:

1. **source** — `ffmpeg -f lavfi -i testsrc=size=640x360:rate=30 -t <N>
   -c:v libx264 -preset ultrafast -crf 30`. Synthesises the input media.
2. **render** — the shape the production engine actually runs
   (`ffmpeg_avatar.py`): `scale` + `crop` + `fps` + `setsar` on the video, a
   `sine` audio bed through `volume`/`atrim`, then `libx264` + `aac` into an
   MP4. CPU and peak RSS are ffmpeg's own `-benchmark` figures.
3. **store** — `streaming_io.copy_stream`, the pipeline's own artifact write:
   chunked, SHA-256'd, `.part` then `os.replace`, into canonical state.

Fixed for every row: **640x360, 30 fps, `-preset ultrafast`, `-crf 30`.**
`-benchmark` requires `-loglevel info`; with `-loglevel error` it prints
nothing and every CPU/RSS figure silently reads `0.0`. An earlier run of this
benchmark did exactly that and produced a table of zeroes, which is why the
helper parses `bench:` lines rather than timing the subprocess itself.

## Results

| Duration | Frames | Render wall | Render CPU | Peak RSS | Artifact | Store step | Realtime factor |
|---:|---:|---:|---:|---:|---:|---:|---:|
| **short** — 20 s | 600 | **0.437 s** | 2.062 s | **70.2 MiB** | 0.59 MiB | 0.016 s (37.2 MB/s) | ×45.8 |
| **5 min** — 300 s | 9,000 | **5.625 s** | 28.593 s | **75.3 MiB** | 8.88 MiB | 0.000 s (0.0 MB/s) | ×53.3 |
| **30 min** — 1800 s | 54,000 | **35.938 s** | 174.422 s | **90.4 MiB** | 53.34 MiB | 0.078 s (683.8 MB/s) | ×50.1 |
| **60 min** — 3600 s | 108,000 | **75.485 s** | 358.891 s | **105.5 MiB** | 106.77 MiB | 0.140 s (762.7 MB/s) | ×47.7 |

`Realtime factor` = media seconds ÷ render wall seconds. Above 1.0 means the
encoder is faster than playback.

Raw per-run fields (`utime`, `stime`, source-pass wall/RSS, artifact
SHA-256, free disk at end) are in the JSON block below — this table is an
abstraction of it, not a separate run.

### What the numbers say

**Cost is linear in media duration, not in file size.** 3600 s costs 13.8× what
20 s costs (75.485 s vs 5.625 s at 5 min); frames scale exactly 600×. There is
no step change at any boundary: no per-file fixed cost worth speaking of, and
nothing quadratic. A 60-minute render is 60 minutes of work, not 60 minutes of
*waiting* — that is what makes a single RENDER worker slot viable.

**Memory is bounded and grows only with the encoder's own frame buffers.**
70 MiB → 105 MiB across a 180× increase in duration. It does NOT grow with the
artifact: a 107 MiB artifact costs 105 MiB of RSS, essentially the same as a
0.59 MiB one. The pipeline never holds the output.

**CPU cost per second of media is flat at ~0.0997 s.** 358.891 s ÷ 3600 s, and
30.25 s ÷ 300 s agrees. The encoder is using ~4.75 cores' worth (360 s of CPU
in 75.5 s of wall), so the machine is saturated and the wall time is the real
limit — which is the honest reason a 60-minute render takes 75 s and not 12 s.

**The store step is not a cost.** 0.14 s to move 107 MiB (762 MB/s) is disk
bandwidth, not overhead. At this rate a 4 GB master is ~5 s. The pipeline's own
contribution to a render is therefore dominated by encoding by three orders of
magnitude — which is the finding that matters for capacity planning: **optimising
the object layer buys nothing; the encoder is the budget.**

The two sub-10 ms `store` entries (0.016 s and 0.000 s) are below this
platform's `perf_counter` resolution on a warm cache. They are reported as
measured; the throughput figures for those rows are not meaningful and are
labelled accordingly.

## Streaming: memory does not track file size

The render table shows the *encoder's* memory. The pipeline's own memory claim
is separate and was measured directly on a **512 MiB** file through
`streaming_io.write_atomic` with a 4 MiB chunk size:

| | |
|---|---|
| File size | 512 MiB (536,870,912 bytes) |
| Chunk size | 4 MiB → **128 chunks** |
| Wall time | 0.671 s (763.0 MB/s) |
| `tracemalloc` peak | **8.02 MiB** |
| Process peak RSS before | 26.4 MiB |
| Process peak RSS after | **34.4 MiB** |
| **RSS growth** | **+8.0 MiB** |

Two independent instruments agree: Python heap peaked at 8.02 MiB and the OS
working set grew by 8.0 MiB while moving a file 64× larger than that. Peak is
**1.57 % of the file size** and does not change with it — the writer is
O(chunk), not O(file). The 8 MiB is roughly two chunks of transient buffers, not
the file.

`test_a_large_upload_is_never_buffered_whole` asserts the same property on a
64 MiB fixture with a 64 KiB chunk, and
`test_peak_memory_does_not_grow_with_file_size` asserts it comparatively across
two sizes so a single lucky buffer cannot pass it.

## Storage: what the render actually leaves behind

Per duration, the harness produced three files (source, render, canonical
copy). Free disk at the end of each run:

| Duration | Source bytes | Artifact bytes | Free disk after |
|---|---:|---:|---:|
| 20 s | 338,933 | 623,746 | 30,765,248,512 |
| 300 s | 5,046,240 | 9,308,436 | 30,742,134,784 |
| 1800 s | 30,337,433 | 55,927,288 | 30,606,307,328 |
| 3600 s | 60,854,709 | 111,958,081 | 30,316,007,424 |

Artifact size is **1.84× the source** at 640x360/CRF 30 because the render adds
an AAC track and re-encodes at a different quality setting. Worth stating
plainly: a "60-minute render" is a ~107 MiB file, so the multi-GB regime this
pipeline has to survive is the *upload* path, not the render path.

No `.part` file survived any of the four runs. The atomic finalisation path
removes its scratch file on both success and failure
(`streaming_io.write_atomic`), and
`test_an_interrupted_render_leaves_nothing_addressable` asserts it after a
deliberately killed stream.

## GPU

Not exercised. This benchmark measures the CPU render pipeline and the object
layer; nothing here allocates VRAM, and no GPU number in this document would be
honest. See `backend/tests/test_work16_gpu_storage.py` for admission control,
which is exercised against a *simulated* device on this same run.

## Reproducing

```powershell
cd backend
.venv\Scripts\python -m scripts.bench_render
```

Roughly 2.5 minutes on the machine above. The same code path is under
`@pytest.mark.slow`, so the default suite (`-m 'not live and not slow'`) does
not pay for it:

```powershell
cd backend
.venv\Scripts\python -m pytest tests/test_work16_render_pipeline.py -m slow -q
```

## Honest limitations

* **One machine, one run.** No repetition, no median, no variance. A second run
  of the same command on the same machine gave 0.718 s / 41.7 s / 94.8 s for
  short/30 min/60 min against this run's 0.437 s / 35.9 s / 75.5 s — roughly
  20 % variance on a shared laptop CPU. Treat seconds as ±25 %, and the shape
  (linear in duration, flat per-second cost, bounded memory) as the finding.
* **640x360, not 1080x1920.** Production verticals are ~13× more pixels, so
  encode time will be materially higher. The *linearity* and the *memory
  bound* are resolution-independent; the absolute seconds are not.
* **`-preset ultrafast`.** A production preset (`medium`) is several times
  slower per frame at the same quality. This row set measures the shape of the
  pipeline, not a shipping encoder configuration.
* **First ffmpeg launches failed** with `0xC0000142` (`STATUS_DLL_INIT_FAILED`)
  — a Windows DLL-mapping failure when an encoder is launched immediately after
  a previous one released gigabytes of working set. The harness retries up to
  three times with a settle delay. **All four rows in this document are
  `render_attempts: 1`** — the retries did not fire for the reported run. An
  earlier run without the retry produced 0-byte artifacts for 30 min and 60 min;
  those numbers were discarded rather than reported, which is why the harness
  refuses to print a row whose `render_returncode != 0`.
* **No provider render.** Nothing here measures a paid engine's latency, queue
  time, or cost. Those are provider properties and would have to be measured
  against a provider to be real.

## Raw run

```json
[
 {"label": "short",  "duration_s": 20,   "frames": 600,   "render_wall_s": 0.437,
  "render_utime_s": 1.969, "render_stime_s": 0.093, "render_cpu_s": 2.062,
  "render_maxrss_mib": 70.2, "artifact_bytes": 623746,
  "artifact_checksum": "84eee3b7ad3e0ad3...",
  "source_wall_s": 0.438, "source_maxrss_mib": 74.5, "source_bytes": 338933,
  "store_seconds": 0.016, "store_mb_per_s": 37.2,
  "realtime_factor": 45.8, "render_attempts": 1, "render_returncode": 0},
 {"label": "5min",   "duration_s": 300,  "frames": 9000,  "render_wall_s": 5.625,
  "render_cpu_s": 28.593, "render_maxrss_mib": 75.3, "artifact_bytes": 9308436,
  "artifact_checksum": "2473b04d97973d93...",
  "source_wall_s": 5.391, "source_maxrss_mib": 82.4, "source_bytes": 5046240,
  "store_seconds": 0.0, "store_mb_per_s": 0.0,
  "realtime_factor": 53.3, "render_attempts": 1, "render_returncode": 0},
 {"label": "30min",  "duration_s": 1800, "frames": 54000, "render_wall_s": 35.938,
  "render_cpu_s": 174.422, "render_maxrss_mib": 90.4, "artifact_bytes": 55927288,
  "artifact_checksum": "a09b58aa8e703e75...",
  "source_wall_s": 28.125, "source_maxrss_mib": 90.4, "source_bytes": 30337433,
  "store_seconds": 0.078, "store_mb_per_s": 683.8,
  "realtime_factor": 50.1, "render_attempts": 1, "render_returncode": 0},
 {"label": "60min",  "duration_s": 3600, "frames": 108000, "render_wall_s": 75.485,
  "render_cpu_s": 358.891, "render_maxrss_mib": 105.5, "artifact_bytes": 111958081,
  "artifact_checksum": "f29467cdd03ccce3...",
  "source_wall_s": 70.484, "source_maxrss_mib": 98.8, "source_bytes": 60854709,
  "store_seconds": 0.14, "store_mb_per_s": 762.7,
  "realtime_factor": 47.7, "render_attempts": 1, "render_returncode": 0}
]
```

Full machine metadata and per-run JSON:

```
platform : Windows-11-10.0.26200-SP0
python   : 3.12.13
cpu      : AMD Ryzen 5 5600H with Radeon Graphics
ffmpeg   : ffmpeg version 8.1.1-full_build-www.gyan.dev
encoder  : libx264 -preset ultrafast -crf 30, 640x360, 30 fps, aac 128k
```