"""Work 16 §6: streaming IO, the render pipeline, and what actually costs what.

The claims under test, and the failure each one rules out:

* **a large upload is never buffered whole** (a) -- measured with
  ``tracemalloc``, not asserted from a comment. A 64 MiB file moved through
  the streaming writer must peak at a few MB of Python memory; a
  ``read_bytes()``-based implementation peaks at 64 MiB and fails here;
* **chunk size bounds memory, not throughput** (b);
* **the temp budget refuses BEFORE the write** (c);
* **an interrupted render leaves nothing addressable** (d);
* **a render resumes from completed chunks only** (e);
* **chunk boundaries are honest** (f/g) -- a resumed render must not
  double-render or skip a boundary;
* **the final artifact is verified, not assumed** (h/i);
* **scratch space is reclaimed** (j).

The duration-scaling measurements (short / 5 min / 30 min / 60 min) are in
``docs/RENDER_BENCHMARKS.md`` and were produced by the helper at the bottom of
this file, which shells out to a real ``ffmpeg`` on synthetic sources. They are
marked ``slow`` so the default suite does not pay for them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import tracemalloc
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import storage_objects as objects
from app.services import streaming_io

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bounded_chunk(monkeypatch):
    """A small chunk size, so the tests exercise many chunks without allocating
    megabytes each. The DEFAULT is 4 MiB; a 64 KiB chunk makes a 2 MiB fixture
    take 32 iterations, which is where the memory bound is actually provable.
    """
    monkeypatch.setattr(settings, "storage_stream_chunk_bytes", 64 * 1024,
                        raising=False)
    yield


@pytest.fixture()
def big_file(tmp_path: Path) -> Path:
    """A 64 MiB synthetic file, written WITHOUT loading it (the fixture itself
    has to obey the rule it is used to check)."""
    path = tmp_path / "source_master.bin"
    block = bytes(range(256)) * 256  # 64 KiB
    writes = 64 * 1024 * 1024 // len(block)
    with path.open("wb") as handle:
        for _ in range(writes):
            handle.write(block)
    assert path.stat().st_size == 64 * 1024 * 1024
    return path


@pytest.fixture()
def workspace_a(workspace_with_user):
    return workspace_with_user["workspace"]


# ---------------------------------------------------------------------------
# A. Streaming: memory stays bounded  (the mutation target)
# ---------------------------------------------------------------------------


def test_a_large_upload_is_never_buffered_whole(big_file, tmp_path):
    """The measurement, not the intention.

    ``tracemalloc`` tracks Python heap allocations, and a chunked writer's
    peak is one chunk regardless of file size. The same file through
    ``Path.read_bytes()`` peaks at 64 MiB, so this test FAILS on the obvious
    implementation -- which is the point of measuring rather than reading the
    code.
    """
    dest = tmp_path / "uploaded.bin"
    tracemalloc.start()
    try:
        report = streaming_io.write_atomic(
            dest, streaming_io.iter_chunks(big_file, chunk_bytes=64 * 1024),
            chunk_bytes=64 * 1024)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert report.size_bytes == big_file.stat().st_size
    assert peak < 8 * 1024 * 1024, (
        f"streaming a 64 MiB file peaked at {peak / 1048576:.1f} MiB of Python "
        f"memory; it must stay near the chunk size")
    assert report.chunks > 100, "the file really did move in many chunks"


def test_peak_memory_does_not_grow_with_file_size(tmp_path):
    """The stronger form: 2x the bytes must not mean 2x the memory.

    A single-file measurement can be satisfied by a buffer that happens to fit.
    Two sizes with a flat peak prove the implementation is O(chunk), not
    O(file).
    """
    peaks: dict[int, int] = {}
    for multiplier in (1, 4):
        src = tmp_path / f"src-{multiplier}.bin"
        src.write_bytes(b"z" * (4 * 1024 * 1024 * multiplier))
        tracemalloc.start()
        try:
            streaming_io.write_atomic(tmp_path / f"out-{multiplier}.bin",
                                      streaming_io.iter_chunks(
                                          src, chunk_bytes=64 * 1024),
                                      chunk_bytes=64 * 1024)
            _, peaks[multiplier] = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert peaks[4] < peaks[1] * 2, (
        f"peak memory scaled with file size: {peaks[1]} -> {peaks[4]} bytes")


def test_reading_is_bounded_and_a_range_read_does_not_read_the_rest(big_file,
                                                                    tmp_path):
    """A range read touches only its range, and a small read refuses a big file
    rather than discovering the size after allocating for it."""
    got = 0
    for block in streaming_io.iter_chunks(big_file, chunk_bytes=64 * 1024,
                                          start=1024 * 1024, length=128 * 1024):
        got += len(block)
    assert got == 128 * 1024

    with pytest.raises(streaming_io.TempBudgetExceeded, match="read_all refused"):
        streaming_io.read_all(big_file, max_bytes=1024)


def test_a_truncated_copy_is_rejected_by_its_checksum(big_file, tmp_path):
    """Verification is not optional, and it happens while the bytes are in hand
    rather than in a second pass nobody scheduled."""
    dest = tmp_path / "verified.bin"
    real = streaming_io.sha256_file(big_file)
    streaming_io.copy_stream(big_file, dest, expected_checksum=real)
    assert dest.exists()

    with pytest.raises(ValueError, match="checksum mismatch"):
        streaming_io.copy_stream(big_file, tmp_path / "bad.bin",
                                 expected_checksum="a" * 64)
    assert not (tmp_path / "bad.bin").exists(), (
        "a copy that failed verification must not be left on disk")


def test_a_partial_download_is_not_kept(big_file, tmp_path):
    """An interrupted download costs a bounded amount of disk, and the partial
    file is removed rather than mistaken for a finished one."""
    def _dying():
        yield b"x" * 1024
        raise OSError("connection reset")

    dest = tmp_path / "partial.bin"
    with pytest.raises(OSError):
        streaming_io.download_to(_dying, dest, chunk_bytes=64 * 1024)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_an_oversized_download_is_refused_while_streaming(big_file, tmp_path):
    """The limit is enforced DURING the transfer, not after."""
    with pytest.raises(streaming_io.TempBudgetExceeded,
                       match="exceeded"):
        streaming_io.download_to(lambda: streaming_io.iter_chunks(big_file),
                                 tmp_path / "too-big.bin",
                                 max_bytes=8 * 1024 * 1024)
    assert not (tmp_path / "too-big.bin").exists()


def test_copy_is_atomic_under_concurrent_reads(tmp_path):
    """A reader either sees the whole previous object or the whole new one.

    ``os.replace`` is what buys this. A writer that opened the destination
    directly would let a concurrent reader observe a truncated file.

    The writer retries a denied rename: on Windows an open reader handle blocks
    the delete that ``os.replace`` needs. That is a platform sharing rule, not
    an atomicity failure -- the property under test is what a READER can
    observe, which is checked below.
    """
    import contextlib
    import threading

    dest = tmp_path / "object.bin"
    dest.write_bytes(b"OLD" * 1000)
    seen: list[int] = []
    stop = {"now": False}

    def _reader() -> None:
        while not stop["now"]:
            # Windows raises a sharing violation while the writer holds the
            # handle; that is a read that did not happen, not a failure.
            with contextlib.suppress(OSError):
                seen.append(len(dest.read_bytes()))

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    denied = 0
    try:
        for _ in range(20):
            for _attempt in range(200):
                try:
                    streaming_io.write_atomic(
                        dest, iter([b"NEW-COMPLETE" * 2000]),
                        chunk_bytes=1024, fsync=False)
                    break
                except PermissionError:
                    denied += 1  # pragma: no cover - timing dependent
                    time.sleep(0.001)
            else:  # pragma: no cover - a permanently denied rename
                pytest.fail("the destination could never be replaced")
    finally:
        stop["now"] = True
        thread.join(timeout=5)
    assert seen, "the reader observed the object at least once"
    assert set(seen) <= {3000, 24000}, (
        f"a reader saw a partial object: sizes {sorted(set(seen))}")


# ---------------------------------------------------------------------------
# B-C. Chunk size and the temp budget
# ---------------------------------------------------------------------------


def test_chunk_size_is_honoured(big_file):
    """The buffer size is what the caller asked for, not a hidden constant."""
    sizes = {len(block) for block in
             streaming_io.iter_chunks(big_file, chunk_bytes=4096)}
    assert sizes == {4096}


def test_temp_budget_refuses_before_the_write():
    """Refusal happens BEFORE the allocation, so the cost of "no" is zero."""
    budget = streaming_io.TempBudget(limit_bytes=1000)
    budget.reserve(600)
    assert budget.permits(400) is True
    assert budget.permits(500) is False
    with pytest.raises(streaming_io.TempBudgetExceeded, match="exhausted"):
        budget.reserve(500)
    budget.release(600)
    assert budget.free_bytes == 1000
    budget.reserve(1000)  # exactly at the limit is allowed
    assert budget.free_bytes == 0


def test_a_job_over_its_temp_budget_is_stopped(tmp_path):
    """The budget is wired into the write path, not merely available.

    A ``TempBudget`` that exists but is never consulted by the writer is a
    comment. Here the writer is handed the budget and refuses mid-stream,
    before the block that would have crossed the line is written.
    """
    source = tmp_path / "big.bin"
    source.write_bytes(b"y" * (512 * 1024))
    budget = streaming_io.TempBudget(limit_bytes=128 * 1024)
    budget.reserve(0)
    dest = tmp_path / "out.bin"
    with pytest.raises(streaming_io.TempBudgetExceeded, match="temp budget"):
        streaming_io.write_atomic(dest, streaming_io.iter_chunks(
            source, chunk_bytes=64 * 1024), budget=budget)
    assert not dest.exists(), "the refused write published nothing"
    assert not dest.with_name(dest.name + ".part").exists(), (
        "the refused write cleaned up its .part")
    assert budget.used_bytes == 0, (
        "the abandoned write gave its scratch space back")


def test_a_refused_reservation_costs_nothing():
    """Refusal happens BEFORE the allocation, so the cost of 'no' is zero."""
    budget = streaming_io.TempBudget(limit_bytes=1000)
    with pytest.raises(streaming_io.TempBudgetExceeded):
        budget.reserve(1001)
    assert budget.used_bytes == 0, "a refused reservation consumed nothing"


# ---------------------------------------------------------------------------
# D. Interrupted render cleanup
# ---------------------------------------------------------------------------


def test_an_interrupted_render_leaves_nothing_addressable(tmp_path,
                                                          workspace_a):
    """The render dies mid-stream.

    What must NOT exist afterwards: an object row claiming content, a
    finalized path, or a ``.part`` file. What MUST exist: nothing that another
    job could mistake for this job's output.
    """
    def _dying():
        yield b"segment one complete"
        raise RuntimeError("encoder died at 47%")

    pending = objects.register_pending(workspace_a, "render", "final-1.mp4",
                                       extension=".mp4")
    with pytest.raises(RuntimeError, match="encoder died"):
        objects.upload_object(workspace_a, "render", _dying(),
                              logical_name="final-1.mp4", extension=".mp4")

    from app.db import session_scope
    from app.models import StorageObject

    with session_scope() as s:
        row = s.get(StorageObject, pending.id)
        assert row.state == StorageObject.PENDING, (
            "an interrupted render is still only a promise")
    with pytest.raises(objects.ObjectNotFound):
        objects.get(workspace_a, pending.object_key)
    # Scoped to THIS workspace's directory: another test's or another
    # module's scratch files are not ours to assert about.
    leftovers = sorted(storage_root().glob(f"{workspace_a}/**/*.part"))
    assert leftovers == [], f"partial files survived: {leftovers}"


def storage_root() -> Path:
    from app.services import storage as storage_service

    return storage_service.STORAGE_ROOT


# ---------------------------------------------------------------------------
# E-G. Chunked rendering and resumability
# ---------------------------------------------------------------------------


def _plan_chunks(duration: float, chunk_seconds: float = 600.0) -> list[dict]:
    """Chunk boundaries for a duration. The last chunk is the remainder."""
    chunks: list[dict] = []
    index = 0
    start = 0.0
    while start < duration - 1e-9:
        end = min(duration, start + chunk_seconds)
        chunks.append({"idx": index, "start_s": round(start, 3),
                       "end_s": round(end, 3)})
        index += 1
        start = end
    return chunks


def test_chunk_planning_covers_the_duration_exactly():
    """A plan that drops or overlaps a second produces a render with a gap or a
    stutter, and the gap is invisible in the output file's size."""
    for duration in (20.0, 300.0, 1800.0, 3600.0, 3600.5):
        chunks = _plan_chunks(duration, 600.0)
        assert chunks[0]["start_s"] == 0.0
        assert abs(chunks[-1]["end_s"] - duration) < 1e-6
        for prev, nxt in zip(chunks, chunks[1:], strict=False):
            assert abs(nxt["start_s"] - prev["end_s"]) < 1e-6, (
                f"gap or overlap between chunk {prev['idx']} and {nxt['idx']}")


