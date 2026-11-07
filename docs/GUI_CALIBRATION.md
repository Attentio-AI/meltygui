# Runtime calibration for the Rust GUI prototype

The retained runtime now owns an opportunistic, local cost model. Consumers read
an immutable snapshot; views do not discover GPUs, load files, or start workers.
The independent [automatic raster policy](GUI_AUTO_CACHE.md) consumes these
snapshots and actual replay observations to select optional texture boundaries.

```python
costs = retained_gui.costs
resolve = costs.estimate('resolve', pixels=width * height)
upload = costs.estimate('upload', bytes=vertex_bytes + index_bytes)
# Independent timelines, not values to add to predict frame latency:
submission_ns = resolve.cpu_ns
elapsed_gpu_ns = resolve.gpu_ns
measured = resolve.cpu_measured, resolve.gpu_measured
```

A measured marker means the coefficients were fitted from observations. It does
not promise accuracy at every dimension or workload. The `revision` on a snapshot
changes when its baseline model changes. Existing snapshots remain valid values.
`RetainedGui(calibration=False)` keeps explicit defaults and disables measurement
and profile work. It is useful for comparisons and constrained hosts.

## What is measured

`gui_calibration.py` owns the model, scheduling, observations and profile worker.
The small native `texture::calibration` companion owns all GL queries and probe
resources on the context's render thread. The existing retained presentation,
input, retirement and close boundaries provide lifecycle hooks.

The baseline uses the actual desktop OpenGL cache configuration: single-sample
RGBA8 targets and the prototype's current resolve/composite shader. It measures:

| Operation | Scope | Model units |
| --- | --- | --- |
| allocate | Texture/FBO creation, storage specification and completeness check | Pixels |
| transition | Binding a different framebuffer | Fixed cost |
| upload | A streaming buffer allocation/upload through `glBufferData` | Bytes |
| clear | Clearing a color target | Pixels |
| resolve | The straight-alpha resolve quad | Pixels |
| composite | One alpha-blended textured quad | Pixels |

The three square dimensions are 32, 128 and 512 pixels. An idle sweep visits each
operation/size twice, one operation at a time. Resources never exceed these sizes;
there is no OOM search or memory-capacity benchmark. Allocation and upload include
driver behavior such as lazy backing: a timestamp does not prove all eventual
transfer/residency costs have already been paid. Offscreen preparation and host
GL-state save/restore are outside the operation interval. Actual maintenance-frame
wall costs, including that preparation, are measured separately below.

CPU submission and GPU timestamp intervals are fitted separately with nonnegative
fixed and scaled terms. Transition cost is fixed. Fitting needs at least three
valid observations and, for a scaled term, two distinct sizes. Each operation
retains at most 24 observations. Missing GPU observations keep the previous GPU
model or the explicit prior. Zero-length timestamp intervals are below timing
resolution and do not become claims of free GPU work. Nonfinite, negative and
one-second-or-longer observations are rejected. Elapsed GPU time can include
submission gaps, scheduling and driver effects; it is not a utilization reading.
The two clocks overlap and must not be added as a frame-time prediction.

The passive sampler arms at most one already-required cache replay per maintenance
tick. Repeated batches rotate the selected view. In legacy per-view mode a view interval includes its own
packet upload, clear, draw commands, child-texture composites and resolve. In
automatic mode the sampler can select a logical subtree in a shared pass; that
interval includes its submitted draw span, excluding the shared upload and
ancestor clear/resolve. Cached-reference quads are not sampled as underlying
view work. Child
replays are separate sequential operations, so a parent's sample does not also
include the child's rasterization. Nothing is rendered twice. At most 256 view
estimates are retained, with an exponential moving average and retirement cleanup.

This boundary does **not** time work performed by Python bodies, external
renderers, ray marchers, CUDA or presentation. A heavy shader executed before cache
replay is not measured merely because its texture is later composited. Integrators
can report an existing valid measurement through the observation seam:

