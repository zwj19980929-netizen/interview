from copy import deepcopy
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError


SUPPORTED_CONTROLS = {
    "text",
    "secret",
    "number",
    "select",
    "switch",
    "textarea",
    "tags",
    "key_value",
}


class FormSchemaError(ValueError):
    pass


def validate_form_schema(schema: Dict[str, Any], *, path: str) -> Dict[str, Any]:
    if not isinstance(schema, dict) or not isinstance(schema.get("fields", []), list):
        raise FormSchemaError("%s must contain a fields list." % path)
    normalized = deepcopy(schema)
    normalized.setdefault("schema_version", "1")
    normalized.setdefault("fields", [])
    seen = set()
    for index, field in enumerate(normalized["fields"]):
        field_path = "%s.fields[%s]" % (path, index)
        if not isinstance(field, dict):
            raise FormSchemaError("%s must be an object." % field_path)
        name = field.get("name")
        if not isinstance(name, str) or not name.strip() or name in seen:
            raise FormSchemaError("%s has a missing or duplicate name." % field_path)
        seen.add(name)
        control = field.get("control", "text")
        if control not in SUPPORTED_CONTROLS:
            raise FormSchemaError("%s has unsupported control %s." % (field_path, control))
        field["control"] = control
        field.setdefault("label", _humanize(name))
        field.setdefault("required", False)
        field.setdefault("write_only", control == "secret")
        options = field.get("options")
        if control == "select" and options is not None:
            if not isinstance(options, list) or not all(
                isinstance(option, dict) and "value" in option and "label" in option for option in options
            ):
                raise FormSchemaError("%s select options are invalid." % field_path)
        visible_when = field.get("visible_when")
        if visible_when is not None and (
            not isinstance(visible_when, dict)
            or not isinstance(visible_when.get("field"), str)
            or "equals" not in visible_when
        ):
            raise FormSchemaError("%s visible_when is invalid." % field_path)
    return normalized


def apply_form_defaults(schema: Dict[str, Any], values: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    result = {}
    for field in schema.get("fields", []):
        if "default" in field:
            result[field["name"]] = deepcopy(field["default"])
    result.update(deepcopy(values or {}))
    return result


def validate_form_values(
    schema: Dict[str, Any],
    values: Any,
    *,
    path: str,
    partial: bool = False,
) -> Dict[str, Any]:
    if not isinstance(values, dict):
        raise _invalid(path, "%s must be an object." % path)
    fields = {field["name"]: field for field in schema.get("fields", [])}
    unknown = sorted(set(values).difference(fields))
    if unknown:
        raise _invalid(path, "%s contains unknown fields." % path, fields=unknown)
    result = deepcopy(values)
    for name, field in fields.items():
        visible = _is_visible(field, result)
        if not visible:
            result.pop(name, None)
            continue
        if name not in result or result[name] is None or result[name] == "":
            if not partial and field.get("required"):
                raise _invalid(path, "%s is missing required fields." % path, fields=[name])
            continue
        _validate_value(field, result[name], path="%s.%s" % (path, name))
    return result


def public_form_state(schema: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    state: Dict[str, Any] = {}
    for field in schema.get("fields", []):
        name = field["name"]
        if field.get("write_only"):
            state[name] = {"configured": bool(values.get(name))}
        elif name in values:
            state[name] = deepcopy(values[name])
    return state


def merge_form_schemas(*schemas: Dict[str, Any]) -> Dict[str, Any]:
    fields: List[Dict[str, Any]] = []
    indexes: Dict[str, int] = {}
    version_parts = []
    for schema in schemas:
        if not schema:
            continue
        version_parts.append(str(schema.get("schema_version", "1")))
        for field in schema.get("fields", []):
            item = deepcopy(field)
            name = item["name"]
            if name in indexes:
                fields[indexes[name]] = item
            else:
                indexes[name] = len(fields)
                fields.append(item)
    return {"schema_version": ".".join(version_parts) or "1", "fields": fields}


def schema_from_json_object(schema: Dict[str, Any], *, secret: bool = False) -> Dict[str, Any]:
    required = set(schema.get("required", []))
    fields = []
    for name, item in (schema.get("properties") or {}).items():
        item_type = item.get("type", "string")
        control = {
            "boolean": "switch",
            "integer": "number",
            "number": "number",
            "array": "tags",
            "object": "key_value",
        }.get(item_type, "secret" if secret or item.get("secret") else "text")
        field = {
            "name": name,
            "label": _humanize(name),
            "control": control,
            "required": name in required,
            "write_only": bool(secret or item.get("secret")),
        }
        for key in ("default", "minimum", "maximum"):
            if key in item:
                field[{"minimum": "min", "maximum": "max"}.get(key, key)] = item[key]
        fields.append(field)
    return {"schema_version": "legacy", "fields": fields}


def _validate_value(field: Dict[str, Any], value: Any, *, path: str) -> None:
    control = field["control"]
    if control in {"text", "secret", "textarea", "select"} and not isinstance(value, str):
        raise _invalid(path, "%s must be a string." % path)
    if control == "switch" and not isinstance(value, bool):
        raise _invalid(path, "%s must be a boolean." % path)
    if control == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise _invalid(path, "%s must be numeric." % path)
    if control == "tags" and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
        raise _invalid(path, "%s must be a list of strings." % path)
    if control == "key_value" and not isinstance(value, dict):
        raise _invalid(path, "%s must be an object." % path)
    if isinstance(value, str):
        if field.get("min_length") is not None and len(value) < int(field["min_length"]):
            raise _invalid(path, "%s is too short." % path)
        if field.get("max_length") is not None and len(value) > int(field["max_length"]):
            raise _invalid(path, "%s is too long." % path)
    if control == "number":
        if field.get("min") is not None and value < field["min"]:
            raise _invalid(path, "%s is below the minimum." % path)
        if field.get("max") is not None and value > field["max"]:
            raise _invalid(path, "%s exceeds the maximum." % path)
    if control == "select" and field.get("options"):
        allowed = {option["value"] for option in field["options"]}
        if value not in allowed:
            raise _invalid(path, "%s is not an allowed option." % path)


def _is_visible(field: Dict[str, Any], values: Dict[str, Any]) -> bool:
    condition = field.get("visible_when")
    return condition is None or values.get(condition["field"]) == condition["equals"]


def _invalid(path: str, message: str, **details: Any) -> ApiError:
    return ApiError(
        "MODEL_CONFIGURATION_INVALID",
        message,
        status_code=400,
        details={"path": path, **details},
    )


def _humanize(value: str) -> str:
    return value.replace("_", " ").strip().title()
