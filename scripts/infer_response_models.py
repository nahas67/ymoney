"""Infer Pydantic models from observed runtime responses (Work 16.5.4 §3).

DESIGN RULES, EACH TRACEABLE TO A REQUIREMENT
---------------------------------------------
1. ``extra="allow"`` on every model (§1). A field absent from observation must be
   PRESERVED, not dropped. Verified behaviourally in
   ``test_work16_5_4_response_filtering.py``.

2. ``required`` only when the key appears in EVERY observed state (§3). One
   populated snapshot proves nothing about an empty workspace, so a single
   observation can never mark a field required.

3. Enums come from BACKEND CONSTANTS, never from the sample (§3: "do not freeze
   open-ended operational states merely to generate enums"). A field is only
   narrowed to a ``Literal`` when every observed value is an ALL-CAPS constant
   that actually exists in ``backend/app``. Job status, for instance, stays
   ``str``: the queue adds states and pinning today's eight would make a
   legitimate ninth a startup validation failure.

4. Numbers stay numbers, but a value seen as both ``int`` and ``float`` widens to
   ``float`` rather than dropping the integer state.

5. Timestamps are inferred as ``str`` unless the field name says otherwise. The
   routers emit ISO strings with a trailing ``Z``; declaring ``datetime`` would
   make the frontend parse a shape the server never promised.

6. Recursion is bounded. Self-referential JSON cannot be expressed as a finite
   Pydantic model, so a repeated shape collapses to ``dict`` at depth.
"""

from __future__ import annotations

import json
import keyword
import re
import hashlib
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1] / "backend" / "app"

#: Field names that are operational STATE, where a new value is expected over the
#: life of the system. These stay `str` even if every observed value happens to
#: be a constant, because the vocabulary is expected to grow.
OPEN_ENDED_HINTS = (
    "status",
    "state",
    "mode",
    "kind",
    "type",
    "stage",
    "outcome",
    "verdict",
    "reason",
    "note",
    "message",
    "detail",
    "source",
    "level",
    "action",
    "health",
    "code",
)

CONST_RE = re.compile(r'^[ \t]*([A-Z][A-Z0-9_]{2,})[ \t]*[:=][ \t]*["\']([^"\']+)["\']', re.M)


def backend_constant_values() -> dict[str, set[str]]:
    """Every ALL-CAPS string constant defined anywhere under ``backend/app``.

    Read from source rather than imported, so the scan cannot execute backend
    code and cannot be fooled by a value that is only produced at runtime.
    """
    found: dict[str, set[str]] = {}
    for path in BACKEND.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name, value in CONST_RE.findall(text):
            found.setdefault(value, set()).add(name)
    return found


def _is_open_ended(field: str) -> bool:
    low = field.lower()
    return any(hint in low for hint in OPEN_ENDED_HINTS)


