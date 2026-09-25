"""Doctor parity: readiness probes + /system/doctor contract."""

from fastapi.testclient import TestClient

from app.main import create_app
from app.services import readiness as rd


def _client():
    return TestClient(create_app(), raise_server_exceptions=False)


def test_readiness_has_doctor_probes():
    data = rd.run_readiness()
    ids = {c["id"] for c in data["checks"]}
    for expected in ("llm", "video_engine", "ffmpeg", "yt_dlp", "tts", "images", "storage", "database", "trends", "publishing", "public_base"):
        assert expected in ids, f"missing probe {expected}"
    for c in data["checks"]:
        assert "status" in c and c["status"] in ("passed", "failed")
        assert "latency_ms" in c and isinstance(c["latency_ms"], int)
        assert "remediation" in c
        if c["status"] == "passed":
            assert c["remediation"] == ""
    assert "blocking_failures" in data
    assert "message" in data


def test_doctor_endpoint_is_fail_closed():
    c = _client()
    r = c.get("/api/v1/system/doctor")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "checks" in body and "doctor" in body
    assert "blocking_failed" in body["doctor"]
    assert "attention_needed" in body["doctor"]
    assert "remediations" in body["doctor"]


def test_readiness_endpoint_backward_compat():
    c = _client()
    r = c.get("/api/v1/system/readiness")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] in ("ready", "blocked")
    assert isinstance(body["checks"], list)
    # legacy fields still present
    assert all("id" in x and "status" in x and "blocking" in x and "detail" in x for x in body["checks"])


def test_checks_carry_tiers():
    import app.services.readiness as rd

    tiers = {c["id"]: c["tier"] for c in rd.run_readiness()["checks"]}
    assert tiers["ffmpeg"] == 0 and tiers["yt_dlp"] == 0
    assert tiers["storage"] == 0 and tiers["database"] == 0
    assert tiers["llm"] == 1 and tiers["publishing"] == 1
    assert set(tiers) >= {"llm", "video_engine", "ffmpeg", "yt_dlp", "tts", "images",
                          "storage", "database", "trends", "publishing", "public_base"}


def test_scrub_redacts_secrets():
    from app.services.readiness import scrub_text

    assert scrub_text("at https://user:s3cr3t@example.com/x ok") == "at https://***@example.com/x ok"
    assert scrub_text("got 401 for ?api_key=ABC123&x=1") == "got 401 for ?api_key=***&x=1"
    assert scrub_text("all clear") == "all clear"


def test_ytdlp_version_reported_or_actionable(monkeypatch):
    import shutil

    import app.services.readiness as rd

    data = rd.run_readiness()
    yt = next(c for c in data["checks"] if c["id"] == "yt_dlp")
    import re as _re

    assert _re.search(r"\d{4}\.\d{2}\.\d{2}", yt["detail"]) or yt["remediation"], yt

    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    missing = rd._check_yt_dlp()
    assert missing[0] is False and "pip install" in missing[3]
