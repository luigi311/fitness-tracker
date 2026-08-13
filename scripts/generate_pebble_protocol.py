"""Generate the Python, C, and manifest Pebble protocol artefacts."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import NoReturn

_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_]*$")
_WIRE_WIDTHS = {8, 16, 32}
# Repository protocol IDs occupy one byte even though Pebble dictionary keys
# are wider. This also caps generated manifest placeholders at 256 entries.
_MAX_PROTOCOL_KEY_ID = (1 << 8) - 1
_SYMBOLIC_SCALES = {"target_kind"}
_RESERVED_MESSAGE_KEY_PREFIX = "RESERVED_PROTOCOL_KEY_"
_C_TUPLE_FIELDS = {8: "uint8", 16: "uint16", 32: "uint32"}
_C_VALUE_TYPES = {8: "uint8_t", 16: "uint16_t", 32: "uint32_t"}
_C_WRITE_FUNCTIONS = {8: "uint8", 16: "uint16", 32: "uint32"}


class ProtocolSchemaError(ValueError):
    """Raised when the Pebble protocol schema is invalid."""


def _schema_error(message: str) -> NoReturn:
    raise ProtocolSchemaError(message)


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        _schema_error(f"{label} must be an integer")
    return value


def _protocol_key_id(value: object, label: str) -> int:
    key_id = _integer(value, label)
    if not 0 <= key_id <= _MAX_PROTOCOL_KEY_ID:
        _schema_error(f"{label} must be between 0 and {_MAX_PROTOCOL_KEY_ID}")
    return key_id


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        _schema_error(f"{label} must be an uppercase identifier")
    return value


def _python_literal(value: int | str) -> str:
    return json.dumps(value) if isinstance(value, str) else str(value)


def _c_scale_literal(value: int | str) -> str:
    if isinstance(value, int):
        return str(value)
    if value == "target_kind":
        return "PEBBLE_PROTOCOL_SCALE_TARGET_KIND"
    _schema_error(f"unsupported symbolic C scale: {value}")


def _load_key(
    index: int,
    raw_key: object,
    key_ids: set[int],
    key_names: set[str],
) -> dict[str, object]:
    label = f"keys[{index}]"
    if not isinstance(raw_key, dict):
        _schema_error(f"{label} must be a table")
    name = _identifier(raw_key.get("name"), f"{label}.name")
    key_id = _protocol_key_id(raw_key.get("id"), f"{label}.id")
    width = _integer(raw_key.get("width"), f"{label}.width")
    unit = raw_key.get("unit")
    scale = raw_key.get("scale")
    if key_id in key_ids or name in key_names:
        _schema_error(f"duplicate Pebble protocol key: {name}/{key_id}")
    if width not in _WIRE_WIDTHS:
        _schema_error(f"{label}.width must be one of {_WIRE_WIDTHS}")
    if not isinstance(unit, str) or not unit:
        _schema_error(f"{label}.unit must be a non-empty string")
    if not isinstance(scale, (int, str)) or isinstance(scale, bool):
        _schema_error(f"{label}.scale must be an integer or symbolic name")
    if isinstance(scale, str) and scale not in _SYMBOLIC_SCALES:
        _schema_error(f"{label}.scale has unsupported symbolic name: {scale}")
    key_ids.add(key_id)
    key_names.add(name)
    return {"name": name, "id": key_id, "width": width, "unit": unit, "scale": scale}


def _load_target(
    index: int,
    raw_target: object,
    target_values: set[int],
    target_names: set[str],
) -> dict[str, int | str]:
    label = f"target_kinds[{index}]"
    if not isinstance(raw_target, dict):
        _schema_error(f"{label} must be a table")
    name = _identifier(raw_target.get("name"), f"{label}.name")
    value = _integer(raw_target.get("value"), f"{label}.value")
    scale = _integer(raw_target.get("scale"), f"{label}.scale")
    if value in target_values or name in target_names:
        _schema_error(f"duplicate Pebble target kind: {name}/{value}")
    target_values.add(value)
    target_names.add(name)
    return {"name": name, "value": value, "scale": scale}


def _load_schema(path: Path) -> tuple[list[dict[str, object]], list[dict[str, int | str]]]:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    if _integer(document.get("version"), "version") != 1:
        _schema_error("unsupported Pebble protocol schema version")

    raw_keys = document.get("keys")
    raw_targets = document.get("target_kinds")
    if not isinstance(raw_keys, list) or not isinstance(raw_targets, list):
        _schema_error("schema must define keys and target_kinds arrays")

    key_ids: set[int] = set()
    key_names: set[str] = set()
    keys = [_load_key(index, raw_key, key_ids, key_names) for index, raw_key in enumerate(raw_keys)]

    target_values: set[int] = set()
    target_names: set[str] = set()
    targets = [
        _load_target(index, raw_target, target_values, target_names)
        for index, raw_target in enumerate(raw_targets)
    ]

    if not keys or not targets:
        _schema_error("schema must define at least one key and target kind")
    return keys, targets


def _python_source(
    keys: list[dict[str, object]],
    targets: list[dict[str, int | str]],
) -> str:
    lines = [
        '"""Generated from pebble/protocol.toml; do not edit manually."""',
        "",
        "from typing import Final",
        "",
    ]
    lines.extend(f"{key['name']}: Final = {key['id']}" for key in keys)
    lines.append("")
    lines.extend(f"{target['name']}: Final = {target['value']}" for target in targets)
    lines.append("")
    lines.extend(f"{key['name']}_SCALE: Final = {_python_literal(key['scale'])}" for key in keys)
    lines.extend(["", "KEY_WIDTHS: Final = {"])
    lines.extend(f"    {key['name']}: {key['width']}," for key in keys)
    lines.extend(["}", "", "KEY_SCALES: Final = {"])
    lines.extend(f"    {key['name']}: {_python_literal(key['scale'])}," for key in keys)
    lines.extend(["}", "", "TARGET_KIND_SCALE: Final = {"])
    lines.extend(f"    {target['name']}: {target['scale']}," for target in targets)
    lines.extend(["}", ""])
    return "\n".join(lines)


def _c_source(
    keys: list[dict[str, object]],
    targets: list[dict[str, int | str]],
) -> str:
    lines = [
        "#ifndef FITNESS_TRACKER_PEBBLE_PROTOCOL_H",
        "#define FITNESS_TRACKER_PEBBLE_PROTOCOL_H",
        "",
        "#include <stdint.h>",
        "",
        "// Generated from pebble/protocol.toml; do not edit manually.",
    ]
    if any(isinstance(key["scale"], str) for key in keys):
        lines.extend(
            [
                "#define PEBBLE_PROTOCOL_SCALE_TARGET_KIND (-1)",
                "",
            ],
        )
    lines.extend(
        [
            "enum {",
        ],
    )
    lines.extend(f"  {key['name']} = {key['id']}," for key in keys)
    lines.extend(["};", ""])
    for key in keys:
        width = key["width"]
        lines.extend(
            [
                f"#define {key['name']}_WIDTH {width}",
                f"#define {key['name']}_C_TYPE {_C_VALUE_TYPES[width]}",
                f"#define {key['name']}_SCALE {_c_scale_literal(key['scale'])}",
                (
                    f"#define {key['name']}_TUPLE_VALUE(tuple) "
                    f"((tuple)->value->{_C_TUPLE_FIELDS[width]})"
                ),
                (
                    f"#define {key['name']}_WRITE(iter, value) "
                    f"dict_write_{_C_WRITE_FUNCTIONS[width]}((iter), {key['name']}, (value))"
                ),
                "",
            ],
        )
    lines.extend(["typedef enum {"])
    lines.extend(f"  {target['name']} = {target['value']}," for target in targets)
    lines.extend(["} PebbleTargetKind;", ""])
    lines.extend(f"#define {target['name']}_SCALE {target['scale']}" for target in targets)
    lines.append("")
    lines.append("#endif")
    return "\n".join(lines) + "\n"


def _manifest_source(path: Path, keys: list[dict[str, object]]) -> str:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    message_keys: list[str] = []
    for key in sorted(keys, key=lambda item: _protocol_key_id(item["id"], "protocol key id")):
        key_id = _protocol_key_id(key["id"], "protocol key id")
        while len(message_keys) < key_id:
            message_keys.append(f"{_RESERVED_MESSAGE_KEY_PREFIX}{len(message_keys)}")
        message_keys.append(_identifier(key["name"], "protocol key name"))
    manifest["pebble"]["messageKeys"] = message_keys
    return json.dumps(manifest, indent=2) + "\n"


def _artefacts(root: Path) -> dict[Path, str]:
    keys, targets = _load_schema(root / "pebble/protocol.toml")
    return {
        root / "src/pebble_bridge/protocol.py": _python_source(keys, targets),
        root / "pebble/src/c/generated_protocol.h": _c_source(keys, targets),
        root / "pebble/package.json": _manifest_source(root / "pebble/package.json", keys),
    }


def main() -> int:
    """Generate artefacts or verify that checked-in artefacts are current."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if artefacts are stale")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root",
    )
    args = parser.parse_args()

    try:
        artefacts = _artefacts(args.root.resolve())
        stale = [
            str(path.relative_to(args.root))
            for path, expected in artefacts.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != expected
        ]
        if args.check:
            if stale:
                print("Stale Pebble protocol artefacts:", *stale, sep="\n", file=sys.stderr)
                result = 1
            else:
                print("Pebble protocol artefacts are current.")
                result = 0
        else:
            for path, expected in artefacts.items():
                path.write_text(expected, encoding="utf-8")
            print("Generated Pebble protocol artefacts.")
            result = 0
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ) as error:
        print(f"Pebble protocol generation failed: {error}", file=sys.stderr)
        result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
