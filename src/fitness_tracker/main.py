import argparse
import json
import re
import signal
import sys
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import cast

import gi
from loguru import logger
from loguru._defaults import LOGURU_FORMAT

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import GLib  # noqa: E402  # ty:ignore[unresolved-import]

_LOGURU_COLOR_TAG = re.compile(r"</?[A-Za-z][^<>]*>")


def _json_default(o: object) -> object:
    # dataclasses
    if is_dataclass(o):
        return asdict(o)  # ty:ignore[invalid-argument-type]
    # enums
    if isinstance(o, Enum):
        return o.value  # or o.name
    # anything else: fallback to string
    return str(o)


def formatter(record: Mapping[str, object]) -> str:
    """Format a Loguru record with its optional structured data payload."""
    plain_format = _LOGURU_COLOR_TAG.sub("", LOGURU_FORMAT)
    base = plain_format.format_map(record)
    extra = cast("dict[str, object]", record["extra"])
    data = extra.get("data", "")

    if isinstance(data, dict) or hasattr(data, "__dataclass_fields__"):
        data_str = json.dumps(data, indent=4, default=_json_default)
        lines = [line.rstrip() for line in data_str.splitlines()]
        lines.insert(0, "")
    elif isinstance(data, list):
        lines = [f"{item}" for item in data]
        lines.insert(0, "")
    else:
        lines = [str(data)]

    message = str(record["message"])
    indent = "\n  " + (" " * (len(base.replace(message, "").strip()) + 1))

    extra["formatted_data"] = indent.join(lines)
    return LOGURU_FORMAT + "{extra[formatted_data]}\n{exception}"


def configure_logger(debug_level: str = "INFO") -> None:
    """Configure Loguru's console sink for the requested verbosity."""
    # Remove default logger to configure our own
    logger.remove()

    # Choose log level based on environment
    # If in debug mode with a "debug" level, use DEBUG; otherwise, default to INFO.
    debug_level = debug_level.upper()

    if debug_level not in ["INFO", "DEBUG", "TRACE"]:
        logger.add(sys.stdout)
        msg = f"Invalid debug level {debug_level}, please choose between INFO, DEBUG, TRACE"
        raise ValueError(msg)

    # Add a sink for file logging and the console.
    logger.add(sys.stdout, level=debug_level, format=formatter)


def main() -> None:
    """Parse command-line arguments and run the GTK application."""
    parser = argparse.ArgumentParser(description="Fitness Tracker")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Generate dummy data for missing sensors and show full UI.",
    )
    parser.add_argument(
        "--debug-level",
        default="INFO",
        help="Set the logging level (default: INFO, options: INFO, DEBUG, TRACE)",
    )
    args = parser.parse_args()

    configure_logger(args.debug_level)

    # Delay importing UI/GI modules and GTK/Adw initialization until after args are parsed
    from fitness_tracker.ui.app import FitnessAppUI  # noqa: PLC0415

    app = FitnessAppUI(test_mode=args.test)

    # Convert Unix signals to a graceful quit so do_shutdown() runs
    GLib.unix_signal_add(
        GLib.PRIORITY_DEFAULT,
        signal.SIGINT,
        lambda *_args: (app.quit(), False)[1],
    )
    GLib.unix_signal_add(
        GLib.PRIORITY_DEFAULT,
        signal.SIGTERM,
        lambda *_args: (app.quit(), False)[1],
    )

    app.run(None)


if __name__ == "__main__":
    main()
