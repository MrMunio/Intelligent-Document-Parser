"""
Extraction spec: the JSON a client sends to describe the output it wants from a document.

The spec is a JSON Schema object, restricted to what an extraction needs, and every field must say what
it holds:

    {
      "title": "Provider contract",                      optional
      "description": "Key terms of a provider contract",  optional
      "type": "object",                                   optional, must be "object"
      "properties": {
        "effective_date": {"type": "string", "format": "date", "description": "Date the contract starts"},
        "parties": {"type": "array", "description": "Every party that signs",
                    "items": {"type": "object", "properties": {
                        "name": {"type": "string", "description": "Legal name"},
                        "role": {"type": "string", "description": "Role in the contract", "enum": ["payer", "provider"]}}}}
      },
      "required": ["effective_date"]                      optional
    }

Rules checked before a job is accepted (all errors are reported together, each with its JSON path):
  * the spec is a JSON object with a non-empty `properties` object;
  * field names are identifiers (letters, digits, underscore; not starting with a digit; up to 64 characters);
  * every field has a supported `type` (string, number, integer, boolean, object, array, or "date")
    and a `description` of at least 3 characters;
  * objects have `properties`; arrays have `items` with a type; `enum` values match the field type;
  * at least EXTRACT_SPEC_MIN_FIELDS and at most EXTRACT_SPEC_MAX_FIELDS fields, nesting up to
    EXTRACT_SPEC_MAX_DEPTH levels, no duplicate keys.

A "field" is a unit the extractor fills: a scalar, or an array (filled as a whole list). Objects are
containers and are flattened into their fields, addressed by dotted paths such as `contract.parties`.
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from shared.config import settings

SCALAR_TYPES = {"string", "number", "integer", "boolean"}
ALL_TYPES = SCALAR_TYPES | {"object", "array"}
TYPE_ALIASES = {"date": ("string", "date"), "datetime": ("string", "date-time"), "date-time": ("string", "date-time"),
                "float": ("number", None), "int": ("integer", None), "bool": ("boolean", None), "str": ("string", None)}
KNOWN_KEYS = {"type", "description", "properties", "items", "enum", "format", "required", "title", "examples",
              "default", "pattern", "minimum", "maximum", "minItems", "maxItems", "additionalProperties", "$schema",
              "$id", "nullable"}
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MIN_DESCRIPTION_CHARS = 3
MAX_DESCRIPTION_CHARS = 2000
MAX_SPEC_CHARS = 200_000
MAX_ENUM_VALUES = 200

APPROACHES = {
    "batch": "Document batch processing: the document is read in parts that fit the model context, in order, "
             "and the JSON is carried from one part to the next and updated.",
    "agentic": "Agentic retrieval: the document is indexed (table of contents, chunks, embeddings) and an agent "
               "navigates it with retrieval tools until every field is resolved. Best for long documents.",
}


class SpecError(ValueError):
    def __init__(self, errors: list[dict], warnings: Optional[list[str]] = None):
        self.errors = errors
        self.warnings = warnings or []
        first = errors[0] if errors else {"path": "$", "message": "invalid spec"}
        more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
        super().__init__(f"{first['path']}: {first['message']}{more}")


@dataclass
class FieldDef:
    path: str
    type: str
    description: str
    context: str = ""  # descriptions of the parent objects
    enum: Optional[list] = None
    format: Optional[str] = None
    items: Optional[dict] = None  # normalized item schema for arrays
    required: bool = False

    def brief(self) -> dict:
        out = {"path": self.path, "type": self.type, "description": self.description}
        if self.type == "array" and self.items:
            out["items"] = describe_type(self.items)
        if self.enum:
            out["enum"] = self.enum
        if self.format:
            out["format"] = self.format
        if self.context:
            out["context"] = self.context
        if self.required:
            out["required"] = True
        return out


@dataclass
class Spec:
    schema: dict  # normalized
    fields: list[FieldDef]
    title: str = ""
    description: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.fields]

    def field(self, path: str) -> Optional[FieldDef]:
        return next((f for f in self.fields if f.path == path), None)

    def empty_result(self) -> dict:
        return conform(None, self.schema)

    def assemble(self, values: dict[str, Any]) -> dict:
        """Builds the nested result from field values keyed by path. Missing fields are null."""
        return _assemble(self.schema, values, "")


def describe_type(schema: dict) -> str:
    t = schema.get("type")
    if t == "array":
        return f"array of {describe_type(schema.get('items') or {})}"
    if t == "object":
        keys = ", ".join(list((schema.get("properties") or {}).keys())[:12])
        return f"object {{{keys}}}"
    if schema.get("format"):
        return f"{t} ({schema['format']})"
    return t or "any"


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------

def _reject_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key '{key}'")
        seen[key] = value
    return seen


def parse_spec_text(text: str) -> Any:
    if text is None or not str(text).strip():
        raise SpecError([{"path": "$", "message": "The spec is empty. Send a JSON object with a 'properties' object."}])
    if len(text) > MAX_SPEC_CHARS:
        raise SpecError([{"path": "$", "message": f"The spec is larger than {MAX_SPEC_CHARS} characters."}])
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates)
    except ValueError as exc:
        raise SpecError([{"path": "$", "message": f"The spec is not valid JSON: {exc}"}]) from exc


class _Validator:
    def __init__(self):
        self.errors: list[dict] = []
        self.warnings: list[str] = []
        self.fields: list[FieldDef] = []
        self.max_depth = max(1, settings.extract_spec_max_depth)

    def error(self, path: str, message: str):
        self.errors.append({"path": path, "message": message})

    def type_of(self, node: dict, path: str) -> tuple[Optional[str], Optional[str]]:
        raw = node.get("type")
        fmt = node.get("format") if isinstance(node.get("format"), str) else None
        if isinstance(raw, list):  # ["string", "null"] style: every field is nullable anyway
            kinds = [t for t in raw if t != "null"]
            if len(kinds) != 1:
                self.error(path, "type lists may only combine one type with 'null'.")
                return None, fmt
            raw = kinds[0]
        if raw is None:
            if "properties" in node:
                return "object", fmt
            self.error(path, "type is required (string, number, integer, boolean, object, array or date).")
            return None, fmt
        if not isinstance(raw, str):
            self.error(path, "type must be a string.")
            return None, fmt
        key = raw.strip().lower()
        if key in TYPE_ALIASES:
            key, alias_fmt = TYPE_ALIASES[key]
            fmt = fmt or alias_fmt
        if key not in ALL_TYPES:
            self.error(path, f"unsupported type '{raw}'. Use string, number, integer, boolean, object, array or date.")
            return None, fmt
        return key, fmt

    def description_of(self, node: dict, path: str, required: bool = True) -> str:
        desc = node.get("description")
        if desc is None:
            if required:
                self.error(path, "description is required: say what the field holds so the extractor can find it.")
            return ""
        if not isinstance(desc, str):
            self.error(path, "description must be a string.")
            return ""
        desc = desc.strip()
        if required and len(desc) < MIN_DESCRIPTION_CHARS:
            self.error(path, f"description must be at least {MIN_DESCRIPTION_CHARS} characters.")
        if len(desc) > MAX_DESCRIPTION_CHARS:
            self.error(path, f"description is longer than {MAX_DESCRIPTION_CHARS} characters.")
        return desc

    def check_enum(self, node: dict, kind: str, path: str) -> Optional[list]:
        if "enum" not in node:
            return None
        values = node["enum"]
        if not isinstance(values, list) or not values:
            self.error(path + ".enum", "enum must be a non-empty list.")
            return None
        if len(values) > MAX_ENUM_VALUES:
            self.error(path + ".enum", f"enum has more than {MAX_ENUM_VALUES} values.")
        checks = {"string": lambda v: isinstance(v, str), "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
                  "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
                  "boolean": lambda v: isinstance(v, bool)}
        if kind not in checks:
            self.error(path + ".enum", "enum is only allowed on string, number, integer and boolean fields.")
            return None
        clean = [v for v in values if v is not None]
        bad = [v for v in clean if not checks[kind](v)]
        if bad:
            self.error(path + ".enum", f"enum values must all be of type {kind} (found {bad[:3]}).")
        if len(set(map(json.dumps, clean))) != len(clean):
            self.error(path + ".enum", "enum values must be unique.")
        return clean

    def unknown_keys(self, node: dict, path: str):
        extra = sorted(set(node) - KNOWN_KEYS)
        if extra:
            self.warnings.append(f"{path}: ignored keys {', '.join(extra)}")

    def node(self, node: Any, path: str, depth: int, describe: bool = True) -> Optional[dict]:
        """Validates one schema node and returns it normalized (or None when it is unusable)."""
        if not isinstance(node, dict):
            self.error(path, "each field must be a JSON object with at least 'type' and 'description'.")
            return None
        if depth > self.max_depth:
            self.error(path, f"nesting is deeper than {self.max_depth} levels.")
            return None
        self.unknown_keys(node, path)
        kind, fmt = self.type_of(node, path)
        desc = self.description_of(node, path, required=describe)
        if kind is None:
            return None
        out: dict[str, Any] = {"type": kind}
        if desc:
            out["description"] = desc
        if fmt:
            out["format"] = fmt
        if kind == "object":
            props = node.get("properties")
            if not isinstance(props, dict) or not props:
                self.error(path, "object fields need a non-empty 'properties' object.")
                return None
            out["properties"] = {}
            required = node.get("required") or []
            if not isinstance(required, list) or any(not isinstance(r, str) for r in required):
                self.error(path + ".required", "required must be a list of property names.")
                required = []
            for name in required:
                if name not in props:
                    self.error(path + ".required", f"'{name}' is listed as required but is not a property.")
            for name, child in props.items():
                child_path = f"{path}.{name}"
                if not NAME_RE.match(name):
                    self.error(child_path, "field names must start with a letter or underscore and contain only "
                                           "letters, digits and underscores (up to 64 characters).")
                    continue
                norm = self.node(child, child_path, depth + 1)
                if norm is not None:
                    out["properties"][name] = norm
            if required:
                out["required"] = [r for r in required if r in out["properties"]]
        elif kind == "array":
            items = node.get("items")
            if not isinstance(items, dict):
                self.error(path, "array fields need an 'items' object that describes one element (with a type).")
                return None
            norm = self.node(items, path + "[]", depth + 1, describe=False)
            if norm is None:
                return None
            out["items"] = norm
        else:
            enum = self.check_enum(node, kind, path)
            if enum:
                out["enum"] = enum
        return out


def _flatten(schema: dict, prefix: str, context: list[str], required: set, out: list[FieldDef]):
    for name, child in (schema.get("properties") or {}).items():
        path = f"{prefix}.{name}" if prefix else name
        if child["type"] == "object":
            _flatten(child, path, context + [f"{path}: {child.get('description', '')}".strip()],
                     set(child.get("required") or []), out)
        else:
            out.append(FieldDef(path=path, type=child["type"], description=child.get("description", ""),
                                context=" / ".join(c for c in context if c), enum=child.get("enum"),
                                format=child.get("format"), items=child.get("items"), required=name in required))


def validate_spec(raw: Any) -> Spec:
    """Validates and normalizes a spec. Raises SpecError with every problem found."""
    if isinstance(raw, (str, bytes)):
        raw = parse_spec_text(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    v = _Validator()
    if not isinstance(raw, dict):
        raise SpecError([{"path": "$", "message": "The spec must be a JSON object with a 'properties' object."}])
    if raw.get("type") not in (None, "object"):
        v.error("$.type", "the top level of the spec must be an object (type 'object').")
    if not isinstance(raw.get("properties"), dict) or not raw.get("properties"):
        raise SpecError([{"path": "$.properties",
                          "message": "properties is required: an object mapping each output key to its definition."}])
    top = dict(raw)
    top["type"] = "object"
    top.setdefault("description", top.get("title") or "Extraction result")
    schema = v.node(top, "$", 0, describe=False)
    fields: list[FieldDef] = []
    if schema is not None:
        _flatten(schema, "", [], set(schema.get("required") or []), fields)
    count = len(fields)
    if not v.errors:
        if count < max(1, settings.extract_spec_min_fields):
            v.error("$.properties", f"the spec defines {count} field(s); at least {settings.extract_spec_min_fields} "
                                    "are required.")
        if count > settings.extract_spec_max_fields:
            v.error("$.properties", f"the spec defines {count} fields; at most {settings.extract_spec_max_fields} "
                                    "are allowed per request.")
    if v.errors:
        raise SpecError(v.errors, v.warnings)
    for f in fields:  # paths in messages and tools use dots without the "$." prefix
        f.path = f.path.lstrip("$.")
    return Spec(schema=schema, fields=fields, title=str(raw.get("title") or ""),
                description=str(raw.get("description") or ""), warnings=v.warnings)


def validation_report(raw: Any) -> dict:
    """The validate-spec endpoint result: never raises."""
    try:
        spec = validate_spec(raw)
    except SpecError as exc:
        return {"valid": False, "errors": exc.errors, "warnings": exc.warnings, "field_count": 0, "fields": []}
    return {"valid": True, "errors": [], "warnings": spec.warnings, "field_count": len(spec.fields),
            "fields": [f.brief() for f in spec.fields], "normalized": spec.schema}


# ---------------------------------------------------------------------------
# Output shaping
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"^[\s$€£¥]*([-+]?\d[\d,]*(?:\.\d+)?|[-+]?\.\d+)\s*%?\s*$")
_TRUE = {"true", "yes", "y", "1", "checked", "x", "on"}
_FALSE = {"false", "no", "n", "0", "unchecked", "off", "none"}


def _to_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = _NUMBER_RE.match(value)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                return None
    return None


def conform(value: Any, schema: dict) -> Any:
    """Coerces a model answer to the declared shape: missing keys become null, extra keys are dropped."""
    kind = schema.get("type")
    if kind == "object":
        src = value if isinstance(value, dict) else {}
        return {name: conform(src.get(name), child) for name, child in (schema.get("properties") or {}).items()}
    if value is None:
        return None
    if kind == "array":
        if isinstance(value, str) and value.strip() == "":
            return None
        items = value if isinstance(value, list) else [value]
        out = []
        for item in items:
            shaped = conform(item, schema.get("items") or {})
            if shaped is not None and shaped != {} and not (isinstance(shaped, dict) and all(v is None for v in shaped.values())):
                out.append(shaped)
        return out
    if kind == "string":
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value).strip()
        if text == "" or text.lower() in ("null", "none", "n/a"):
            return None
        if schema.get("format") == "date":
            text = normalize_date(text)
        return _match_enum(text, schema)
    if kind in ("number", "integer"):
        num = _to_number(value)
        if num is None:
            return None
        if kind == "integer":
            if num != int(num):
                return None
            return _match_enum(int(num), schema)
        return _match_enum(int(num) if num == int(num) and isinstance(value, int) else num, schema)
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        return None
    return value


_DATE_FORMATS = ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y",
                 "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d", "%d.%m.%Y", "%B %dst, %Y", "%B %dnd, %Y", "%B %drd, %Y", "%B %dth, %Y")


def normalize_date(text: str) -> str:
    """'March 1, 2026', '03/01/2026', '2026-03-01T00:00:00' -> '2026-03-01'. Text that is not a date is kept."""
    from datetime import datetime
    candidate = re.sub(r"\s+", " ", text.strip().rstrip("."))
    candidate = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", candidate)
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", candidate):
        candidate = candidate[:10]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def _match_enum(value: Any, schema: dict) -> Any:
    enum = schema.get("enum")
    if not enum:
        return value
    if value in enum:
        return value
    if isinstance(value, str):
        for option in enum:
            if isinstance(option, str) and option.lower() == value.lower():
                return option
    return None


def _assemble(schema: dict, values: dict, prefix: str) -> Any:
    out = {}
    for name, child in (schema.get("properties") or {}).items():
        path = f"{prefix}.{name}" if prefix else name
        if child["type"] == "object":
            out[name] = _assemble(child, values, path)
        else:
            out[name] = conform(values.get(path), child)
    return out


def in_spec_order(value: Any, schema: dict) -> Any:
    """Restores the spec's key order (PostgreSQL JSONB stores object keys sorted). Unknown keys are kept, last."""
    if schema.get("type") == "object" and isinstance(value, dict):
        props = schema.get("properties") or {}
        out = {k: in_spec_order(value[k], props[k]) for k in props if k in value}
        out.update({k: v for k, v in value.items() if k not in props})
        return out
    if schema.get("type") == "array" and isinstance(value, list):
        return [in_spec_order(v, schema.get("items") or {}) for v in value]
    return value