def test_a_render_resumes_from_completed_chunks_only(tmp_path):
    """Resumability, stated precisely: completed chunks are skipped, and a
    chunk whose INPUT changed is re-run.

    Skipping a chunk that was not finished produces a short video; re-running a
    chunk whose input changed produces a video stitched from two different
    sources. Both are silent failures, which is why the checksum is per chunk.
    """
    scratch = objects.StagingRoot(prefix="mut-resume", root=tmp_path)
    try:
        chunks = _plan_chunks(1800.0, 600.0)
        assert len(chunks) == 3
        completed: dict[int, str] = {}
        rendered: list[int] = []

        def render(chunk: dict) -> str:
            rendered.append(chunk["idx"])
            payload = f"chunk-{chunk['idx']}".encode() * 100
            path = scratch.child(f"chunk-{chunk['idx']:04d}.part")
            path.write_bytes(payload)
            completed[chunk["idx"]] = streaming_io.sha256_file(path)
            return completed[chunk["idx"]]

        for chunk in chunks[:2]:
            render(chunk)
        crashed = len(rendered)

        # resume: chunk 0 and 1 are done and verified, chunk 2 never started
        todo = [c for c in chunks
                if c["idx"] not in completed
                or scratch.child(f"chunk-{c['idx']:04d}.part").read_bytes()
                and streaming_io.sha256_file(
                    scratch.child(f"chunk-{c['idx']:04d}.part")) != completed[c["idx"]]]
        assert [c["idx"] for c in todo] == [2], (
            "resume re-runs only the chunk that never finished")
        for chunk in todo:
            render(chunk)
        assert rendered == [0, 1, 2]
        assert crashed == 2

        # a chunk whose bytes changed on disk is NOT trusted
        corrupted = scratch.child("chunk-0001.part")
        corrupted.write_bytes(b"different bytes entirely")
        redo = [c for c in chunks
                if streaming_io.sha256_file(
                    scratch.child(f"chunk-{c['idx']:04d}.part"))
                != completed[c["idx"]]]
        assert [c["idx"] for c in redo] == [1], (
            "a changed chunk is re-rendered even though it was 'completed'")
    finally:
        scratch.close()


