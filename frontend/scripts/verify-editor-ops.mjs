// Work 13.1 §7 verification: the editor's local op applier + inverses.
// Run via esbuild-bundled node (no test runner added to the repo).
import { applyOpsLocal, inverseOps } from "../src/editor/adapters/timelineAdapter";

let failures = 0;
const check = (name, cond, extra = "") => {
  if (cond) {
    console.log(`  ok   ${name}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${name} ${extra}`);
  }
};

const clip = (id, extra = {}) => ({
  id,
  name: id,
  start: 0,
  duration: 3,
  source: {},
  effects: [],
  source_start: 0,
  volume: 1,
  speed: 1,
  fade_in: 0,
  fade_out: 0,
  transform: {},
  text: {},
  transition_in: "cut",
  transition_out: "cut",
  ...extra,
});

const doc = (clips, kind = "caption") => ({
  workspace_id: "ws",
  fps: 30,
  aspect_ratio: "9:16",
  duration_seconds: 6,
  tracks: [{ id: "t1", kind, name: "T", clips }],
});

const find = (d, id) =>
  d.tracks.flatMap((t) => t.clips).find((c) => c.id === id);

console.log("== add_keyframe ==");
{
  const d0 = doc([clip("c1")]);
  const ops = [
    {
      type: "add_keyframe",
      track: "caption",
      clip_id: "c1",
      keyframe: { id: "k1", t: 0, easing: "LINEAR", props: { x: 0.1 } },
    },
  ];
  const d1 = applyOpsLocal(d0, ops);
  check("keyframe stored", find(d1, "c1").keyframes.length === 1);
  check("props stored", find(d1, "c1").keyframes[0].props.x === 0.1);
  const inv = inverseOps(d0, ops);
  const d2 = applyOpsLocal(d1, inv);
  check("undo removes the keyframe", (find(d2, "c1").keyframes ?? []).length === 0);
  // redo = re-apply the original
  const d3 = applyOpsLocal(d2, ops);
  check("redo restores it", find(d3, "c1").keyframes.length === 1);
}

console.log("== add_keyframe keeps canonical (t, id) order ==");
{
  const d0 = doc([clip("c1")]);
  const ops = [
    { type: "add_keyframe", track: "caption", clip_id: "c1",
      keyframe: { id: "late", t: 2, props: { x: 0.9 } } },
    { type: "add_keyframe", track: "caption", clip_id: "c1",
      keyframe: { id: "early", t: 0, props: { x: 0.1 } } },
  ];
  const d1 = applyOpsLocal(d0, ops);
  const ids = find(d1, "c1").keyframes.map((f) => f.id);
  check("sorted by time", JSON.stringify(ids) === JSON.stringify(["early", "late"]), ids.join(","));
}

console.log("== move_keyframe ==");
{
  const d0 = doc([clip("c1", { keyframes: [
    { id: "a", t: 0, easing: "LINEAR", props: { x: 0.1 } },
    { id: "b", t: 2, easing: "LINEAR", props: { x: 0.9 } },
  ]})]);
  const ops = [{ type: "move_keyframe", track: "caption", clip_id: "c1",
    keyframe_id: "b", t: 1 }];
  const d1 = applyOpsLocal(d0, ops);
  const kfs = find(d1, "c1").keyframes;
  check("re-ordered by time", JSON.stringify(kfs.map((f) => f.id)) === JSON.stringify(["a", "b"]));
  check("time applied", kfs[1].t === 1);
  const inv = inverseOps(d0, ops);
  const d2 = applyOpsLocal(d1, inv);
  const back = find(d2, "c1").keyframes;
  check("undo restores original time", back[1].t === 2, `got ${back[1].t}`);
  check("undo restores original order",
    JSON.stringify(back.map((f) => f.id)) === JSON.stringify(["a", "b"]));
}

console.log("== update_keyframe ==");
{
  const d0 = doc([clip("c1", { keyframes: [
    { id: "a", t: 0, easing: "LINEAR", props: { x: 0.1 } },
  ]})]);
  const ops = [{ type: "update_keyframe", track: "caption", clip_id: "c1",
    keyframe_id: "a", keyframe: { easing: "HOLD", props: { scale: 2 } } }];
  const d1 = applyOpsLocal(d0, ops);
  const f = find(d1, "c1").keyframes[0];
  check("easing updated", f.easing === "HOLD");
  check("id is stable", f.id === "a");
  const inv = inverseOps(d0, ops);
  const d2 = applyOpsLocal(d1, inv);
  const back = find(d2, "c1").keyframes[0];
  check("undo restores easing", back.easing === "LINEAR");
  check("undo restores props", back.props.x === 0.1 && back.props.scale === undefined);
}

console.log("== delete_keyframe ==");
{
  const d0 = doc([clip("c1", { keyframes: [
    { id: "a", t: 0, easing: "LINEAR", props: { x: 0.1 } },
    { id: "b", t: 1, easing: "LINEAR", props: { x: 0.5 } },
  ]})]);
  const ops = [{ type: "delete_keyframe", track: "caption", clip_id: "c1",
    keyframe_id: "a" }];
  const d1 = applyOpsLocal(d0, ops);
  check("one removed", find(d1, "c1").keyframes.length === 1);
  const inv = inverseOps(d0, ops);
  const d2 = applyOpsLocal(d1, inv);
  check("undo restores both", find(d2, "c1").keyframes.length === 2);
  check("undo restores the deleted frame",
    find(d2, "c1").keyframes.some((f) => f.id === "a"));
}

console.log("== set_keyframes ==");
{
  const d0 = doc([clip("c1", { keyframes: [
    { id: "old", t: 0, easing: "LINEAR", props: { x: 0.1 } },
  ]})]);
  const ops = [{ type: "set_keyframes", track: "caption", clip_id: "c1",
    keyframes: [
      { id: "n1", t: 0, props: { opacity: 0 } },
      { id: "n2", t: 2, props: { opacity: 1 } },
    ]}];
  const d1 = applyOpsLocal(d0, ops);
  const kfs = find(d1, "c1").keyframes;
  check("replaced wholesale", kfs.length === 2 && !kfs.some((f) => f.id === "old"));
  check("easing defaulted", kfs.every((f) => f.easing === "LINEAR"));
  const inv = inverseOps(d0, ops);
  const d2 = applyOpsLocal(d1, inv);
  const back = find(d2, "c1").keyframes;
  check("undo restores the prior chain", back.length === 1 && back[0].id === "old");
}

console.log("== keyframe ops never mutate the source doc ==");
{
  const d0 = doc([clip("c1", { keyframes: [
    { id: "a", t: 0, easing: "LINEAR", props: { x: 0.1 } },
  ]})]);
  const before = JSON.stringify(d0);
  applyOpsLocal(d0, [{ type: "set_keyframes", track: "caption", clip_id: "c1",
    keyframes: [{ id: "z", t: 1, props: { x: 0.9 } }] }]);
  check("source doc untouched", JSON.stringify(d0) === before);
}

console.log("== an op for a missing clip is a no-op, not a crash ==");
{
  const d0 = doc([clip("c1")]);
  const d1 = applyOpsLocal(d0, [{ type: "add_keyframe", track: "caption",
    clip_id: "ghost", keyframe: { id: "k", t: 0, props: { x: 0.1 } } }]);
  check("no throw, no phantom clip", d1.tracks[0].clips.length === 1);
}

console.log(failures === 0 ? "\nALL EDITOR OP TESTS PASSED"
  : `\n${failures} EDITOR OP TEST(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