def order_result(result: Any, spec_text: Any) -> Any:
    """An extraction result_data with `data` in the requester's key order."""
    if not isinstance(result, dict) or not isinstance(result.get("data"), dict) or not spec_text:
        return result
    try:
        schema = json.loads(spec_text) if isinstance(spec_text, str) else spec_text
    except ValueError:
        return result
    return {**result, "data": in_spec_order(result["data"], schema)}


def get_path(data: Any, path: str) -> Any:
    node = data
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def merge_results(previous: dict, new: dict, schema: dict) -> dict:
    """
    Carries state between document parts (batch approach). A value the model returns replaces the old one;
    a null never erases a value found earlier; arrays are merged without exact duplicates.
    """
    if schema.get("type") == "object":
        prev = previous if isinstance(previous, dict) else {}
        cur = new if isinstance(new, dict) else {}
        return {name: merge_results(prev.get(name), cur.get(name), child)
                for name, child in (schema.get("properties") or {}).items()}
    if schema.get("type") == "array":
        merged, seen = [], set()
        for item in (previous or []) + (new or []):
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                merged.append(item)
        return merged or (None if previous is None and new is None else merged)
    return new if new is not None else previous


def strict_json_schema(schema: dict) -> dict:
    """
    The spec as a structured-output schema: every key required, every value nullable,
    no additional properties. Accepted by OpenAI strict mode, Claude output formats and Ollama.
    """
    kind = schema.get("type")
    desc = schema.get("description")
    if kind == "object":
        props = {name: strict_json_schema(child) for name, child in (schema.get("properties") or {}).items()}
        out: dict[str, Any] = {"type": "object", "properties": props, "required": list(props.keys()),
                               "additionalProperties": False}
    elif kind == "array":
        out = {"type": ["array", "null"], "items": _not_null(strict_json_schema(schema.get("items") or {"type": "string"}))}
    else:
        out = {"type": [kind, "null"]}
        if schema.get("enum"):
            out["enum"] = list(schema["enum"]) + [None]
    if desc:
        fmt = schema.get("format")
        out["description"] = desc + (f" (format: {fmt})" if fmt else "")
    return out


