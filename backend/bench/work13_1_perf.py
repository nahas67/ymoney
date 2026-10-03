"""Work 13.1 §9 — performance + cache-invalidation measurement.

Measures the graph-building cost of the closure work at the sizes the work
order names, and measures the chunk-render key behaviour the architecture
relies on. Prints a table; asserts only the deterministic properties (same
input -> same output), never a wall-clock threshold, because timings on a
shared CI box are not a correctness signal.
"""

from __future__ import annotations

import statistics
import time

from app.engine.captions.cache import caption_cache_key, chunk_render_key
from app.engine.motion.graph import (
    build_visual_join,
    keyframe_expressions,
    order_effects,
    plan_composite,
    plan_transitions,
    validate_keyframes,
)

REPEATS = 20


def _timed(fn, *args, **kwargs):
    samples = []
    for _ in range(REPEATS):
        start = time.perf_counter()
        fn(*args, **kwargs)
        samples.append((time.perf_counter() - start) * 1000.0)
    return min(samples), statistics.median(samples)


def _row(label, low, median, unit="ms"):
    print(f"  {label:<44} {low:>8.3f} {median:>8.3f}  {unit}")


# ---------------------------------------------------------------------------
# 1. keyframe chains
# ---------------------------------------------------------------------------


def measure_keyframes() -> None:
    print("Keyframe chains (validate + compile to ffmpeg expressions)")
    for count in (2, 10, 25, 50, 64):
        frames = [{"id": f"k{i}", "t": round(i * 0.08, 4),
                   "easing": "EASE_IN_OUT",
                   "props": {"x": i / count, "y": 1 - i / count,
                             "scale": 1.0 + i / count, "opacity": 1.0}}
                  for i in range(count)]

        def build(frames=frames):
            valid, _ = validate_keyframes(frames, clip_duration=60.0)
            return keyframe_expressions(valid, width=1080, height=1920)

        low, median = _timed(build)
        _row(f"{count} keyframes", low, median)
    print("  (64 is MAX_KEYFRAMES; beyond it the chain is refused, not built)")


# ---------------------------------------------------------------------------
# 2. transitions
# ---------------------------------------------------------------------------


def _transition_doc(count: int) -> tuple[dict, list[dict]]:
    clips = []
    segments = []
    for index in range(count + 1):
        clip_id = f"v{index}"
        clips.append({"id": clip_id, "name": clip_id,
                      "start": index * 2.0, "duration": 2.0,
                      "source": {}, "effects": [], "source_start": 0.0,
                      "volume": 1.0, "speed": 1.0, "fade_in": 0.0,
                      "fade_out": 0.0, "transform": {}, "text": {},
                      "transition_in": "cut", "transition_out": "cut",
                      **({"transition": {
                          "from_item": f"v{index}", "to_item": f"v{index + 1}",
                          "type": "DISSOLVE", "duration": 0.5}}
                         if index < count else {})})
        segments.append({"clip": {"id": clip_id}, "duration": 2.0})
    doc = {"workspace_id": "ws", "fps": 30, "aspect_ratio": "9:16",
           "duration_seconds": 2.0 * (count + 1),
           "tracks": [{"id": "t", "kind": "video", "name": "v",
                       "clips": clips}]}
    return doc, segments


def measure_transitions() -> None:
    print("Transition ladder (plan + xfade graph for the whole join)")
    for count in (1, 5, 10, 20):
        doc, segments = _transition_doc(count)

        def build(doc=doc, segments=segments):
            plan, _warnings = plan_transitions(doc, segments)
            return build_visual_join(
                [f"[v{i}]" for i in range(len(segments))],
                [2.0] * len(segments), plan,
                xfade_builder=lambda spec, offset:
                    f"xfade=transition={spec['type'].lower()}"
                    f":duration={spec['duration']}:offset={offset:.4f}")

        low, median = _timed(build)
        _row(f"{count} transitions ({count + 1} segments)", low, median)

    # 10 transitions is the size the work order names; report its output size.
    doc, segments = _transition_doc(10)
    plan, _ = plan_transitions(doc, segments)
    join = build_visual_join([f"[v{i}]" for i in range(11)], [2.0] * 11, plan,
                             xfade_builder=lambda spec, offset:
                             f"xfade=transition=dissolve:offset={offset:.4f}")
    print(f"  -> 10 transitions produce {len(join.filters)} join filters, "
          f"{len(join.applied)} applied, warnings={len(join.warnings)}")


