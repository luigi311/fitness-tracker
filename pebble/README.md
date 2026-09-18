# Pebble companion

Workouts have three pages:

1. Workout target, remaining time or distance, and step position.
2. Current sensor readings.
3. Average sensor readings for the current step.

All workout pages retain the target bar and arrow, background target highlighting,
and live target guidance. The averages page still evaluates the target against
the current reading. Steps without a target keep the three pages but have no
target position or highlighting.

Free runs have two pages: current readings and averages for the entire recording.
The page position appears beside elapsed time.

| Button | Action |
| --- | --- |
| Up / Down | Previous / next page, wrapping at either end |
| Select | Cycle the large sensor value on sensor pages |
| Hold Up | Toggle the sensor grid / large value only |
| Hold Select | Toggle metric / imperial units |

Heart rate, pace, cadence, and power averages come from the phone's received
sensor samples. Each sensor has its own sample count; missing values are excluded
and zero values are included. Pace is calculated from mean speed. Preview and
paused samples are excluded. Step averages reset when advancing or manually
changing steps, including when returning to an earlier step. Free-run averages
cover the entire recording, including a workout that has finished.

Distance remains the session total on both sensor pages and is labeled `TOTAL`
on the averages page. Missing averages display `-`. The phone retains averages
when the watch reconnects; a lost link hides sensor values until updates resume.

Build with `pebble build` in this directory. The averages pages require both the
updated watch app and the updated Fitness Tracker application.
