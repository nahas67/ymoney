"""Remove the dead Transitions block from CaptionMotionPanel and mount it in the Inspector.

`CaptionMotionPanel` mounts only for `sel.track === "caption"` (Editor.tsx:801),
and its Transitions block was gated `track !== "caption"` (line 309). The
conjunction is unsatisfiable, so the control was unreachable dead code.

The block moves to `components/motion/TransitionEditor.tsx`, mounted in the
Inspector under the SAME visual gate the Inspector already uses for
`TransformEditor` -- because the renderer's transition is a VIDEO cross-dissolve
and `TRACK_FAMILIES["visual"]` is {video, broll, avatar}.

Line-range edits, verified after: the block is 309..429 inclusive (1-based),
terminated by the blank line before `<SubHead>Effects</SubHead>` at 431.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PANEL = REPO / "frontend" / "src" / "components" / "motion" / "CaptionMotionPanel.tsx"
EDITOR = REPO / "frontend" / "src" / "pages" / "Editor.tsx"


def cut_transitions_block() -> None:
    lines = PANEL.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(
        i for i, l in enumerate(lines) if l.rstrip().endswith('track !== "caption" && (')
    )
    # The block ends at the line before the blank that precedes <SubHead>Effects.
    end = next(
        i for i, l in enumerate(lines) if i > start and "<SubHead>Effects</SubHead>" in l
    )
    removed = lines[start:end]
    text = PANEL.read_text(encoding="utf-8")
    print(f"cutting {len(removed)} lines ({start + 1}..{end}) from CaptionMotionPanel")
    PANEL.write_text(
        text.replace("".join(removed), ""), encoding="utf-8"
    )


def mount_in_inspector() -> None:
    editor = EDITOR.read_text(encoding="utf-8")

    anchor = """      {(sel.track === "video" || sel.track === "broll" || sel.track === "avatar") && (
        <TransformEditor clip={clip} commit={(t: any) => commit(
          [{ type: "update_transform", track: sel.track, clip_id: clip.id, transform: t }], "Transform")} />
      )}"""
    addition = anchor + """
      {/* Transitions live HERE, not in CaptionMotionPanel. See
          components/motion/TransitionEditor.tsx for the traced rationale: the
          renderer's transition is a video cross-dissolve, so it belongs with the
          other VISUAL-track clip controls, gated by the same condition as
          TransformEditor. `siblings` is the selected track's own clips, because
          adjacency is what makes a transition legal. */}
      <TransitionEditor
        clip={clip}
        track={sel.track}
        siblings={siblings}
        commit={commit}
        disabled={readOnly} />"""
    assert anchor in editor, "TransformEditor anchor not found"
    editor = editor.replace(anchor, addition, 1)

    # The Inspector needs the selected track's clips.
    sig = 'function Inspector({ sel, clip, commit, splitAt, time, timelineId, readOnly }: any) {'
    newsig = (
        'function Inspector({ sel, clip, siblings, commit, splitAt, time, timelineId, readOnly }: any) {'
    )
    assert sig in editor, "Inspector signature not found"
    editor = editor.replace(sig, newsig, 1)

    call = '<Inspector sel={sel} clip={selClip} commit={commitOps} splitAt={splitAtPlayhead} time={time} timelineId={timelineId} readOnly={isViewer} />'
    newcall = (
        '<Inspector sel={sel} clip={selClip} siblings={siblingsOf(sel)} commit={commitOps} '
        "splitAt={splitAtPlayhead} time={time} timelineId={timelineId} readOnly={isViewer} />"
    )
    assert call in editor, "Inspector call site not found"
    editor = editor.replace(call, newcall, 1)

    # helper + import
    editor = editor.replace(
        'import CaptionMotionPanel from "../components/motion/CaptionMotionPanel";',
        'import CaptionMotionPanel from "../components/motion/CaptionMotionPanel";\n'
        'import TransitionEditor from "../components/motion/TransitionEditor";',
        1,
    )
    editor = editor.replace(
        "function Inspector({",
        """/** Every clip on the SAME track as the selection -- a transition needs a
 *  neighbour, and adjacency is per-track. */
function siblingsOf(sel: any, all: Track[] = []): Array<Record<string, any>> {
  const track = all.find((t) => t.kind === sel?.track);
  return (track?.clips ?? []) as Array<Record<string, any>>;
}

function Inspector({""",
        1,
    )
    EDITOR.write_text(editor, encoding="utf-8")
    print("mounted TransitionEditor in Inspector")


if __name__ == "__main__":
    cut_transitions_block()
    mount_in_inspector()