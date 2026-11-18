"""Extraction spec validation, normalisation, output shaping and request options."""
import json

import pytest

from shared.config import settings
from shared.extract_spec import (ExtractOptionsError, SpecError, conform, example_spec, get_path, merge_results,
                                 normalize_options, order_result, strict_json_schema, validate_spec, validation_report)


def _errors(raw):
    with pytest.raises(SpecError) as info:
        validate_spec(raw)
    return {(e["path"], e["message"].split(":")[0].split(" (")[0]) for e in info.value.errors}, info.value.errors


def test_example_spec_is_valid_and_flattened_into_fields():
    spec = validate_spec(example_spec())
    assert spec.paths == ["contract_title", "effective_date", "initial_term_years", "auto_renewal", "parties",
                          "termination.without_cause_notice_days", "termination.cure_period_days", "governing_law_state"]
    date = spec.field("effective_date")
    assert date.type == "string" and date.format == "date"  # "date" is an alias
    assert spec.field("parties").type == "array" and spec.field("parties").items["type"] == "object"
    assert spec.field("termination.cure_period_days").context.startswith("termination")
    assert spec.field("contract_title").required and not spec.field("auto_renewal").required


def test_spec_is_accepted_as_json_text_and_type_null_unions():
    text = json.dumps({"properties": {"total": {"type": ["number", "null"], "description": "Invoice total"}}})
    spec = validate_spec(text)
    assert spec.schema["properties"]["total"]["type"] == "number"


@pytest.mark.parametrize("raw, path, fragment", [
    ({"properties": {"a": {"type": "string"}}}, "$.a", "description is required"),
    ({"properties": {"a": {"description": "Some value"}}}, "$.a", "type is required"),
    ({"properties": {"a": {"type": "text", "description": "Some value"}}}, "$.a", "unsupported type"),
    ({"properties": {"a": {"type": "string", "description": "x"}}}, "$.a", "at least 3 characters"),
    ({"properties": {"a": {"type": "object", "description": "Group"}}}, "$.a", "need a non-empty 'properties'"),
    ({"properties": {"a": {"type": "array", "description": "List"}}}, "$.a", "need an 'items' object"),
    ({"properties": {"a": {"type": "array", "description": "List", "items": {"description": "x"}}}}, "$.a[]", "type is required"),
    ({"properties": {"a": {"type": "integer", "description": "Code", "enum": ["one", 2]}}}, "$.a.enum", "enum values must all be of type integer"),
    ({"properties": {"a": {"type": "boolean", "description": "Flag", "enum": []}}}, "$.a.enum", "enum must be a non-empty list"),
    ({"properties": {"1abc": {"type": "string", "description": "Bad name"}}}, "$.1abc", "field names must start"),
    ({"properties": {"a": {"type": "string", "description": "Valid"}}, "required": ["b"]}, "$.required", "'b' is listed"),
    ({"type": "array", "properties": {"a": {"type": "string", "description": "Valid"}}}, "$.type", "the top level"),
])
def test_invalid_specs_are_rejected_with_the_json_path(raw, path, fragment):
    _, errors = _errors(raw)
    assert any(e["path"] == path and fragment in e["message"] for e in errors), errors


def test_every_problem_is_reported_at_once():
    _, errors = _errors({"properties": {"a": {"type": "string"}, "b": {"type": "nope", "description": "B field"},
                                        "c": {"description": "C field"}}})
    assert {e["path"] for e in errors} == {"$.a", "$.b", "$.c"}


@pytest.mark.parametrize("raw, fragment", [
    ("", "The spec is empty"), ("{not json", "not valid JSON"), ('{"properties": {"a": 1, "a": 2}}', "duplicate key"),
    ([], "must be a JSON object"), ({"properties": {}}, "properties is required"), ({"title": "x"}, "properties is required"),
])
def test_malformed_specs(raw, fragment):
    _, errors = _errors(raw)
    assert fragment in errors[0]["message"]


def test_field_count_and_depth_limits(monkeypatch):
    monkeypatch.setattr(settings, "extract_spec_min_fields", 3)
    _, errors = _errors({"properties": {"a": {"type": "string", "description": "Only one"}}})
    assert "at least 3" in errors[0]["message"]
    monkeypatch.setattr(settings, "extract_spec_min_fields", 1)
    monkeypatch.setattr(settings, "extract_spec_max_fields", 2)
    many = {"properties": {f"f{i}": {"type": "string", "description": "Field value"} for i in range(3)}}
    assert "at most 2" in _errors(many)[1][0]["message"]
    monkeypatch.setattr(settings, "extract_spec_max_depth", 2)
    deep = {"properties": {"a": {"type": "object", "description": "Level 1", "properties": {
        "b": {"type": "object", "description": "Level 2", "properties": {"c": {"type": "string", "description": "Level 3"}}}}}}}
    assert "deeper than 2" in _errors(deep)[1][0]["message"]


def test_unknown_keys_are_warnings_not_errors():
    report = validation_report({"properties": {"a": {"type": "string", "description": "Value", "x-ui": 1}}})
    assert report["valid"] and report["field_count"] == 1 and "x-ui" in report["warnings"][0]
    bad = validation_report({"properties": {"a": {"type": "string"}}})
    assert not bad["valid"] and bad["errors"][0]["path"] == "$.a"


