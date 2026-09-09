import re
from typing import Any, Dict


class StructuredResponseValidationError(ValueError):
    """Raised when parsed AI output violates its declared response rules."""

    def __init__(self, message: str, *, reason_code: str = "invalid", schema_path: str = "$"):
        # Keep the existing human-readable exception interface. Diagnostics
        # carry schema locations only, never a failing value or extra key.
        super().__init__(message)
        self.reason_code = reason_code
        self.schema_path = re.sub(r"\[\d+\]", "[]", schema_path)


def validate_structured_response(value: Any, schema: Dict[str, Any]) -> None:
    """Validate parsed AI output without logging or returning response content."""
    _validate(value, schema, path="$")


def _validate(value: Any, schema: Dict[str, Any], *, path: str) -> None:
    if "enum" in schema and value not in schema["enum"]:
        _fail("failed enum validation", path)
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        if not any(_matches_type(value, item) for item in schema_type):
            _fail("has the wrong type", path)
    elif schema_type and not _matches_type(value, schema_type):
        _fail("has the wrong type", path)

    if isinstance(value, dict):
        required = schema.get("required") or []
        missing = [key for key in required if key not in value]
        if missing:
            _fail("is missing required fields", path)
        if "minProperties" in schema and len(value) < int(schema["minProperties"]):
            _fail("has too few properties", path)
        properties = schema.get("properties") or {}
        for key, child_schema in properties.items():
            if key in value:
                _validate(value[key], child_schema, path="%s.%s" % (path, key))
        if schema.get("additionalProperties") is False and set(value).difference(properties):
            _fail("has unexpected fields", path)
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            _fail("has too few items", path)
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            _fail("has too many items", path)
        if schema.get("uniqueItems") and len({repr(item) for item in value}) != len(value):
            _fail("contains duplicate items", path)
        if schema.get("items"):
            for index, item in enumerate(value):
                _validate(item, schema["items"], path="%s[%s]" % (path, index))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            _fail("is below minimum", path)
        if "maximum" in schema and value > schema["maximum"]:
            _fail("is above maximum", path)
    elif isinstance(value, str):
        comparable = value.strip() if int(schema.get("minLength", 0)) > 0 else value
        if "minLength" in schema and len(comparable) < int(schema["minLength"]):
            _fail("is too short or blank", path)
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            _fail("is too long", path)


def _matches_type(value: Any, schema_type: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(schema_type, True)


def _fail(reason: str, path: str) -> None:
    code = {
        "failed enum validation": "enum",
        "has the wrong type": "type",
        "is missing required fields": "required",
        "has too few properties": "min_properties",
        "has unexpected fields": "additional_properties",
        "has too few items": "min_items",
        "has too many items": "max_items",
        "contains duplicate items": "unique_items",
        "is below minimum": "minimum",
        "is above maximum": "maximum",
        "is too short or blank": "min_length",
        "is too long": "max_length",
    }[reason]
    raise StructuredResponseValidationError(
        "AI response %s at %s." % (reason, path), reason_code=code, schema_path=path,
    )
