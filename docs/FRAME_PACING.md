# Frame pacing: how input reaches the screen in 5.5 ms

How a MeltyGUI app on native Wayland gets from "input read" to "on the panel" in about
5.5 ms at 120 Hz, down from 16.5 ms. It has two halves: what the app does, and what the
compositor has to do for the app's timing to be true. The last section says what to
expect on compositors we have not patched.

Numbers here were measured on 2026-11-12 on one machine: NVIDIA, a 7680x2160 panel at
120 Hz (real rate 119.979 Hz), the patched Hyprland 0.56.2, a light test scene. Sections
marked "not measured" are reasoning, not results.

## The short version

| Stage | Input read to screen | What changed |
|---|---|---|
| Start | 16.5 ms | Input was read, the frame drawn, then `eglSwapBuffers` blocked until the compositor wanted a frame. |
| Wait before reading input | about 8.3 ms | The wait moved in front of the input read (`Backend.pace`, step 1). |
| Late start | about 5 ms | The frame starts as late as it can and still make the next refresh (step 2). |
| True on the panel | 5.5 ms | The compositor reports the panel's real flip time; before that the figure stopped at an internal draw. |

A frame period at 120 Hz is 8.33 ms. The remaining 5.5 ms is the drawing time (about
1.7 ms for the test scene) plus a 3.5 ms guard that the compositor needs to take the
frame, draw the desktop and flip (it tunes itself, see below; larger windows need more).

## App side (MeltyGUI)

All of it lives in `meltygui/core/windowing/backends/native_wayland.py`; the levers are
constants at the top of that file. `app.py` calls `glfw.pace(...)` at the top of the run
loop, before events are processed.

**1. Wait before the input is read, not after the frame is drawn.**
Each swap asks the compositor for a `wl_surface.frame` callback. `pace` waits for it
(handling events meanwhile) before the frame's input is read. The frame is then drawn
from input that is one wait fresher. `WAIT` (50 ms) bounds the wait for hidden windows,
which are never asked.

**2. Start late.**
Each swap also asks for `wp_presentation` feedback, which says when the frame was on
screen. From the last such time and the output's period, `pace` knows when the next
refresh is and waits until

    start = refresh - GUARD - cost

- `cost` is how long recent frames took to draw (`COSTS` frames of the last `FRESH`
  seconds, the `COST` share of them covered; the slowest tenth is left to the guard).
- `GUARD` (3.5 ms) is what the compositor needs between receiving the frame and the flip.
- If the next refresh is already out of reach, the frame aims at the one after it. A
  frame too slow to fit a period at all starts at once.
- The period comes from the compositor or the output mode, never from measuring frames:
  frames that miss would stretch it, and a stretched period spaces frames out further.

**3. The guard finds its own length: a base plus a lift.**
A frame missed when it was handed over in time and still came on screen later than the
refresh it was for. Frames that took longer to draw than expected are not judged (no guard
helps them), nor is the first frame after a pause or a slow frame. `Backend.tune` keeps two
things apart:

- **Lift, for what happens now.** `MISSES` (3) misses within `RECENT` (0.5 s) make the
  guard `MISS` (0.5 ms) longer at once. The lift fades to nothing over `FADE` (0.5 s): a
  disturbance costs latency for about half a second, then the guard is back where it was.
- **Base, for what the display needs.** Lifts that keep following each other (each within
  `AGAIN`, 2 s, of the one before) for `LEARN` (3 s) are no disturbance: the base gets
  `MISS` longer. After `REST` (10 s) it eases back towards `GUARD`, `STEP` (0.1 ms) every
  `CALM` (1 s), and never below `GUARD`: it does not search for the edge. Misses while
  easing put it back (no higher than where the easing began); when that repeats at about
  the same length the rest doubles, up to `REST_MAX` (2 minutes).

`GUARD` (3.5 ms) is where the base starts and what it returns to, capped at half a period
(2.1 ms at 240 Hz). Base and lift together stay under `LONG` (0.66 period, 5.5 ms at 120 Hz: longer bought nothing in the
measurement below and makes frames aim at every second refresh). A new refresh
rate starts over. Raise `MISSES` or lower `GUARD` for less latency and more stutters;
`ADAPT = False` pins the guard at `GUARD`.

The drawing time a frame is given comes from the frames of the last `FRESH` (0.5 s) only,
so a few slow frames stop counting half a second later, however sparse the frames are.

What the base settles at depends on the window. Measured 2026-11-12 on the panel with a
real pointer (240 moves a second): a small animating window holds at 3.5 ms; a 2130x1763
window with three nested windows needs 4.5 to 5 ms, because the compositor also waits for
the app's own GPU work (about 1 ms there, found by adding `glFinish`). With the guard
pinned during a drag in that window, frames handed over in time still missed at these
rates: 6 ms 0.4 %, 5.5 ms 0.9 %, 4.5 ms 2.5 %, 4 ms 21 %, 3.5 ms 69 %, 3 ms 96 %.

What the rule replaced, and why (same day, same window): misses used to be counted as "two
refreshes after the frame before", a slow count over 10 s with 0.1 ms eased per 4 s after
a 30 s rest. A frame that aims past the next refresh on purpose (guard plus drawing time
longer than a period) looked like a miss, so a long guard produced its own misses: it went
to `LONG` (6.25 ms) within 20 s of use and stayed there for the rest of the session (9 to
11 ms on screen, stretches at every second refresh).

**Turning it off.** `LATE = False` keeps step 1 only: about 8.3 ms, with nothing that
can miss.

