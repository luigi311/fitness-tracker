# Responsive interface

Layout selection uses the aspect ratio of the application window before any
internal scaling. Display zoom, device names, absolute pixel dimensions, and font
size do not choose different panel arrangements.

| Window width / height | Session arrangement | Reference canvas |
| --- | --- | --- |
| Below 0.80 | Gauge, timers and step counter, live metrics, then equipment controls | 360×720 |
| 0.80 through 1.35 | Gauge and timers side by side; metrics and equipment controls side by side below | 540×620 portrait; 620×540 landscape |
| Above 1.35 | Gauge above timers on the left, live metrics in the middle, equipment controls on the right | 720×360 |

Balanced sessions without equipment use the lower area for the metrics. Free
sessions hide workout guidance and show elapsed time and the live chart; portrait
places metrics before the chart, while landscape puts the chart beside metrics.
Setup and History stack their sections in portrait, including balanced portrait,
and use side-by-side panes in landscape, including balanced landscape.

A small aspect-ratio hysteresis of 0.015 keeps integer allocation rounding from
switching layouts at a boundary. Crossing an aspect boundary or rotating a window
can change the arrangement. Changing zoom while preserving aspect does not.

## Uniform scaling

`core/layout.py` selects the layout and fits its reference canvas inside the
window. The remaining axis extends to fill the available area. The `ScaledView`
GTK container allocates the existing application in those reference coordinates
and applies one uniform transform to it. Text, gauge drawing, chart axes, spacing,
controls and bottom navigation therefore scale together. Native popover
contents receive the same transform within their own surfaces. Charts render
at the combined native and interface scale to keep their backing images sharp.

For example, a 720×1440 Librem display provides approximately 720×1440 logical
pixels at 100%, 360×720 at 200%, and 240×480 at 300%. The interface transforms are
respectively 2, 1, and two-thirds, so a 48-unit control paints at approximately 96
panel pixels at all three zoom settings. Intermediate percentages follow the same
calculation. A 1080×1240 Comet uses the balanced reference and gives the same
physical geometry at 100%, 200%, and 250%.

GTK handles input through the same transform, keeping hit testing aligned with
painted controls. System window borders, compositor bars, and external system
dialogs retain system sizing. If those bars change the available window aspect,
a layout change is possible. Physical devices still need verification of compositor
behavior, touch input, and rendering performance.

Accessibility font enlargement remains independent of display zoom. It enlarges
text within the chosen arrangement; content can scroll horizontally or vertically
rather than moving equipment controls to another panel. Recording actions remain
outside that scroll area. Previous/Next remain in the recording footer. Portrait
uses two footer rows; balanced and landscape use one row. Global Tracker, History,
and Settings navigation stays at the bottom. The in-app title bar stays hidden in
every arrangement, reclaiming its height without depending on display zoom.

## Session behavior

The same widgets remain alive during zoom, resize, and rotation. Reflow does not
restart recording, reset step timers, change trainer targets, or invoke controls.
Trainer bias and selected modes survive movement between panels. Trainer modes and
adjustments remain directly available in every arrangement. Mode choices use up to
three columns in portrait and two in other arrangements; enlarged text can use
fewer columns inside the same panel. Compact mode-button padding, portrait
metric-tile margins, and vertical gaps retain two
metric columns, all six readings, and the full trainer panel. Portrait shows
source indicators and Live/Dead text beside each number. Square and landscape
layouts keep that information underneath the value. Filled green Live markers
mean connected; hollow Dead markers mean disconnected, with full connection details
in tooltips and accessible labels. Numeric text sizes and 48-unit touch heights
are retained. Portrait incline sessions keep a disclosure showing the current
value. Enlarged text can scroll those same panels.

Workouts show Elapsed, Remaining, and Step (for example `5/15`). The counter follows
the executable step sequence through preview, automatic progression, and previous
or next navigation. Free sessions hide workout readouts. Step changes retain the
full desktop notification without an in-app step toast. Other session notices stay
above the recording footer.

The gauge shows the current reading against the target band without a separate
status badge. Its painted bounds stay centered, and its text is fitted clear of
the arc and the other label. Its accessible description includes deviation from
the closest band edge. Untargeted steps clear previous guidance. A disconnected
source hides the reading and needle. Offline metrics show dashes while accumulated
distance remains readable. Location has no separate debugging status row.

Hold-to-stop remains a 1.5-second hold. Resize, zoom, rotation, unmapping, release,
and movement outside the button cancel a pending hold. The progress fill remains
centered and contrasted in light and dark themes. Workout chart visibility and
the existing live-chart redraw cadence are unchanged.

## History and setup

History keeps Activities and Compare controls above its content, with global
navigation below. Portrait places filters, totals, and cards in one scroll area.
Landscape places filters and totals beside an independently scrolling list.
Selection, filters, sorting, unit conversion, asynchronous queries, and export
retain their behavior across reflow. Comparison controls and legends sit beside
the plot in landscape and use the selected-activities menu in portrait. Chart
margins update on resize rather than performing a layout calculation every redraw.

Activity setup keeps its keyboard-accessible sport and environment selectors and
the original house, tree, and lightning icons. Its free-session action stays
outside the setup scroller, while the workout library scrolls independently.

## Verification

Run the regular suite with `uv run pytest`. Native GTK checks opt in before
importing GTK, so environments without a display skip those modules:

```sh
xvfb-run -a env FITNESS_TRACKER_GTK_TESTS=1 uv run pytest tests/test_responsive_layout.py tests/test_history_responsive_layout.py
```

Pure policy tests cover every integer display percentage from 100% through 300%,
aspect boundaries, and allocation rounding. Native tests check actual transformed
allocations across 100%, 115%, 125%, 150%, 175%, 200%, 250%, and 300%; fonts, touch
bounds, panel parents, sensor status, callbacks, and stop cancellation must remain
consistent. Other cases cover free sessions, workouts, incline and trainer controls,
enlarged text, rotation, no-signal states, chart labels, gauge clearance and
centering, navigation, setup, History, selection, and notification hit testing.

Set `FITNESS_TRACKER_SCREENSHOTS=/tmp/fitness-layouts` to save sample native renders.
To exercise real pointer cancellation on an isolated X server, also set
`FITNESS_TRACKER_XINPUT_TESTS=1`; this requires X11 and XTEST libraries. These desktop
checks do not replace physical-device testing.