@pytest.mark.parametrize("value, schema, expected", [
    ("$1,250.50", {"type": "number"}, 1250.5), ("120", {"type": "integer"}, 120), ("120 days", {"type": "integer"}, None),
    (12.0, {"type": "integer"}, 12), (12.5, {"type": "integer"}, None), ("Yes", {"type": "boolean"}, True),
    ("no", {"type": "boolean"}, False), ("maybe", {"type": "boolean"}, None), (42, {"type": "string"}, "42"),
    ("  ", {"type": "string"}, None), ("N/A", {"type": "string"}, None), ("PAYER", {"type": "string", "enum": ["payer", "provider"]}, "payer"),
    ("broker", {"type": "string", "enum": ["payer", "provider"]}, None), ("x", {"type": "array", "items": {"type": "string"}}, ["x"]),
    ([{"code": "A", "extra": 1}, {}], {"type": "array", "items": {"type": "object", "properties": {"code": {"type": "string"}}}}, [{"code": "A"}]),
])
def test_conform_coerces_values_to_the_declared_type(value, schema, expected):
    assert conform(value, schema) == expected


def test_conform_shapes_objects_and_merge_keeps_found_values():
    spec = validate_spec(example_spec())
    first = conform({"contract_title": "Agreement", "parties": [{"name": "A", "role": "payer"}], "noise": 1}, spec.schema)
    assert "noise" not in first and first["termination"] == {"without_cause_notice_days": None, "cure_period_days": None}
    second = conform({"contract_title": None, "auto_renewal": False, "parties": [{"name": "A", "role": "payer"},
                                                                                 {"name": "B", "role": "provider"}]}, spec.schema)
    merged = merge_results(first, second, spec.schema)
    assert merged["contract_title"] == "Agreement"  # a null never erases a found value
    assert merged["auto_renewal"] is False  # False is a value, not "missing"
    assert [p["name"] for p in merged["parties"]] == ["A", "B"]  # arrays merged without duplicates
    assert get_path(merged, "termination.cure_period_days") is None


def test_strict_schema_is_closed_and_nullable():
    strict = strict_json_schema(validate_spec(example_spec()).schema)
    assert strict["additionalProperties"] is False and set(strict["required"]) == set(strict["properties"])
    assert strict["properties"]["contract_title"]["type"] == ["string", "null"]
    assert strict["properties"]["parties"]["type"] == ["array", "null"]
    item = strict["properties"]["parties"]["items"]
    assert item["type"] == "object" and item["additionalProperties"] is False
    assert item["properties"]["role"]["enum"][-1] is None


def test_request_options():
    models = {"anthropic:claude-sonnet-5-5": object()}
    opts = normalize_options(approach="Batch", model="anthropic:claude-sonnet-5-5", instructions=" be exact ",
                             context_window="20000", ocr_mode=None, available_models=models)
    assert opts == {"approach": "batch", "model": "anthropic:claude-sonnet-5-5", "instructions": "be exact",
                    "context_window": 20000, "ocr_mode": "auto"}
    for kwargs, fragment in [({"approach": "magic"}, "approach must be"), ({"model": "openai:gpt-x"}, "not available"),
                             ({"context_window": 1000}, "at least 16000"), ({"context_window": "big"}, "whole number"),
                             ({"ocr_mode": "sometimes"}, "ocr_mode"), ({"instructions": "x" * 4001}, "4000")]:
        base = {"approach": "agentic", "model": None, "instructions": None, "context_window": None, "ocr_mode": None,
                "available_models": models}
        base.update(kwargs)
        with pytest.raises(ExtractOptionsError, match=fragment):
            normalize_options(**base)


def test_results_are_returned_in_the_spec_key_order():
    spec = validate_spec(example_spec())
    stored = dict(sorted(conform({"contract_title": "A", "parties": [{"role": "payer", "name": "B"}]}, spec.schema).items()))
    stored["parties"] = [dict(sorted(p.items(), reverse=True)) for p in stored["parties"]]
    ordered = order_result({"data": stored, "summary": {}}, json.dumps(spec.schema))["data"]
    assert list(ordered) == list(spec.schema["properties"])
    assert list(ordered["parties"][0]) == ["name", "role"] and list(ordered["termination"]) == ["without_cause_notice_days", "cure_period_days"]
    assert order_result({"x": 1}, None) == {"x": 1}


@pytest.mark.parametrize("text, expected", [
    ("March 1, 2026", "2026-03-01"), ("March 1st, 2026", "2026-03-01"), ("03/01/2026", "2026-03-01"),
    ("1 March 2026", "2026-03-01"), ("2026-03-01T09:30:00Z", "2026-03-01"), ("2026-03-01", "2026-03-01"),
    ("within 30 days", "within 30 days"),
])
def test_date_fields_are_normalised_to_iso(text, expected):
    assert conform(text, {"type": "string", "format": "date"}) == expected
    assert conform("March 1, 2026", {"type": "string"}) == "March 1, 2026"  # only date fields are touched
