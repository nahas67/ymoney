"""Versioned creation-template registry (OpenCreator-inspired).

Built-ins live as ``app/templates/<module>/<id>/<version>/template.json`` and
are loaded once at import. Workspaces override any template without touching
files via ``settings_json["templates"]["<module>/<id>"]`` (payload patch +
optional title), merged by :func:`apply_override` — no migration needed.

Schema per template.json: module, id, version, title, description,
inputs[{key,label,type,options?,default?}], payload (module-defined),
attribution.
"""

from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "templates"

REQUIRED_KEYS = ("module", "id", "version", "title", "payload")

CUSTOM_KEY = "templates_custom"


class TemplateError(Exception):
    pass


def validate_template(data: dict) -> list[str]:
    """Return a list of problems (empty = valid)."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["template must be a JSON object"]
    for key in REQUIRED_KEYS:
        if key not in data or data[key] in (None, ""):
            problems.append(f"missing required key: {key}")
    if not isinstance(data.get("payload", {}), dict):
        problems.append("payload must be an object")
    for i, inp in enumerate(data.get("inputs") or []):
        if not isinstance(inp, dict) or "key" not in inp or "type" not in inp:
            problems.append(f"inputs[{i}] needs key + type")
    source = data.get("source", "")
    if source and (not isinstance(source, str) or len(source) > 500):
        problems.append("source must be a short string (author link)")
    return problems


def _iter_files() -> list[Path]:
    if not TEMPLATE_ROOT.exists():
        return []
    return sorted(TEMPLATE_ROOT.glob("*/*/*/template.json"))


@lru_cache(maxsize=1)
def load_all() -> dict[str, dict]:
    """{(module, id, version): template dict}; invalid files are skipped loudly."""
    from loguru import logger

    out: dict[str, dict] = {}
    for path in _iter_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"[templates] unreadable {path}: {exc}")
            continue
        problems = validate_template(data)
        if problems:
            logger.warning(f"[templates] invalid {path}: {'; '.join(problems)}")
            continue
        # trust directory layout over fields for identity
        module, tid, version = path.parts[-4], path.parts[-3], path.parts[-2]
        out[(module, tid, version)] = {**data, "module": module, "id": tid, "version": version}
    return out


def list_templates(module: str = "", workspace_id: str | None = None) -> list[dict]:
    """Latest version per (module, id); optional module filter.

    With a workspace, community customs are appended (flagged custom:true).
    Customs may not collide with built-in ids — customize those via override
    patches instead, so authorship stays unambiguous.
    """
    latest: dict[tuple[str, str], dict] = {}
    for (mod, tid, ver), data in load_all().items():
        if module and mod != module:
            continue
        cur = latest.get((mod, tid))
        if cur is None or ver > cur["version"]:
            latest[(mod, tid)] = data
    out = [latest[k] for k in sorted(latest)]
    if workspace_id:
        seen = {(t["module"], t["id"]) for t in out}
        for t in custom_templates(workspace_id):
            if module and t["module"] != module:
                continue
            if (t["module"], t["id"]) in seen:
                continue  # defensive: customs can't shadow built-ins
            out.append({**t, "custom": True})
    return sorted(out, key=lambda t: (t["module"], t["id"]))


def versions_of(module: str, tid: str, workspace_id: str | None = None) -> list[str]:
    out = sorted(v for (m, t, v) in load_all() if m == module and t == tid)
    if workspace_id:
        out = sorted(set(out) | {t.get("version", "") for t in custom_templates(workspace_id)
                                 if t.get("module") == module and t.get("id") == tid})
    return out


def custom_templates(workspace_id: str | None) -> list[dict]:
    """Workspace-authored templates (OpenCreator community pattern).

    Stored under settings_json["templates_custom"]; invalid entries are
    skipped loudly. Attribution defaults to "workspace custom" so authorship
    is never blank; an optional `source` links the original.
    """
    from loguru import logger

    if not workspace_id:
        return []
    try:
        from app.db import session_scope
        from app.models import Workspace

        with session_scope() as s:
            ws = s.get(Workspace, workspace_id)
            raws = (ws.settings_json.get(CUSTOM_KEY) or []) if ws and ws.settings_json else []
    except Exception:
        return []
    out = []
    for raw in raws if isinstance(raws, list) else []:
        if validate_template(raw if isinstance(raw, dict) else {}):
            logger.warning("[templates] invalid custom template skipped")
            continue
        t = deepcopy(raw)
        t.setdefault("attribution", "workspace custom")
        t.setdefault("source", "")
        out.append(t)
    return out


def get_template(module: str, tid: str, version: str = "", workspace_id: str | None = None) -> dict:
    """Latest matching version by default; customs resolve first; KeyError when unknown."""
    if workspace_id:
        cands = [(t.get("version", ""), t) for t in custom_templates(workspace_id)
                 if t.get("module") == module and t.get("id") == tid]
        if cands:
            if version:
                for v, d in cands:
                    if v == version:
                        return {**deepcopy(d), "custom": True}
                raise KeyError(f"unknown version '{version}' for custom template '{module}/{tid}'")
            return {**deepcopy(max(cands, key=lambda c: c[0])[1]), "custom": True}
    cands = [(v, d) for (m, t, v), d in load_all().items() if m == module and t == tid]
    if not cands:
        raise KeyError(f"unknown template '{module}/{tid}'")
    if version:
        for v, d in cands:
            if v == version:
                return deepcopy(d)
        raise KeyError(f"unknown version '{version}' for template '{module}/{tid}'")
    return deepcopy(max(cands, key=lambda c: c[0])[1])


def apply_override(base: dict, patch: dict | None) -> dict:
    """Deep-merge a workspace patch over a built-in (patch wins)."""
    merged = deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = apply_override(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def workspace_overrides(workspace_id: str | None) -> dict:
    """Raw override map from workspace settings ({} when none)."""
    if not workspace_id:
        return {}
    try:
        from app.db import session_scope
        from app.models import Workspace

        with session_scope() as s:
            ws = s.get(Workspace, workspace_id)
            if not ws or not ws.settings_json:
                return {}
            return dict(ws.settings_json.get("templates") or {})
    except Exception:
        return {}


def resolve_template(module: str, tid: str, workspace_id: str | None = None,
                     version: str = "") -> dict:
    """Customs first, then built-in + workspace override merged; the single
    read path for engines."""
    if workspace_id:
        customs = [t for t in custom_templates(workspace_id)
                   if t.get("module") == module and t.get("id") == tid]
        if customs:
            return get_template(module, tid, version, workspace_id)
    base = get_template(module, tid, version)
    key = f"{module}/{tid}"
    patch = workspace_overrides(workspace_id).get(key)
    if not patch:
        return base
    merged = apply_override(base, patch if isinstance(patch, dict) else {})
    merged["overridden"] = True
    return merged


__all__ = [
    "TemplateError",
    "apply_override",
    "custom_templates",
    "get_template",
    "list_templates",
    "load_all",
    "resolve_template",
    "validate_template",
    "versions_of",
    "workspace_overrides",
]