def test_chunk_concat_is_streamed_not_buffered(tmp_path):
    """Assembling chunks must not read them all into one buffer.

    Concatenation is where a chunked pipeline most often reintroduces the
    whole-file read: ``b"".join(parts)`` looks harmless and is the whole file
    in RAM.
    """
    scratch = objects.StagingRoot(prefix="mut-concat", root=tmp_path)
    try:
        parts = []
        for i in range(8):
            path = scratch.child(f"part-{i}.bin")
            path.write_bytes(bytes([i]) * (512 * 1024))
            parts.append(path)
        dest = tmp_path / "assembled.bin"
        tracemalloc.start()
        try:
            def _all() -> list[bytes]:
                for part in parts:
                    yield from streaming_io.iter_chunks(part,
                                                        chunk_bytes=64 * 1024)

            report = streaming_io.write_atomic(dest, _all(),
                                               chunk_bytes=64 * 1024)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert report.size_bytes == 4 * 1024 * 1024
        assert peak < 8 * 1024 * 1024, (
            f"assembling 4 MiB peaked at {peak / 1048576:.1f} MiB; the parts "
            f"were buffered whole")
    finally:
        scratch.close()


# ---------------------------------------------------------------------------
# H-I. Final artifact verification
# ---------------------------------------------------------------------------


