"""
Catalogue of built-in PHI / PII entity types and validation of per-request redaction options.
Shared by the API (validation, discovery endpoint) and the worker (prompt building).
"""
import re
from typing import Any, Optional

BUILTIN_ENTITIES: list[dict[str, str]] = [
    {"label": "PATIENT_NAME", "group": "Names", "description": "Patient, member, subscriber or relative name (first, last or full)."},
    {"label": "PROVIDER_NAME", "group": "Names", "description": "Name of any care team member: doctor, nurse, therapist, case manager, social worker or other clinician or staff named in the record."},
    {"label": "DATE", "group": "Dates", "description": "Any date tied to a person: birth, admission, discharge, service, procedure, death."},
    {"label": "AGE_OVER_89", "group": "Dates", "description": "An age explicitly above 89 years."},
    {"label": "MRN", "group": "Identifiers", "description": "Medical record number or patient ID code."},
    {"label": "SSN", "group": "Identifiers", "description": "Social security number."},
    {"label": "HEALTH_PLAN_ID", "group": "Identifiers", "description": "Health plan, insurance member, policy or group number."},
    {"label": "ACCOUNT_NUMBER", "group": "Identifiers", "description": "Bank, billing or patient account number."},
    {"label": "LICENSE_NUMBER", "group": "Identifiers", "description": "Driver licence, professional licence, certificate, NPI or DEA number."},
    {"label": "DEVICE_ID", "group": "Identifiers", "description": "Medical device identifier or serial number, vehicle identifier or plate."},
    {"label": "OTHER_ID", "group": "Identifiers", "description": "Any other unique personal identifier not covered by another type."},
    {"label": "PHONE", "group": "Contact", "description": "Telephone or fax number."},
    {"label": "EMAIL", "group": "Contact", "description": "Email address."},
    {"label": "ADDRESS", "group": "Contact", "description": "Street address, city, county, ZIP code or any geographic unit smaller than a state."},
    {"label": "URL", "group": "Contact", "description": "Web address."},
    {"label": "IP_ADDRESS", "group": "Contact", "description": "IP address."},
]
BUILTIN_LABELS = [e["label"] for e in BUILTIN_ENTITIES]

REDACTION_STYLES = ["label", "blackout"]
OCR_MODES = ["auto", "always", "never"]

MAX_CUSTOM_ENTITIES = 25


class OptionsError(ValueError):
    pass


def normalize_label(label: str) -> str:
    """'Employee ID ' -> 'EMPLOYEE_ID'. Labels become placeholder names, so they are kept strict."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", (label or "").strip()).strip("_").upper()
    if not cleaned:
        raise OptionsError("Custom entity label must contain letters or digits.")
    if cleaned[0].isdigit():
        cleaned = f"X_{cleaned}"
    return cleaned[:40]


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def normalize_phi_options(raw: Optional[dict]) -> dict:
    """Validates request options and fills defaults. Raises OptionsError with a user-facing message."""
    raw = raw or {}

    entity_types = raw.get("entity_types")
    if entity_types is None:
        entity_types = list(BUILTIN_LABELS)
    else:
        if not isinstance(entity_types, list):
            raise OptionsError("entity_types must be a list of built-in labels.")
        entity_types = [str(t).strip().upper() for t in entity_types if str(t).strip()]
        unknown = [t for t in entity_types if t not in BUILTIN_LABELS]
        if unknown:
            raise OptionsError(f"Unknown built-in entity types: {', '.join(unknown)}. "
                               f"Use custom_entities to define your own.")
        entity_types = [t for t in BUILTIN_LABELS if t in entity_types]

    custom_in = raw.get("custom_entities") or []
    if not isinstance(custom_in, list):
        raise OptionsError("custom_entities must be a list of {label, description} objects.")
    if len(custom_in) > MAX_CUSTOM_ENTITIES:
        raise OptionsError(f"At most {MAX_CUSTOM_ENTITIES} custom entities are allowed per request.")
    custom: list[dict[str, str]] = []
    seen = set()
    for item in custom_in:
        if not isinstance(item, dict):
            raise OptionsError("Each custom entity must be an object with 'label' and 'description'.")
        label = normalize_label(str(item.get("label", "")))
        description = str(item.get("description", "")).strip()
        if not description:
            raise OptionsError(f"Custom entity '{label}' needs a description of what to detect.")
        if len(description) > 600:
            raise OptionsError(f"Custom entity '{label}' description is longer than 600 characters.")
        if label in seen:
            raise OptionsError(f"Custom entity label '{label}' is defined more than once.")
        seen.add(label)
        custom.append({"label": label, "description": description})

    if not entity_types and not custom:
        raise OptionsError("Select at least one built-in entity type or define a custom entity.")

    instructions = str(raw.get("instructions") or "").strip()
    if len(instructions) > 2000:
        raise OptionsError("instructions must be at most 2000 characters.")

    style = str(raw.get("redaction_style") or "label").strip().lower()
    if style not in REDACTION_STYLES:
        raise OptionsError(f"redaction_style must be one of: {', '.join(REDACTION_STYLES)}.")

    ocr_mode = str(raw.get("ocr_mode") or "auto").strip().lower()
    if ocr_mode not in OCR_MODES:
        raise OptionsError(f"ocr_mode must be one of: {', '.join(OCR_MODES)}.")

    return {
        "entity_types": entity_types,
        "custom_entities": custom,
        "instructions": instructions,
        "redaction_style": style,
        "ocr_mode": ocr_mode,
        "use_pattern_detectors": _as_bool(raw.get("use_pattern_detectors"), True),
        "scrub_metadata": _as_bool(raw.get("scrub_metadata"), True),
    }