def _not_null(schema: dict) -> dict:
    """Array elements are values, not optional slots."""
    out = dict(schema)
    t = out.get("type")
    if isinstance(t, list):
        kinds = [k for k in t if k != "null"]
        out["type"] = kinds[0] if len(kinds) == 1 else kinds
    if "enum" in out:
        out["enum"] = [e for e in out["enum"] if e is not None]
    return out


def example_spec() -> dict:
    return {
        "title": "Provider contract summary",
        "description": "Key commercial and legal terms of a healthcare provider contract",
        "type": "object",
        "properties": {
            "contract_title": {"type": "string", "description": "Full title of the agreement as written on the cover page"},
            "effective_date": {"type": "date", "description": "Date the contract takes effect"},
            "initial_term_years": {"type": "integer", "description": "Length of the initial term in years"},
            "auto_renewal": {"type": "boolean", "description": "Whether the contract renews automatically"},
            "parties": {
                "type": "array", "description": "Every party that signs the contract",
                "items": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Legal name of the party"},
                    "role": {"type": "string", "description": "Role of the party", "enum": ["payer", "provider", "other"]},
                }},
            },
            "termination": {
                "type": "object", "description": "Termination terms",
                "properties": {
                    "without_cause_notice_days": {"type": "integer", "description": "Days of written notice to terminate without cause"},
                    "cure_period_days": {"type": "integer", "description": "Days allowed to cure a material breach"},
                },
            },
            "governing_law_state": {"type": "string", "description": "State whose law governs the contract"},
        },
        "required": ["contract_title", "effective_date"],
    }