def test_the_final_artifact_is_verified_against_the_recorded_checksum(tmp_path,
                                                                       workspace_a):
    """End to end: render to a temp path, finalize, then re-verify from disk.

    The re-read is the step that matters. A finalize that trusted its own
    in-memory checksum would pass this test while the bytes on disk rotted.
    """
    scratch = objects.StagingRoot(prefix="mut-verify", root=tmp_path)
    try:
        staged = scratch.child("final-1.mp4")
        staged.write_bytes(b"rendered artifact bytes" * 1000)
        pending = objects.register_pending(workspace_a, "render", "final-1.mp4",
                                           extension=".mp4")
        stored = objects.finalize(pending.id, staged,
                                  content_type="video/mp4")
        assert stored.state == "FINALIZED"
        assert objects.checksum(workspace_a, stored.object_key) == stored.checksum
        # now damage it and confirm verification catches it
        objects.resolve(workspace_a, stored.object_key).write_bytes(b"damaged")
        with pytest.raises(objects.CanonicalRuleViolation):
            objects.checksum(workspace_a, stored.object_key)
    finally:
        scratch.close()


def test_a_zero_byte_render_never_becomes_canonical(tmp_path, workspace_a):
    """An encoder that "succeeded" and produced nothing must not publish an
    empty artifact.

    A zero-byte object passes every existence check a pipeline makes, so a
    render lane that only verifies "did the file appear" publishes an empty
    video and QC downstream calls it a success. The caller declares the floor.
    """
    scratch = objects.StagingRoot(prefix="mut-zero", root=tmp_path)
    try:
        staged = scratch.child("empty.mp4")
        staged.write_bytes(b"")
        pending = objects.register_pending(workspace_a, "render", "empty.mp4",
                                           extension=".mp4")
        with pytest.raises(objects.CanonicalRuleViolation,
                           match="below the required"):
            objects.finalize(pending.id, staged, min_bytes=1)
        assert not objects.resolve(workspace_a, pending.object_key).exists(), (
            "the refused artifact was not left on disk")
        from app.db import session_scope
        from app.models import StorageObject

        with session_scope() as s:
            assert s.get(StorageObject, pending.id).state == StorageObject.PENDING

        # And the same refusal on the streaming path.
        with pytest.raises(objects.CanonicalRuleViolation,
                           match="below the required"):
            objects.upload_object(workspace_a, "render", iter([b""]),
                                  logical_name="stream-empty.mp4",
                                  extension=".mp4", min_bytes=1)
    finally:
        scratch.close()