**What it costs.** A frame that takes much longer than the ones before it misses its
refresh and shows 8.3 ms late. That is the trade: lower latency for a narrower margin.

**Measuring.** `tools/pace_calibrate.py [seconds]` steps the guard from 6 ms to 0.5 ms
and prints, per step, missed refreshes, mean and worst latency, and how far the
compositor's presented times stray from its period ("off grid"). The fps overlay's
"Present x ms" is the same measurement for the window being drawn.

| Guard | Missed | Input read to panel flip |
|---|---|---|
| 4.0 ms | 0 of 480 | 5.8 ms |
| 3.5 ms (set) | 1 of 479 | 5.5 ms |
| 3.0 ms | 5 of 476 | 5.0 ms |
| 2.5 ms | 13 of 467 | 4.6 ms |

## Compositor side (our patched Hyprland)

The app can only aim at the times the compositor reports. On this desktop every desktop
is a headless output and the panel shows a 1:1 mirror of the one being viewed, which
made those times wrong in ways a stock compositor on a real output does not have. The
patches live in `~/.local/opt/hyprland-hdr/patches/` and are described in `CATALOG.md`
there.

| Patch | What it does | Why the app needs it |
|---|---|---|
| `hyprland-panel-driven-desktop` | The viewed desktop is drawn inside the panel's frame, not on its own timer. | Without it the desktop and the panel run on two clocks and a frame waits up to a period for the copy. |
| `hyprland-deadline-desktop` | With `render:late_commit`, the desktop is drawn at a render deadline just before the flip, not right after the previous one. | A frame handed over 3.5 ms before the flip is in that flip. Drawn after the previous flip, it would wait a full period more. |
| `hyprland-panel-presented` | Clients of the viewed desktop get the panel's real flip time as "presented", and a damaged desktop asks the panel for a frame directly. | The late start aims at presented times. Before this they were the time of the deadline draw, which moves, and requests went through the desktop's 120.000 Hz timer while the panel runs 119.979 Hz. |
| `hyprland-cursor-pixel-lock` | Cursor and dragged windows use the same whole pixel (floor). | Not latency: it removed the visible cursor and window disagreement. |

Settings in use: `late_commit = true`, `new_render_scheduling = false`,
`frame_grace_ms = 1`, `commit_budget_ms = 0` (adaptive), `drag_commit_lead_ms = 1.659`.

**What went wrong without the last patch** (measured on the viewed desktop, fixed 3 ms
guard): bursts of 2 to 11 missed refreshes per second lasting about 8 s, and with the
commit budget pinned a stable state where every frame skipped a refresh (13 ms). An
agent desktop, which is not mirrored, was healthy throughout, so it does not reproduce
problems of the viewed one.

**A correction that matters when comparing numbers.** Before `hyprland-panel-presented`,
"presented" on the viewed desktop was the desktop's own headless commit, which does not
include the copy to the panel or the flip. The "6 ms" seen then was at least a frame
short of the truth. The 5.5 ms now is measured to the panel's flip.

**What the compositor still adds.** The render budget (the time before the flip at which
it draws) is the floor for `GUARD`. It is the slowest recent render plus a margin, at
least 0.7 ms. Its live value is not readable from outside, only its effect: guards
under 3 ms start missing.

## Other desktop environments

Not measured: none of this was run outside the patched Hyprland. What follows is what
the mechanism needs and where it is likely to differ.

**What the app needs from any compositor**

- `wl_surface.frame` callbacks: every Wayland compositor has them. Step 1 (about one
  period) should work everywhere.
- `wp_presentation` with real flip times: Mutter (GNOME), KWin (KDE), sway and other
  wlroots compositors, and stock Hyprland implement it. Without it the late start has
  nothing to aim at and `pace` stays at step 1.
- A known render deadline: this is the part that varies, and it sets the guard.

**Likely behaviour**

| Environment | Expectation | Why |
|---|---|---|
| GNOME (Mutter) | Step 1 works. Late start probably needs a longer guard. | Mutter starts its own frame a computed time before the flip and moves that time with its render cost, so the cut-off is not fixed. |
| KDE (KWin) | Similar to GNOME. | KWin also schedules its render late with an adaptive margin. |
| sway, wlroots compositors | Step 1 works. Late start depends on `max_render_time`. | By default sway draws right after the flip, so a late frame waits a full period: late start would then show about a period more, not less. |
| Stock Hyprland, real output | Step 1 works. Late start depends on its scheduling mode. | Without late commit it draws at the frame event, as sway does. |
| Variable refresh rate | Unknown. | There is no fixed grid to aim at; the period from the output mode is only the fastest rate. |
| X11, Windows, macOS, iOS | No pacing. | `pace` exists only in the native Wayland backend; other backends return at once. |

The general rule: the late start pays off only when the compositor draws close to the
flip. On a compositor that draws right after the previous flip, set `LATE = False`;
latency is then about one period and nothing can miss.

**How to find out on a new machine.** Run `tools/pace_calibrate.py 4` on the display in
question and read three things:

- "off grid" large (0.5 ms or more): the compositor's presented times are not real flip
  times, or its deadline moves. Late start will jitter there.
- Latency that does not fall as the guard shrinks, or sits a period higher than
  expected: the compositor draws early. Use `LATE = False`.
- Otherwise pick the shortest guard whose misses match the first rows and set `GUARD`.

The guard tunes itself per refresh rate, but it is one value for the whole app, not per
output: windows on two displays with different needs share the longer one.
