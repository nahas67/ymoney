"""Release-only regressions: observed shapes, not coverage-number targets."""
import sys
from pathlib import Path

from pydantic import ValidationError
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import infer_response_models as infer


def compile_models(engine):
    scope = {"__name__": "observed_test"}
    exec(infer.render_models(engine.lines), scope)
    for name in engine.models.values():
        scope[name].model_rebuild(_types_namespace=scope)
    return scope


def test_method_and_nested_shapes_do_not_alias():
    engine = infer._Inferencer()
    read = engine.infer_response("/objects", "GET", [{"item": {"name": "x"}}])
    create = engine.infer_response("/objects", "POST", [{"item": {"id": 1}}])
    assert read != create
    scope = compile_models(engine)
    assert scope[read].model_validate({"item": {"name": "x"}})
    assert scope[create].model_validate({"item": {"id": 1}})
    with pytest.raises(ValidationError):
        scope[create].model_validate({"item": {"name": "x"}})


def test_nullable_is_independent_of_optional():
    engine = infer._Inferencer()
    name = engine.infer_response("/nullable", "GET", [{"n": None}, {"n": 1, "later": "x"}])
    model = compile_models(engine)[name]
    schema = model.model_json_schema()
    assert schema["required"] == ["n"]
    assert {v["type"] for v in schema["properties"]["n"]["anyOf"]} == {"integer", "null"}
    assert model.model_validate({"n": None}).later is None
    with pytest.raises(ValidationError):
        model.model_validate({})


def test_record_ids_never_become_schema_properties():
    def emitted(key):
        engine = infer._Inferencer()
        engine.infer_response("/records", "GET", [{"records": {key: {"count": 2}}}])
        return infer.render_models(engine.lines)
    assert emitted("01234567-89ab-cdef-0123-456789abcdef") == emitted("fedcba98-7654-3210-fedc-ba9876543210")


def test_empty_root_is_not_a_contract():
    assert infer._Inferencer().infer_response("/empty", "GET", [{}, {}]) is None