# ---------------------------------------------------------------------------
# 3. effect ordering + composite graph
# ---------------------------------------------------------------------------


def measure_effects() -> None:
    print("Effect ordering and composite graph planning")
    chains = {
        "5 effects": [{"type": t} for t in
                      ("CROP", "COLOR_ADJUST", "BLUR", "OPACITY",
                       "DROP_SHADOW")],
        "14 effects": [{"type": t} for t in
                       ("CROP", "PAN", "ZOOM", "TRACKED_CALLOUT", "MASK",
                        "BACKGROUND_BLUR", "COLOR_ADJUST", "BLUR", "SHARPEN",
                        "GLOW", "OPACITY", "DROP_SHADOW", "CROP", "BLUR")],
    }
    for label, effects in chains.items():
        low, median = _timed(order_effects, effects)
        _row(label, low, median)

    effect = {"type": "BACKGROUND_BLUR", "params": {"radius": 18}}
    ctx = {"subject_mask_key": "mask-1", "subject_mask_path": "C:/tmp/m.png"}
    low, median = _timed(plan_composite, effect, ctx)
    _row("composite BACKGROUND_BLUR graph", low, median)
    plan = plan_composite(effect, ctx)
    print(f"  -> composite emits {len(plan.filters)} graph fragments, "
          f"available={plan.available}")

    low, median = _timed(plan_composite, effect, {})
    _row("composite with NO mask (NOT_AVAILABLE path)", low, median)


# ---------------------------------------------------------------------------
# 4. simultaneous animated overlays
# ---------------------------------------------------------------------------


def measure_overlays() -> None:
    print("Simultaneous animated overlays (5 concurrent keyframed chains)")
    frames = [{"id": "a", "t": 0.0, "easing": "LINEAR",
               "props": {"x": 0.1, "opacity": 0.0}},
              {"id": "b", "t": 2.0, "easing": "EASE_OUT",
               "props": {"x": 0.9, "opacity": 1.0}}]

    def build():
        for _ in range(5):
            valid, _ = validate_keyframes(frames, clip_duration=3.0)
            keyframe_expressions(valid, width=1080, height=1920)

    low, median = _timed(build)
    _row("5 animated overlays", low, median)
    print("  -> 5 overlays x 2 keyframes = 10 compiled expression chains")


# ---------------------------------------------------------------------------
# 5. cache keys: what invalidates, and how selectively
# ---------------------------------------------------------------------------


