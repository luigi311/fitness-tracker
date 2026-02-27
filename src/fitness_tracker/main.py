import argparse
import signal
import sys

from gi.repository import GLib
from loguru import logger

from fitness_tracker.ui import FitnessAppUI


def configure_logger(debug_level: str = "INFO") -> None:
    # Remove default logger to configure our own
    logger.remove()

    # Choose log level based on environment
    # If in debug mode with a "debug" level, use DEBUG; otherwise, default to INFO.

    if debug_level not in ["INFO", "DEBUG", "TRACE"]:
        logger.add(sys.stdout)
        msg = f"Invalid debug level {debug_level}, please choose between INFO, DEBUG, TRACE"
        raise ValueError(msg)

    # Add a sink for file logging and the console.
    logger.add(sys.stdout, level=debug_level)


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