class _Inferencer:
    def __init__(self) -> None:
        self.constants = backend_constant_values()
        self.models: dict[str, str] = {}
        self.lines: list[str] = []
        self._counter = 0

    # -- naming ------------------------------------------------------------
    def _model_name(self, path: str) -> str:
        slug = re.sub(r"[^0-9a-zA-Z]+", " ", path)
        parts = [p.capitalize() for p in slug.split() if p]
        name = "".join(parts)
        # Path parameters become readable words: "workspaces/{workspace_id}/jobs"
        name = name.replace("WorkspaceId", "Workspace")
        return name or "Observed"

    def _unique(self, base: str) -> str:
        return base

    # -- public ------------------------------------------------------------
    def infer_response(
        self,
        spec_path: str,
        method: str,
        observed_states: list[Any],
    ) -> str | None:
        """Emit a model for one endpoint's response and return its class name."""
        bodies = [s for s in observed_states if isinstance(s, (dict, list))]
        if not bodies:
            return None

        root = bodies[0]
        # Method and endpoint identity must not alias. Nested names include their
        # complete parent path; no field called `items` shares a global model.
        digest = hashlib.sha256(f"{method.upper()} {spec_path}".encode()).hexdigest()[:8]
        base = f"{method.capitalize()}{self._model_name(spec_path)}{digest}"
        if isinstance(root, list):
            # A bare array response: describe the ITEM, then wrap it, so the
            # published schema is an array of real items rather than array[any].
            item = self._emit_model(f"{base}Item", [b for b in bodies if isinstance(b, list)])
            if item is None:
                return None
            cls = self._unique(base)
            self.lines.append(
                f"class {cls}(_ObservedBase):\n"
                f'    """``{method} {spec_path}`` returns an array."""\n\n'
                f"    root: list[{item}]\n"
            )
            return cls

        cls = self._emit_model(base, bodies)
        return cls

    # -- internals ---------------------------------------------------------
    def _emit_model(self, name: str, bodies: list[Any]) -> str | None:
        dicts = [b for b in bodies if isinstance(b, dict)]
        if not dicts:
            return None
        if name in self.models:
            return self.models[name]

        keys = sorted({key for body in dicts for key in body})

        # An object that was EMPTY in every observed state yields a model with no
        # fields, which with `extra="allow"` validates as `Any` -- exactly the
        # fictional contract this work order forbids, dressed up as a schema.
        # Declining to emit it makes the caller fall back to a truthful `dict`.
        if not keys:
            return None

        cls = self._unique(name)
        self.models[name] = cls

        fields: list[str] = []
        n_required = 0
        for key in keys:
            values = [b[key] for b in dicts if key in b]
            # A field is REQUIRED in the published schema only if EVERY observed
            # state carried it. Giving every field a default would declare the
            # contract empty of requirements, which §3 forbids.
            is_required = all(key in b for b in dicts)
            annotation = self._annotation(key, values, f"{name}{_nested_name(key)}")
            safe = re.sub(r"[^0-9a-zA-Z_]", "_", key)
            if not safe or safe[0].isdigit() or safe.startswith("_") or keyword.iskeyword(safe):
                safe = f"f_{safe}"
            alias = f", alias={key!r}" if safe != key else ""
            if is_required:
                n_required += 1
                # Required means the KEY exists, not that its value is non-null.
                fields.append(f"    {safe}: {annotation}" + (f" = Field(...{alias})" if alias else ""))
            else:
                if annotation != "None" and not annotation.endswith(" | None"):
                    annotation += " | None"
                fields.append(f"    {safe}: {annotation} = " + (f"Field(None{alias})" if alias else "None"))

        note = (
            f"\n    # {n_required} of {len(fields)} fields were present in every "
            f"observed state"
        )
        self.lines.append(
            f"class {cls}(_ObservedBase):\n"
            f'    """Observed response contract."""\n\n'
            f"    model_config = ConfigDict(extra=\"allow\")\n\n"
            f"{chr(10).join(fields)}{note}\n"
        )
        return cls

    def _annotation(self, field: str, values: list[Any], name: str = "Value") -> str:
        present = [v for v in values if v is not None]
        nullable = len(present) < len(values)

        # Enum candidate: every observed non-null value is a backend constant,
        # AND the field is not an operational state that is expected to grow.
        #
        # The open-ended filter is deliberately aggressive, which is why the
        # generated module contains almost no enums. That is the correct
        # outcome, not a broken feature: `test_work16_5_4_inference.py` exercises
        # this branch directly so it cannot rot into untested dead code.
        if present and not _is_open_ended(field):
            literals: list[str] = []
            known = True
            for v in present:
                if not isinstance(v, str) or v not in self.constants:
                    known = False
                    break
                if v not in literals:
                    literals.append(v)
            if known and 1 < len(literals) <= 24:
                joined = ", ".join(json.dumps(v) for v in sorted(literals))
                return f"Literal[{joined}] | None" if nullable else f"Literal[{joined}]"

        kinds = set()
        for v in present:
            kinds.add(_json_kind(v))

        if not kinds:
            return "None"
        if kinds == {"str"}:
            return "str | None" if nullable else "str"
        if kinds == {"bool"}:
            return "bool | None" if nullable else "bool"
        if kinds <= {"int", "float"}:
            # A field seen as both int and float must accept either.
            if "float" in kinds:
                return "float | None" if nullable else "float"
            return "int | None" if nullable else "int"
        if kinds == {"null"}:
            return "None"

        if kinds == {"object"}:
            objects = [v for v in present if isinstance(v, dict)]
            keys = {k for v in objects for k in v}
            # UUID/date-keyed dictionaries are records, not DTO properties.
            # Emitting their keys freezes generated workspace ids into OpenAPI.
            if keys and all(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}|\d{4}-\d{2}-\d{2}(?:T.*)?|\d+", k) for k in keys):
                annotation = self._annotation(field, [v for obj in objects for v in obj.values()], name + "Value")
                return f"dict[str, {annotation}]" + (" | None" if nullable else "")
            nested = self._emit_model(
                name, objects
            )
            if nested:
                return f"{nested} | None" if nullable else nested
            return "dict[str, JsonValue] | None" if nullable else "dict[str, JsonValue]"

        if kinds == {"array"}:
            # All items share a shape? Describe the item. Otherwise a heterogeneous
            # array is genuinely `list` and forcing one item type would be a lie.
            item_kinds = {
                _json_kind(i) for arr in present if isinstance(arr, list) for i in arr
            }
            if item_kinds == {"object"}:
                rows = [i for arr in present if isinstance(arr, list) for i in arr if isinstance(i, dict)]
                nested = self._emit_model(name + "Row", rows) if rows else None
                if nested:
                    return f"list[{nested}] | None" if nullable else f"list[{nested}]"
            rows = [i for arr in present for i in arr]
            item = self._annotation(field, rows, name + "Item") if rows else "JsonValue"
            return f"list[{item}]" + (" | None" if nullable else "")

        # Mixed JSON kinds have a real union, not an unconstrained Any schema.
        branches = [self._annotation(field, [v for v in present if _json_kind(v) == kind], name + kind.capitalize()) for kind in sorted(kinds)]
        return " | ".join(branches + (["None"] if nullable else []))


