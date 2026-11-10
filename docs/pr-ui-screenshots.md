# Pull request UI screenshots

Every PR runs **UI screenshots**, rendering the base commit (**Before**) and the
PR head commit (**After**) with the same capture harness. The six pages are
Tracker (with a populated workout library), a running workout, a Free Run,
History (with two recorded activities), History comparison (both activities
selected), and Settings.

The matrix is defined in `scripts/ui_screenshots/matrix.json`. Sizes are logical
application pixels; PNGs are saved at the corresponding panel resolution.

| Device | Scale | Portrait | Landscape |
| --- | --- | --- | --- |
| Librem 5 (720×1440 panel) | 300% | 240×480 | 480×240 |
| Librem 5 | 200% | 360×720 | 720×360 |
| Mecha Comet (1080×1240 panel) | 250% | 432×496 | 496×432 |
| Mecha Comet | 200% | 540×620 | 620×540 |
| Generic tablet | 100% | 800×1280 | 1280×800 |

This produces 60 before/after pairs (120 PNGs). These are application viewport
captures, including the app navigation, without operating-system panels or browser
chrome. Phone scaling is simulated by allocating the effective logical size and
scaling the GTK snapshot to the panel size. Xvfb does not emulate a physical
Wayland compositor or device input. Content that scrolls is captured at its initial
position. Existing clipping is visible in the images; captures do not fail simply
because a page does not fit.

Captures use real production GTK pages and shell layout with fixed sample
readings, workout files, dates, metric units, font, DPI and dark theme. The capture
process creates temporary XDG directories and a private SQLite database. It
constructs session widgets without starting the recorder, simulator timers,
Bluetooth scans, location services or integrations. History and comparison
background results must finish before capture. Each checkout uses its own frozen
Python dependencies.

## Viewing results

**Publish UI screenshots** updates one bot comment on the PR. There are five
separate tables, grouped by device and scale: **Librem 5 · 300%**, **Librem 5 · 200%**,
**Mecha Comet · 250%**, **Mecha Comet · 200%**, and **Tablet · 100%**. Each table has
**Page | Before | After** columns. The Page cell identifies the page, portrait or
landscape orientation, and logical viewport size. All six pages appear in both
orientations within their group. The tables are visible without expanding a section.
The comment identifies both commit SHAs and links to the capture run. Its
`ui-screenshots` artifact contains the full-resolution PNGs for 30 days.

For inline PR images, the publisher creates an images-only `ui-screenshots`
branch, storing files below `pr-<number>/before/` and `pr-<number>/after/`. URLs
include the image commit SHA, so they continue to work after later captures and
after artifacts expire. Images for other PRs are retained. PNGs and previous
image revisions remain in that branch's Git history, increasing repository storage.
No external image service or extra secret is required. Repository rules must
allow `GITHUB_TOKEN` to write this branch and PR comments.

The publisher starts working once **both workflows and their helpers have merged
into the repository's default branch**. See [GitHub's workflow_run behavior][workflow-run].
The initial PR can still generate and upload its screenshot artifact. Subsequent pushes update the comment; closed PRs
and superseded base/head revisions are skipped. Updating the PR's base branch
alone does not trigger a capture; synchronize the PR branch to refresh the comparison.

## Image-size budget

The publisher accepts at most **3 MiB per PNG** and **32 MiB of PNG data per
publication**, including both Before and After. It rejects an oversized set
before writing images or updating the PR comment. Metadata is separate from
this image budget. Existing image URLs continue to use their commit SHA.

The limit is based on two normal local Xvfb/Cairo captures of `main` at
`7c99fef`, using the `0f38fa3` capture harness and the complete six-page,
ten-layout matrix. Each 60-image capture measured **5,896,143 bytes**; together,
the 120 PNGs measured **11,792,286 bytes (11.25 MiB)**. The largest PNG was
**171,414 bytes**, the Comet 250% portrait history comparison. These are raw PNG
file sizes, before ZIP or Git compression.

Twice the measured publication size is 23,584,572 bytes. Rounding that up to the
next power of two gives **33,554,432 bytes (32 MiB)**, about 2.85 times the normal
baseline. Re-measure normal captures when changing the matrix or rendering setup
before adjusting this limit.

## Workflow permissions

The `pull_request` capture workflow has only `contents: read`, including on forks.
It can execute PR code but has no publication credentials. The separate
`workflow_run` publisher runs default-branch code with artifact read, contents
write and PR write permissions. It never executes code from the PR or artifact.
It validates workflow provenance, live PR identity and base/head SHAs, then reads
only the known PNG paths with expected dimensions and size limits. Symlinks are
rejected. Git publication uses a fresh object database without checking out the
image branch. The comment is updated only while the same PR revision remains
current.

## Local capture and checks

Install the same native libraries as the capture workflow (GTK 4, libadwaita,
Graphene, Cairo, Xvfb, D-Bus, DejaVu fonts and Noto color emoji), then run:

```sh
uv sync --frozen --no-dev
dbus-run-session -- xvfb-run -a -s '-screen 0 1600x1600x24 -nolisten tcp' \
  uv run --no-sync python scripts/ui_screenshots/capture.py --output /tmp/ui-screenshots
```

To capture one page/layout, append these optional, repeatable arguments:

```sh
--page workout --viewport librem5-300-portrait
```

The harness may live outside the target checkout; select the target revision's
virtual environment with `uv run --project /path/to/checkout --no-sync python
/path/to/harness/capture.py --output /tmp/screenshots`. This is how CI captures a
base revision that predates the harness.

Run publication tests without installing application dependencies (Node.js 24):

```sh
node --test scripts/ui_screenshots/publish.test.cjs
```

The tests cover the matrix, artifact validation, same-repository and fork PR
identity checks, stale runs, per-image and aggregate byte limits, comment image
coverage, and local Git publication without network access or GitHub writes.

## Refreshing documentation screenshots

To replace the six documentation images, run the same native capture harness
with its documentation preset:

```sh
dbus-run-session -- xvfb-run -a -s '-screen 0 1600x1600x24 -nolisten tcp' \
  uv run --no-sync python scripts/ui_screenshots/capture.py \
  --documentation --output docs/screenshots
```

This captures Tracker, a running Free Run, a running workout, a cycling trainer
workout, History, and History comparison. Images use the existing filenames and
the Librem 5 200% portrait viewport: 360×720 logical pixels, saved at 720×1440.
They contain the application content without desktop or phone system bars, using
the same isolated sample data and dark theme as PR captures. The documentation
preset leaves the PR capture matrix unchanged. The README uses these files
directly. AppStream image URLs remain pinned to the existing release and can be
updated with the next release.

[workflow-run]: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run
