"""Postman collection: generated from the live spec, committed file stays in sync."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _gen():
    spec = importlib.util.spec_from_file_location("gen_postman", REPO_ROOT / "scripts" / "gen_postman.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_postman_collection_covers_api():
    from fastapi.testclient import TestClient

    from app.main import create_app

    gen = _gen()
    client = TestClient(create_app(), raise_server_exceptions=False)
    r = client.get("/openapi.json")
    assert r.status_code == 200, r.text
    col = gen.build_collection(r.json())

    assert col["info"]["schema"].endswith("/collection.json")
    assert {v["key"] for v in col["variable"]} == {"baseUrl", "workspaceId", "jwt", "apiKey"}

    ops = [(i["request"]["method"], i["request"]["url"]["raw"], i["name"])
           for f in col["item"] for i in f["item"]]
    assert len(ops) >= 100, f"only {len(ops)} requests generated"
    assert any(m == "POST" and "ai-covers" in u for m, u, _ in ops)
    assert any(m == "POST" and u.rstrip("/").endswith("/webhooks") for m, u, _ in ops)
    assert any(m == "POST" and u.rstrip("/").endswith("/api-keys") for m, u, _ in ops)
    assert any(m == "GET" and u.endswith("/system/doctor") for m, u, _ in ops)
    assert any("(X-API-Key)" in n for _, _, n in ops)
    # collection-level bearer auth inherited by JWT requests
    assert col["auth"]["type"] == "bearer"

    # committed artifact is in sync with the generator
    committed = json.loads((REPO_ROOT / "docs" / "ymoney-postman.json").read_text(encoding="utf-8"))
    assert sum(len(f["item"]) for f in committed["item"]) == len(ops)
