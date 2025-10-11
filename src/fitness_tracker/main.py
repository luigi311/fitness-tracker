import argparse
import signal

from gi.repository import GLib

from fitness_tracker.ui import FitnessAppUI


def main():
    parser = argparse.ArgumentParser(description="Fitness Tracker")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Generate dummy data for missing sensors and show full UI.",
    )
    args = parser.parse_args()

    app = FitnessAppUI(test_mode=args.test)

    # Convert Unix signals to a graceful quit so do_shutdown() runs
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT,  lambda *a: (app.quit(), False)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda *a: (app.quit(), False)[1])

    app.run(None)


if __name__ == "__main__":
    main()
