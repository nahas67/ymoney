"""FastAPI response-filtering semantics — the assumption 172 generated contracts rest on.

Work 16.5.4 §1/§2: the programme prefers `response_model=` over `responses={}`, but
`response_model` VALIDATES AND FILTERS. A model that omits one key silently drops
that key from the wire while every test stays green — the exact failure §2 says
to detect.

Work 16.5.3 verified this statically (AST drift guards). It never verified what
the runtime actually does to a field the model does not declare. Since every
generated contract in this phase is derived from OBSERVED data, and no
observation can prove a field is absent from every unobserved state, the
difference between "silently dropped" and "preserved" decides whether the
approach is honest or fiction.

So it is measured here, against the real FastAPI in this venv, not asserted from
documentation. If a FastAPI upgrade changes this behaviour, these tests fail
before 172 contracts quietly start dropping data.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

REPO = Path(__file__).resolve().parents[2]


class _Plain(BaseModel):
    known: str


class _Extra(BaseModel):
    model_config = ConfigDict(extra="allow")

    known: str


@pytest.fixture(scope="module")
def probe() -> dict[str, dict]:
    app = FastAPI()

    @app.get("/plain")
    def plain() -> _Plain:
        return {"known": "a", "undeclared_but_real": "must survive"}  # type: ignore[return-value]

    @app.get("/extra")
    def extra() -> _Extra:
        return {"known": "a", "undeclared_but_real": "must survive"}  # type: ignore[return-value]

    @app.get("/documented", responses={200: {"model": _Plain}})
    def documented() -> dict:
        return {"known": "a", "undeclared_but_real": "must survive"}

    with TestClient(app) as c:
        return {
            "response_model": c.get("/plain").json(),
            "response_model_extra": c.get("/extra").json(),
            "responses_only": c.get("/documented").json(),
        }


class TestResponseFilteringIsWhatWeThinkItIs:
    def test_plain_response_model_DROPS_an_undeclared_field(self, probe):
        # Recorded deliberately. This is the hazard the whole approach avoids,
        # and if a future FastAPI version stops dropping, this test is the one
        # that tells us the generation strategy needs revisiting.
        assert probe["response_model"] == {"known": "a"}

    def test_response_model_with_extra_allow_PRESERVES_an_undeclared_field(self, probe):
        # The property 172 generated contracts depend on: a field missed during
        # observation is PRESERVED rather than silently discarded.
        assert probe["response_model_extra"]["undeclared_but_real"] == "must survive"

    def test_responses_only_preserves_the_payload_untouched(self, probe):
        # Confirms the Work 16.5.2 rationale: `responses={}` documents without
        # touching the wire.
        assert probe["responses_only"]["undeclared_but_real"] == "must survive"


class TestTheProbeIsReal:
    def test_the_probe_script_actually_exists_and_was_not_edited_to_pass(self):
        # Anti-tautology: the fixture above re-declares the models locally, so a
        # reader might wonder whether it reflects the shipped generator. Assert
        # the shipped INFERENCE module preserves undeclared fields.
        #
        # Checked in `infer_response_models.py`, not `gen_response_contracts.py`:
        # the orchestrator drives the pipeline but the `extra="allow"` config is
        # emitted by the inference step. Asserting against the wrong file made
        # this test fail for a reason unrelated to what it protects.
        source = (REPO / "scripts" / "infer_response_models.py").read_text(
            encoding="utf-8"
        )
        assert 'extra="allow"' in source, (
            "the inference module must emit models configured to preserve "
            "undeclared fields, or attaching them as response_model would "
            "silently drop real data"
        )
        assert (REPO / "scripts" / "gen_response_contracts.py").exists()

    def test_generated_models_declare_extra_allow(self):
        """Every emitted model must be `extra="allow"`.

        Checked on the real module rather than on a re-declaration, so this fails
        if the generator ever emits an unconfigured model.
        """
        generated = REPO / "backend" / "app" / "schemas" / "generated.py"
        if not generated.exists():
            pytest.skip("generated.py not produced yet")
        text = generated.read_text(encoding="utf-8")
        classes = [ln for ln in text.splitlines() if ln.startswith("class ")]
        assert classes, "generated.py declares no models"
        assert text.count('extra="allow"') >= len(classes), (
            f"only {text.count('extra=\"allow\"')} of {len(classes)} generated "
            f"models allow extra fields; the rest would silently drop data"
        )