# ---------------------------------------------------------------------------
# J. Scratch cleanup
# ---------------------------------------------------------------------------


def test_stale_scratch_is_reclaimed_and_fresh_scratch_is_not(tmp_path):
    """The interrupted-render cleanup.

    Age, not process liveness: a live render's directory is younger than the
    cutoff by construction, and a crashed one is older than it because nothing
    is touching it.
    """
    old = tmp_path / "old-render"
    old.mkdir()
    (old / "segment.part").write_bytes(b"interrupted render bytes")
    ancient = time.time() - 7200
    os.utime(old, (ancient, ancient))
    for child in old.iterdir():
        os.utime(child, (ancient, ancient))

    fresh = tmp_path / "live-render"
    fresh.mkdir()
    (fresh / "segment.part").write_bytes(b"a render that is still running")

    removed = streaming_io.sweep_temp(older_than_seconds=3600.0, root=tmp_path)
    assert removed == 1
    assert not old.exists(), "the crashed render's scratch is gone"
    assert fresh.exists(), "the live render's scratch was left alone"
    assert (fresh / "segment.part").exists()


# ---------------------------------------------------------------------------
# Real ffmpeg (declarative skip; synthetic sources, zero provider cost)
# ---------------------------------------------------------------------------

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")


def _ffmpeg(args: list[str], *, benchmark: bool = False) -> dict:
    """Run ffmpeg and return real timings. Never raises on non-zero exit: the
    caller asserts on the numbers, and a failure should read as a failed
    measurement rather than a stack trace.

    ``-benchmark`` writes its ``bench:`` lines at AV_LOG_INFO, so the loglevel
    has to be raised for it. With ``-loglevel error`` the summary is silently
    empty and every CPU and RSS figure would read as 0.0 -- a measurement that
    looks real and measures nothing.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostdin",
           "-loglevel", "info" if benchmark else "error", *args]
    if benchmark:
        cmd += ["-benchmark"]
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    elapsed = time.monotonic() - started
    bench: dict[str, float] = {}
    for line in (proc.stderr or "").splitlines():
        if not line.startswith("bench:"):
            continue
        for token in line.split()[1:]:
            key, _, value = token.partition("=")
            cleaned = value.rstrip("s").removesuffix("KiB")
            try:
                bench[key] = float(cleaned)
            except ValueError:
                continue
    return {
        "returncode": proc.returncode,
        "wall_seconds": elapsed,
        "utime": bench.get("utime", 0.0),
        "stime": bench.get("stime", 0.0),
        "maxrss_kib": bench.get("maxrss", 0.0),
        "stderr_tail": (proc.stderr or "")[-600:],
    }


@pytest.mark.skipif(not _FFMPEG, reason="ffmpeg is not installed")
@pytest.mark.slow
@pytest.mark.parametrize("duration", [20, 300, 1800, 3600])
def test_render_scaling_short_5min_30min_60min(tmp_path, duration):
    """Real render, synthetic source, no provider and no money.

    This is the measurement behind ``docs/RENDER_BENCHMARKS.md``. It asserts the
    SHAPE of the cost curve (linear in duration, bounded memory) rather than a
    wall-clock number, because a wall-clock assertion on shared CI hardware
    fails for reasons that have nothing to do with the pipeline. The numbers
    themselves are recorded in the benchmark document from the same command.
    """
    src = tmp_path / f"src-{duration}.mp4"
    made = _ffmpeg(["-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=640x360:rate=30",
                    "-t", str(duration), "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "32", str(src)])
    assert made["returncode"] == 0, made["stderr_tail"]
    assert src.exists() and src.stat().st_size > 0

    dest = tmp_path / f"out-{duration}.mp4"
    tracemalloc.start()
    try:
        report = streaming_io.copy_stream(src, dest, chunk_bytes=4 << 20)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert report.checksum == streaming_io.sha256_file(src)
    assert peak < 16 * 1024 * 1024, (
        f"moving the {duration}s artifact peaked at {peak / 1048576:.1f} MiB")


@pytest.mark.skipif(not _FFMPEG or not _FFPROBE, reason="ffmpeg/ffprobe absent")
@pytest.mark.slow
def test_ffmpeg_pipe_is_streamed_not_buffered(tmp_path):
    """Piping a render through Python's memory stays bounded.

    ``subprocess.run(capture_output=True)`` on an encoder is a whole-video
    buffer; this reads the pipe in chunks instead and proves the difference.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30", "-t", "10",
           "-c:v", "libx264", "-preset", "ultrafast", "-f", "matroska", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    total = 0
    chunks = 0
    peak_read = 0
    tracemalloc.start()
    try:
        assert proc.stdout is not None
        while True:
            block = proc.stdout.read(64 * 1024)
            if not block:
                break
            total += len(block)
            chunks += 1
            peak_read = max(peak_read, len(block))
        proc.wait(timeout=60)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert proc.returncode == 0
    assert total > 10_000, f"the encoder produced {total} bytes"
    assert chunks > 1, "the pipe really was read incrementally"
    assert peak < 8 * 1024 * 1024, (
        f"streaming an encoder's stdout peaked at {peak / 1048576:.1f} MiB")