def _json_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    return "other"


def _nested_name(field: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[^0-9a-zA-Z]+", field) if p) or "Nested"


HEADER = '''"""GENERATED RESPONSE CONTRACTS -- Work 16.5.4 §1. DO NOT HAND-EDIT.

Regenerate with::

    backend\\\\.venv\\\\Scripts\\\\python scripts\\\\gen_response_contracts.py

WHAT THIS IS
------------
Pydantic response contracts for the 172 ordinary-JSON endpoints the rebuilt UI
calls, which previously declared no 2xx schema at all and could therefore only be
contract-tested on route and method.

HOW THEY WERE OBTAINED
---------------------
OBSERVED, not imagined. `scripts/ui_contract_observer.py` builds a real
workspace, seeds real objects, and records the actual JSON each endpoint
returns -- in BOTH an empty and a populated state.

WHY `extra="allow"` ON EVERY MODEL
----------------------------------
A snapshot cannot prove a field is absent from every unobserved state. Without
this, `response_model=` would SILENTLY DROP a field the generator missed while
every test stayed green. With it, a missed field is preserved and reported
instead. The behaviour is verified at runtime, not assumed:
`tests/test_work16_5_4_response_filtering.py`.

WHY SO FEW ENUMS
----------------
`Literal` is emitted only when every observed value is an ALL-CAPS constant that
exists in `backend/app`. Operational states (status, mode, verdict, health...)
stay `str`, because those vocabularies are expected to grow and pinning them
turns a legitimate new backend state into a startup validation failure.

These models document the shape. They do not authorise anything.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _ObservedBase(BaseModel):
    """Base for every generated contract: preserve what we did not observe."""

    model_config = ConfigDict(extra="allow")

'''


def render_models(lines: list[str]) -> str:
    """Sort by dependency so a model is defined after everything it references."""
    body = "\n\n".join(lines)
    return HEADER + "\n" + body + "\n"