```python
external_key = (view_id, 'external-body')
retained_gui.calibration.observe('external', pixels, cpu_ns, gpu_ns, view=external_key)
actual_external = retained_gui.calibration.views.get(external_key)
retained_gui.calibration.observe_reuse(view_id)
```

Keep external phases under a distinct observation key so their durations are not
blended with the automatic replay sample. The integrator retires that key with
`calibration.retire(external_key)` when its owner closes. Those hooks do not start
timers or rerender content. Reuse counts are an optional
policy observation hook, not a per-frame scan or an automatically collected full
reuse trace. Unmeasured external shader work remains unknown.

## Startup, idle rules and resource ownership

Construction only installs defaults. The first presentation never reads a profile,
creates a worker, fingerprints the GL context or submits calibration work. After
a one-second grace period, existing presentations may run maintenance, at most
once per 250 ms. No callback requests additional frames for calibration. A fully
sleeping surface may never finish a sweep, which is fine: saved models and defaults
remain usable without it. There is no recurring idle poll or GPU worker.

The scheduler first consumes completed file/query results and arms a future
passive replay. A probe is allowed only after two input-free seconds, with no
captured/active interaction or drag, and after less than 4 ms of retained frame
work. Keyboard, text, held keys/buttons, pointer movement and wheel events all
reset the idle grace period. At most one probe is submitted per tick; missed ticks
never accumulate a backlog. This is a bounded work rule, not a hard real-time
promise about driver-call duration. A probe failure postpones probes for an hour.

A sweep repeats after an hour, only on eligible existing frames. Eight consecutive
factor-of-four prediction mismatches for the same comparable baseline operation
can request an earlier idle refresh. The predictor for that streak is held fixed
while the observation model still updates. Changes in a view's content update its
own estimate immediately; they are not treated as hardware drift.

There are at most four pending timestamp pairs. Only a completed end timestamp
permits reading results; the earlier timestamp is then complete too. Production
never uses `glFinish`, blocking query-result reads, nested elapsed-time queries or
GPU readback. Availability is checked only at maintenance ticks. Results older
than five seconds are discarded on the next such tick. Closing/canceling the cache
deletes pending queries and resources on its owning context. The host must close
while that context remains current, as for its existing textures and programs;
this prototype does not add context-loss recovery. If a context has already been
lost/destroyed, `TextureCache.calibration_cancel(context_lost=True)` forgets the
sampler's owned handles without GL calls. The lost context reclaims its GL objects;
numeric names must never be deleted in a replacement context. That explicit hook
is tested with destruction of a separate real GL context. Detecting/recovering an
unexpected driver reset remains the existing host's responsibility.

Probe memory respects the cache's current byte budget, including pending probe
resources. Real target/scratch allocations cancel pending probes first, giving
rendering priority. Calibration does not discover a VRAM limit, change residency
budgets, persist a capacity promise, or reserve a share of system RAM/VRAM. A host
can also pass live `pressure=True` to maintenance to suppress active probes.

## Environment and local persistence

The current context supplies vendor, renderer, GL/driver and shader-language
versions, context flags/profile, the replay configuration version and its actual
physical-device UUID. The optional UUID function is resolved only when its
extension is advertised; it is not an extension-module link requirement. An absent,
ambiguous or unavailable UUID disables profile reuse/persistence for that context;
in-memory learning still works. No primary-GPU guess or hardware enumeration is
used, including on machines with identical GPUs.

A file worker adds bounded CPU vendor/family/model/stepping information from the
first 4096 bytes of `/proc/cpuinfo`, machine architecture, host/kernel and Python
implementation. It performs no subprocess probes or device scans. That environment
and the model version select a separate file under
`$XDG_CACHE_HOME/meltygui/gui-costs` (normally `~/.cache/meltygui/gui-costs`). Profile
reads are capped at 64 KiB. Unknown versions, different environments, malformed
values and profiles older than 30 days fall back to defaults.

