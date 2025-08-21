import signal
import argparse

from fitness_tracker.ui import FitnessAppUI


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    parser = argparse.ArgumentParser(description="Fitness Tracker")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Generate dummy data for missing sensors and show full UI.",
    )
    args = parser.parse_args()

    app = FitnessAppUI(test_mode=args.test)
    app.run(None)


if __name__ == "__main__":
    main()
