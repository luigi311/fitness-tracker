import argparse
import json
import signal
import sys
from dataclasses import asdict, is_dataclass
from enum import Enum

from gi.repository import GLib
from loguru import logger
from loguru._defaults import LOGURU_FORMAT

from fitness_tracker.ui import FitnessAppUI


def _json_default(o):
    # dataclasses
    if is_dataclass(o):
        return asdict(o)
    # enums
    if isinstance(o, Enum):
        return o.value  # or o.name
    # anything else: fallback to string
    return str(o)

def formatter(record) -> str:
    def_format = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
        "{level: <8} | "
        "{name}:{function}:{line} - "
        "{message}"
    )

    base = def_format.format_map(record)
    data = record["extra"].get("data", "")

    if isinstance(data, dict) or hasattr(data, "__dataclass_fields__"):
        data_str = json.dumps(data, indent=4, default=_json_default)
        lines = [line.rstrip() for line in data_str.splitlines()]
        lines.insert(0, "")
    elif isinstance(data, list):
        lines = [f"{item}" for item in data]
        lines.insert(0, "")
    else:
        lines = [str(data)]

    indent = "\n  " + (" " * (len(base.replace(record["message"], "").strip()) + 1))

    record["extra"]["formatted_data"] = indent.join(lines)
    return LOGURU_FORMAT + "{extra[formatted_data]}\n{exception}"


def configure_logger(debug_level: str = "INFO") -> None:
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


def main():
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

    app = FitnessAppUI(test_mode=args.test)

    # Convert Unix signals to a graceful quit so do_shutdown() runs
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT,  lambda *a: (app.quit(), False)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda *a: (app.quit(), False)[1])

    app.run(None)


if __name__ == "__main__":
    main()
