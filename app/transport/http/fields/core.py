from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Union

from pydantic import BaseModel


_MISSING = object()


@dataclass(frozen=True)
class Field:
    """A declarative output field.

    ``source`` may rename an input attribute/key. ``default`` is emitted only
    when the source is absent. Serializers are deliberately small and pure so
    field declarations remain readable security allow-lists.
    """

    source: Optional[str] = None
    default: Any = _MISSING
    serializer: Optional[Callable[[Any], Any]] = None


@dataclass(frozen=True)
class Nested:
    fields: Mapping[str, "FieldDefinition"]
    source: Optional[str] = None
    default: Any = _MISSING
    allow_none: bool = False


@dataclass(frozen=True)
class ListOf:
    item: Union[Field, Nested]
    source: Optional[str] = None
    default: Any = _MISSING


FieldDefinition = Union[Field, Nested, ListOf]
FieldSet = Mapping[str, FieldDefinition]


def raw(*, source: Optional[str] = None, default: Any = _MISSING) -> Field:
    return Field(source=source, default=default)


def string(*, source: Optional[str] = None, default: Any = _MISSING) -> Field:
    return Field(source=source, default=default, serializer=str)


def integer(*, source: Optional[str] = None, default: Any = _MISSING) -> Field:
    return Field(source=source, default=default, serializer=int)


def boolean(*, source: Optional[str] = None, default: Any = _MISSING) -> Field:
    return Field(source=source, default=default, serializer=bool)


def nested(
    fields: FieldSet,
    *,
    source: Optional[str] = None,
    default: Any = _MISSING,
    allow_none: bool = False,
) -> Nested:
    return Nested(fields=fields, source=source, default=default, allow_none=allow_none)


def list_of(
    item: Union[Field, Nested],
    *,
    source: Optional[str] = None,
    default: Any = _MISSING,
) -> ListOf:
    return ListOf(item=item, source=source, default=default)


def marshal(value: Any, fields: FieldSet) -> Dict[str, Any]:
    """Project a mapping/object through an explicit output allow-list."""

    output: Dict[str, Any] = {}
    for output_name, definition in fields.items():
        source = definition.source or output_name
        item = _read(value, source)
        if item is _MISSING:
            if definition.default is _MISSING:
                continue
            item = definition.default
        output[output_name] = _marshal_definition(item, definition)
    return output


def _marshal_definition(value: Any, definition: FieldDefinition) -> Any:
    if isinstance(definition, Field):
        if value is None or definition.serializer is None:
            return _json_value(value)
        return definition.serializer(value)
    if isinstance(definition, Nested):
        if value is None:
            return None if definition.allow_none else {}
        return marshal(value, definition.fields)
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("ListOf fields require a non-string sequence.")
    if isinstance(definition.item, Nested):
        return [marshal(item, definition.item.fields) for item in value]
    return [_marshal_definition(item, definition.item) for item in value]


def _read(value: Any, source: str) -> Any:
    current = value
    for part in source.split("."):
        if isinstance(current, BaseModel):
            current = current.model_dump()
        if isinstance(current, Mapping):
            if part not in current:
                return _MISSING
            current = current[part]
            continue
        if not hasattr(current, part):
            return _MISSING
        current = getattr(current, part)
    return current


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    return value