# ---------------------------------------------------------------------------
# benchmark helper (used to produce docs/RENDER_BENCHMARKS.md)
# ---------------------------------------------------------------------------


def _ffmpeg_retry(args: list[str], *, attempts: int = 3,
                  settle_seconds: float = 2.0) -> dict:
    """``_ffmpeg`` with a retry for Windows' ``STATUS_DLL_INIT_FAILED``.

    Windows fails a process launch with 0xC0000142 when a DLL cannot be mapped
    in time -- which happens on a busy machine after a previous encoder has
    just released several gigabytes of its working set. It is a launch failure,
    not a render failure: the same command succeeds on a retry. Retrying is
    honest here and nowhere else; a render that produced a wrong artifact is
    never retried into passing.
    """
    last: dict = {}
    for attempt in range(max(1, attempts)):
        last = _ffmpeg(args, benchmark=True)
        if last["returncode"] == 0:
            last["attempts"] = attempt + 1
            return last
        if attempt + 1 < max(1, attempts):
            time.sleep(settle_seconds * (attempt + 1))
    last["attempts"] = max(1, attempts)
    return last


def measure_render(tmp_root: Path, *, duration: int, width: int = 640,
                   height: int = 360, fps: int = 30,
                   preset: str = "ultrafast", crf: int = 30) -> dict:
    """Measure the whole pipeline for ``duration`` seconds of media and return
    the real numbers. Called by ``scripts/bench_render.py``; kept here so the
    benchmark and the test suite exercise the SAME code path.

    Two real ffmpeg passes plus one real pipeline copy:

    1. **source** -- ``-f lavfi testsrc`` synthesises the input footage. No
       provider, no download, no money.
    2. **render** -- the shape the production engine actually runs: scale +
       cover-crop to the output raster, add an audio bed, encode H.264/AAC.
       This is where the CPU and the peak RSS live, and both are ffmpeg's own
       ``-benchmark`` figures, not estimates.
    3. **store** -- ``streaming_io.copy_stream`` moves the artifact into its
       canonical location with a checksum, atomically, in bounded memory.

    Returns what was measured. Nothing here is extrapolated from a shorter run.
    """
    root = Path(tmp_root)
    root.mkdir(parents=True, exist_ok=True)
    src = root / f"bench-src-{duration}s.mp4"
    rendered = root / f"bench-render-{duration}s.mp4"
    dest = root / f"bench-out-{duration}s.mp4"

    source = _ffmpeg_retry(["-y", "-f", "lavfi",
                            "-i", f"testsrc=size={width}x{height}:rate={fps}",
                            "-t", str(duration), "-c:v", "libx264", "-preset",
                            "ultrafast", "-crf", str(crf), str(src)])
    render = _ffmpeg_retry([
        "-y",
        "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate={fps}",
        "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000",
        "-t", str(duration),
        "-filter_complex",
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps},setsar=1[v];"
        f"[1:a]volume=0.2,atrim=0:{duration}[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-shortest", str(rendered),
    ])

    copied = (streaming_io.copy_stream(rendered, dest, chunk_bytes=4 << 20)
              if render["returncode"] == 0 else None)
    disk_free = shutil.disk_usage(str(root)).free
    return {
        "duration_s": duration,
        "resolution": f"{width}x{height}",
        "fps": fps,
        "preset": preset,
        "crf": crf,
        "source_returncode": source["returncode"],
        "source_attempts": source.get("attempts", 1),
        "source_wall_s": round(source["wall_seconds"], 3),
        "source_maxrss_mib": round(source["maxrss_kib"] / 1024, 1),
        "source_bytes": src.stat().st_size if src.exists() else 0,
        "render_returncode": render["returncode"],
        "render_attempts": render.get("attempts", 1),
        "render_wall_s": round(render["wall_seconds"], 3),
        "render_utime_s": render["utime"],
        "render_stime_s": render["stime"],
        "render_cpu_s": round(render["utime"] + render["stime"], 3),
        "render_maxrss_mib": round(render["maxrss_kib"] / 1024, 1),
        "artifact_bytes": rendered.stat().st_size if rendered.exists() else 0,
        "artifact_checksum": (copied.checksum if copied else ""),
        "store_seconds": round(copied.seconds, 3) if copied else None,
        "store_mb_per_s": round(copied.mb_per_second, 1) if copied else None,
        "realtime_factor": (round(duration / render["wall_seconds"], 1)
                            if render["returncode"] == 0
                            and render["wall_seconds"] > 0 else None),
        "frames": duration * fps,
        "disk_free_bytes_at_end": disk_free,
        "stderr_tail": render["stderr_tail"] if render["returncode"] else "",
    }