# ---------------------------------------------------------------------------
# Request options
# ---------------------------------------------------------------------------

class ExtractOptionsError(ValueError):
    pass


def normalize_options(*, approach: Optional[str], model: Optional[str], instructions: Optional[str],
                      context_window: Optional[int], ocr_mode: Optional[str], available_models: dict) -> dict:
    """
    Per-request options. `available_models` maps "provider:model" -> ModelInfo for engines with credentials.
    `model` may be omitted (the configured engine is used) or name one of those keys.
    """
    approach = (approach or "agentic").strip().lower()
    if approach not in APPROACHES:
        raise ExtractOptionsError(f"approach must be one of: {', '.join(APPROACHES)}.")
    model_key = (model or "").strip() or None
    if model_key is not None and model_key not in available_models:
        names = ", ".join(sorted(available_models)) or "none"
        raise ExtractOptionsError(f"model '{model_key}' is not available. Choose one of: {names}.")
    text = (instructions or "").strip()
    if len(text) > 4000:
        raise ExtractOptionsError("instructions must be 4000 characters or fewer.")
    window = None
    if context_window not in (None, "", 0):
        try:
            window = int(context_window)
        except (TypeError, ValueError) as exc:
            raise ExtractOptionsError("context_window must be a whole number of tokens.") from exc
        if window < 16_000:
            raise ExtractOptionsError("context_window must be at least 16000 tokens.")
    mode = (ocr_mode or "auto").strip().lower()
    if mode not in ("auto", "always", "never"):
        raise ExtractOptionsError("ocr_mode must be auto, always or never.")
    return {"approach": approach, "model": model_key, "instructions": text, "context_window": window, "ocr_mode": mode}