A daemon worker loads and atomically replaces small JSON profiles. Rendering never
waits for it. More recent in-memory samples take priority over a late profile load.
Dirty data is saved at most every 30 seconds and on close when no file job is in
flight. Close saves are best effort and do not delay exit. Failed persistence leaves
rendering usable and retries on a later scheduled save. View observations, resource
handles and live pressure are never serialized into profiles or `AppSession`.

## Validation and measured overhead

See `tests/test_gui_calibration.py` for deterministic model, scheduler and profile
checks, and `tests/test_gui_calibration_gpu.py` for actual GL queue/resource,
state-preservation, separate-clock and unchanged-replay checks. The native baseline
and existing retained GPU tests run on a reserved desktop, with isolated XDG paths.
The performance harness is `tests/benchmark_gui_calibration.py`; it runs ordinary,
uninstrumented rendering and uses real scheduling time. Its GPU completion waits
are benchmark-only and outside CPU submission measurements.

Measured on 2026-11-07 with the NVIDIA GeForce RTX 4090 / driver 595.99.02.
Each iteration randomizes the order of disabled, defaults-only, passive-only and
idle-enabled scenes. Modes share the new component's small initialization and
lifecycle hooks: this comparison isolates measurement/persistence activity, not
all scaffolding relative to the pre-feature source. A benchmark-only 1 ms sleep
between iterations is outside the timers and supplies ordinary elapsed time.

| Workload / mode | CPU median | CPU p95 | GPU median |
| --- | ---: | ---: | ---: |
| 341-node hot, disabled | 105.87 us | 169.56 us | 4.096 us |
| 341-node hot, defaults | 105.73 us | 168.95 us | 4.096 us |
| 341-node hot, passive | 106.20 us | 168.61 us | 4.096 us |
| 341-node hot, idle | 105.75 us | 172.77 us | 4.096 us |
| 21-node redraw, disabled | 1255.66 us | 1674.09 us | 293.38 us |
| 21-node redraw, defaults | 1236.95 us | 1687.23 us | 282.62 us |
| 21-node redraw, passive | 1240.81 us | 1632.77 us | 274.43 us |
| 21-node redraw, idle | 1237.84 us | 1681.82 us | 286.72 us |

Hot scenes run 4,000 measured frames per mode and execute zero bodies; redraw
scenes run 1,000 and execute exactly 21 bodies each frame. All modes produce the
same pixels. Passive redraw sampling covers all 21 views, with 32 completed
intervals. Hot scenes have no new replays to sample, as expected.

The idle modes submit 32 hot-scene and 33 redraw-scene probes during these runs.
Frames that actually submit a probe cost more: hot CPU median **443.84 us**
(p95 578.16 us, max 1089.04 us); redraw CPU median **1574.46 us** (p95 1879.55 us,
max 2172.76 us). These totals include normal rendering and probe preparation.
They are measured costs of opportunistic work, not a claim of zero overhead.
Steady-state medians differ by less than the observed run variability.

Six fresh-process launches per mode, including imports, GL setup and the first
21-node usable scene, had medians **341.64 ms disabled / 345.51 ms enabled**.
Ranges overlap (329.64–366.17 / 328.52–353.23 ms); this small sample does not
establish a precise startup delta. Every launch asserts zero probes, no file job
and no environment lookup before first presentation. The separate visible
scrolling-lab smoke completed all 36 baseline observations, scrolled, selected a
row, hid/showed the nested panel and then kept body counts unchanged while idle.

The complete focused prototype suite passed **266 tests** after the context-loss
cancellation check was added. The evidence JSON contains per-frame CPU samples,
GPU summaries, startup samples, model snapshots, probe counts and pixel/body
assertions. It and the visible smoke screenshot are shared as Hub files.

Coverage is Linux desktop OpenGL on an NVIDIA RTX 4090. macOS, other GPU vendors,
CUDA, external-shader timing, native hot reload, explicit context-loss recovery,
non-RGBA8/MSAA cost models and a noneditable wheel are not validated here. This is
an extension of the opt-in prototype, not a release validation claim.