def measure_cache() -> None:
    print("Cache keys (chunk render, 16 hex chars)")
    base_doc = {"workspace_id": "ws", "fps": 30, "aspect_ratio": "9:16",
                "duration_seconds": 4.0,
                "tracks": [{"id": "t", "kind": "video", "name": "v",
                            "clips": [{"id": "v1", "name": "v1", "start": 0.0,
                                       "duration": 4.0, "source": {},
                                       "effects": [], "source_start": 0.0,
                                       "volume": 1.0, "speed": 1.0,
                                       "fade_in": 0.0, "fade_out": 0.0,
                                       "transform": {}, "text": {},
                                       "transition_in": "cut",
                                       "transition_out": "cut"}]}]}

    def with_mutation(**clip_patch):
        doc = {**base_doc,
               "tracks": [{"id": "t", "kind": "video", "name": "v",
                           "clips": [{**base_doc["tracks"][0]["clips"][0],
                                      **clip_patch}]}]}
        return doc

    baseline = chunk_render_key(sub_doc=base_doc, chunk_index=0)
    cases = {
        "keyframes added": with_mutation(keyframes=[
            {"id": "k1", "t": 0.0, "props": {"x": 0.1}}]),
        "keyframe value changed": with_mutation(keyframes=[
            {"id": "k1", "t": 0.0, "props": {"x": 0.9}}]),
        "transition added": with_mutation(transition={
            "from_item": "v1", "to_item": "v2", "type": "FADE",
            "duration": 0.5}),
        "mask attached": with_mutation(subject_mask_asset_id="mask-9"),
        "effect parameter changed": with_mutation(effects=[
            {"type": "BLUR", "params": {"radius": 3}}]),
        "unrelated name change": with_mutation(name="v1-renamed"),
    }
    for label, doc in cases.items():
        key = chunk_render_key(sub_doc=doc, chunk_index=0)
        verdict = "INVALIDATES" if key != baseline else "reuses (isolated)"
        print(f"  {label:<44} {verdict}")

    low, median = _timed(chunk_render_key, sub_doc=base_doc, chunk_index=0)
    _row("chunk_render_key computation", low, median, unit="ms")

    low, median = _timed(caption_cache_key, source_checksum="s",
                         timeline_version=3,
                         style={"size": 60}, preset="minimal")
    _row("caption_cache_key computation", low, median, unit="ms")

    # HONEST GRANULARITY: the unit of cached work is the CHUNK, not the clip.
    # Each chunk is hashed from ITS OWN sliced document, so a keyframe edit in
    # chunk 1 must leave chunk 0's key (and therefore its rendered pixels)
    # untouched. Per-CLIP selectivity would need a different chunk model (the
    # longform chunker slices by chapter), so it is not claimed here.
    def chunk_doc(chapter: int, **patch):
        return {"workspace_id": "ws", "fps": 30, "aspect_ratio": "9:16",
                "duration_seconds": 4.0,
                "tracks": [{"id": f"t{chapter}", "kind": "video",
                            "name": f"chapter{chapter}", "clips": [
                                {"id": f"v{chapter}", "name": f"v{chapter}",
                                 "start": 0.0, "duration": 4.0, "source": {},
                                 "effects": [], "source_start": 0.0,
                                 "volume": 1.0, "speed": 1.0, "fade_in": 0.0,
                                 "fade_out": 0.0, "transform": {}, "text": {},
                                 "transition_in": "cut", "transition_out": "cut",
                                 **patch}]}]}

    # a keyframe edit lands in chapter 1 only
    edited = chunk_doc(1, keyframes=[{"id": "k1", "t": 0.0,
                                      "props": {"x": 0.1}}])
    pristine = chunk_doc(1)
    chapter0 = chunk_doc(0)
    other0 = chunk_doc(0)

    c0_before = chunk_render_key(sub_doc=chapter0, chunk_index=0)
    c0_after = chunk_render_key(sub_doc=other0, chunk_index=0)
    c1_before = chunk_render_key(sub_doc=pristine, chunk_index=1)
    c1_after = chunk_render_key(sub_doc=edited, chunk_index=1)
    print(f"  {'chapter 0 key stable across renders':<44} "
          f"{'yes' if c0_before == c0_after else 'NO'}")
    print(f"  {'keyframe edit invalidates its own chunk':<44} "
          f"{'yes' if c1_before != c1_after else 'NO'}")
    print("  (each chunk key is content-addressed from that chunk's own document,"
          " so an edit invalidates exactly the chunk containing it)")


# ---------------------------------------------------------------------------
# 6. determinism: the property that actually matters
# ---------------------------------------------------------------------------


def assert_determinism() -> None:
    print("Determinism (same input -> byte-identical graph)")
    doc, segments = _transition_doc(10)
    plan, _ = plan_transitions(doc, segments)
    builder = lambda spec, offset: (  # noqa: E731
        f"xfade=transition={spec['type'].lower()}"
        f":duration={spec['duration']}:offset={offset:.4f}")
    labels = [f"[v{i}]" for i in range(11)]
    durations = [2.0] * 11
    graphs = {
        tuple(build_visual_join(labels, durations, plan,
                                xfade_builder=builder).filters)
        for _ in range(5)
    }
    assert len(graphs) == 1, "the transition graph is not deterministic"
    print("  5 rebuilds of a 10-transition join produced 1 distinct graph")

    effects = [{"type": "BLUR"}, {"type": "CROP"}, {"type": "OPACITY"}]
    orders = {tuple(e["type"] for e in order_effects(effects)[0])
              for _ in range(5)}
    assert len(orders) == 1, "effect ordering is not deterministic"
    print(f"  5 orderings of one effect set -> {orders}")


def main() -> None:
    measure_keyframes()
    print()
    measure_transitions()
    print()
    measure_effects()
    print()
    measure_overlays()
    print()
    measure_cache()
    print()
    assert_determinism()
    print("\nAll performance and cache measurements completed.")


if __name__ == "__main__":
    main()
