import signal
from fitness_tracker.ui import FitnessAppUI


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = FitnessAppUI()
    app.run(None)


if __name__ == "__main__":
    main()
