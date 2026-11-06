# Responsive session UI

The same session widgets remain alive during a resize or rotation. Layout changes
do not restart recording, reset the step timer, or send trainer commands. Layouts
use libadwaita breakpoints in logical pixels (`px`) and text-scaled pixels (`sp`),
rather than physical display resolutions or device names. Only the last matching breakpoint
applies, so each layout specifies all of its deviations from the default.

| Available session area | Presentation |
| --- | --- |
| Narrow portrait | Gauge above step details; two metric columns |
| At least 480sp wide, near-square | Gauge beside step details; three metric columns |
| 350–599 logical pixels wide and at most 560sp high | Compact gauge beside step details; three metric columns; title and next-step preview yield space |
| At least 350 logical pixels wide, below 480sp wide and at most 600sp high, or at most 400sp high | The same compact layout covers intermediate text scales and rotated near-square displays |
| Below 350 logical pixels wide and at most 560sp high | Compact hero, stacked timers, and two metric columns |
| At least 600sp wide and aspect ratio at least 1.3 | Guidance beside metrics; two metric columns |
| At least 700 logical pixels / 420sp wide, at most 440sp high, aspect ratio at least 1.6 | Guidance beside a compact three-column metric grid, including at enlarged text sizes |
| At least 900×700 logical pixels, aspect ratio at most 1.3 | Double-size text and controls throughout the session, with the dashboard filling the available height |
| 420–519 logical pixels wide and 300–560px high | A consistent 80% density for near-square high-scale allocations, with three metric columns |
| Below 280 logical pixels wide | A consistent two-thirds density with the portrait hierarchy and two metric columns |
| 400–599px wide, at most 280px high, aspect ratio at least 1.6 | The same two-thirds density with guidance beside three metric columns |

For example, a 1080×1240 display can provide roughly 540×620 logical pixels at
2× scaling or 360×413 at 3× scaling, before shell bars. The compact layout also
handles reduced height from those bars. At unusually narrow sizes or enlarged
text, scrolling takes priority over shrinking touch targets. The recording footer
stays outside the scroll area. Its actions share a row from 300sp upward and stack
below that width. Very narrow windows can also keep a single row when the text
fits, and stack actions when text is enlarged.

The large near-square layout uses the 540×620 Comet layout at 2× as its readability
reference. At 1080×1240 and 1×, captions, timers, target text, metric values, units,
sensor labels, trainer controls and recording actions all grow together. Recording
actions are 96 logical pixels high, giving the same panel-pixel height as 48px at
2×. Gauge labels and chart labels scale with the dashboard while retaining their
overlap and clipping checks. Large near-square windows also enlarge navigation.
Returning to a smaller allocation restores the regular text and control sizes.

Comet at 2.5× uses an 80% density, and Librem 5 at 3× uses a two-thirds density.
Text, gauge geometry, chart labels, tile padding, navigation and touch targets
scale together. This keeps their physical sizes close to the 2× reference:
40px controls at 2.5× occupy 100 panel pixels, while 32px at 3× occupy 96 panel
pixels, compared with 48px at 2× occupying 96. The application minimum width is
200px, allowing a 240px Librem portrait viewport without horizontal clipping.
All six metrics and target guidance remain visible at normal text size in both
orientations. Enlarged text can scroll while recording actions remain visible.
Activity setup also uses the two-thirds density at the Librem's 3× allocations.
Portrait keeps the environment cards within the narrow window; short landscape
places selection beside the workout list, with the free-session action visible.

Workouts show Elapsed, Remaining, and Step (for example, `5/15`) together.
The counter follows the executable step sequence through preview, automatic
progression, and previous/next navigation. When width is limited, the two time
values stack beside the step counter. Free sessions hide the workout readouts.

The gauge shows the current reading against the target band without a separate
target-status row. Its complete painted bounds, including labels and needle,
are centered horizontally and vertically inside the frame. Compact layouts draw
the gauge about 10% smaller. Both text lines scale together to stay clear of the
arc and each other. The hold-to-stop fill stays vertically centered within its button.
The gauge's accessible description includes deviation from the closest
band edge, using seconds per selected distance unit for pace. An untargeted step
clears the previous gauge. A disconnected target source hides the reading and
needle. Offline live metrics show a dash; accumulated distance remains readable.
Location tracking uses the same live metric indicators without a separate status row.
Session toasts appear within the dashboard's scroll area, above the recording
footer. Step announcements use a short step/count message; the target remains
in the guidance area and the full desktop notification.
Moving a held pointer or touch outside Stop cancels the countdown, including
during GTK's implicit grab. Moving back inside requires a fresh press. The fill
and label retain contrast in light and dark themes.

Trainer controls appear directly when the available width and height can fit
the target, all metrics, and the full adjustment panel. On smaller allocations or
with larger text, they use a disclosure row showing the current mode/value.
Its touch size follows the session density.
The same control widgets and selected bias survive this change without sending
trainer commands. Incline adjustments retain their disclosure row. Disclosures
have space above the recording footer and scroll controls into view when expanded.
Workout charts retain their previous visibility
policy; free sessions place the chart after portrait metrics and beside landscape
metrics. Chart margins update on resize, keeping layout work out of each live redraw.
Larger windows use header navigation
instead of displaying both navigation bars. Short landscape windows keep the
compact bottom navigation. Activity setup also uses side-by-side panes in wide
or short landscape windows, with keyboard-accessible environment toggles.
Smaller vertical gaps and card padding leave more room for the workout list.
Environment cards retain the original house, tree and lightning emoji icons.

## Verification

Run the normal suite with `uv run pytest`. Native GTK tests opt in so CI without a
display can continue running the existing headless tests:

```sh
xvfb-run -a env FITNESS_TRACKER_GTK_TESTS=1 uv run pytest tests/test_responsive_layout.py
```

On an existing display, omit `xvfb-run -a`. Add
`FITNESS_TRACKER_SCREENSHOTS=/tmp/fitness-layouts` to save sample dashboard renders.
The native tests cover portrait and landscape, reduced shell height, Comet sizes
at 1×/2×/3×, free sessions, workouts, trainer and incline controls, outdoor sessions,
larger text, rotation without state changes, stop-hold cancellation, drawn gauge
text clearance, stop-fill centering, and direct trainer access when space allows.
They also compare the physical text and recording-control sizes of the Comet's
1× and 2× layouts and check chart label bounds at both densities.
High-scale coverage includes Comet at 2.5× and Librem 5 at 3×, their physical text
sizes against 2× references, enlarged-text scrolling, and rotation through all
four high-scale allocations without recording-control callbacks.
Additional checks cover toast hit-testing in the production shell and populated
Librem 3× setup in both orientations. Stop feedback is checked for contrast.
To exercise real pointer cancellation on an isolated X server, also set
`FITNESS_TRACKER_XINPUT_TESTS=1`; this requires the X11 and XTEST libraries.

These are desktop GTK allocation and behavior checks. Compositor scaling, touch
input, and rendering performance still need confirmation on physical devices.
