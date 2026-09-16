# Core: rendering plumbing and shared runtime

Core makes reusable render functions work: it supplies their inputs, tracks state,
runs converters, dispatches events, caches drawing and connects windows to the OS.
`Melty` owns the shared runtime. Feature rendering belongs in `view/<feature>_view.py`;
feature adapters belong in `model/<feature>_model.py`; injected feature state belongs
in `state/<feature>_state.py`.

## Where to start

The root keeps six Python modules: `core_render.py`, `melty.py`,
`definition_hotswap.py`, `module_compatibility.py`, `module_names.py`, and the
package initializer. The two JSON manifests describe canonical names and aliases.
Everything else is grouped by the runtime responsibility it serves:

| Folder | Responsibility and main entry points |
|---|---|
| `input/` | Event delivery, devices, hit testing, drag/drop and selection: `input_handler.py`, `collision.py`, `drag_drop_core.py` |
| `rendering/` | Render dispatch, registration, parameter injection support, modes and decorators: `render_dispatch.py`, `parameter_core.py`, `mode.py` |
| `conversion/` | Dict-like objects, conversion graphs, hosting and persistence: `dict_conversion.py`, `render_host.py`, `load_save_v2.py` |
| `cache/` | Drawing caches and invalidation: `tile_cache.py`, `invalidation_tracker.py` |
| `windowing/` | Surface lifecycle, native windows, chrome and platform backends: `surface.py`, `window_api.py`, `backends/` |
| `graphics/` | Shared GL resources, shaders, overlays, capture and tensor/graph integration: `gl_state.py`, `shader_func.py`, `lut_core.py` |
| `layout/` | Cursor, grid, column, header and dropdown plumbing |
| `styling/` | Shared styles, colours, fonts and font warmup |
| `files/` | Filesystem polling, metadata and file/import-tree integration |
| `runtime/` | App/session lifecycle, scheduling, settings and shared process helpers |
| `diagnostics/` | Notifications, profiling, tracing, inspection and diagnostics integration |
| `automation/` | Orchestration, actions, queries, search and MCP integration |
| `services/` | Terminal, chat and account runtime integration |

These folders organize wiring; they do not turn feature algorithms or local
presentation into core code. Some inherited integration modules remain mixed;
see [the outstanding ownership review](../../docs/ARCHITECTURE_DEBT.md).

## Shared presentation inputs

Render functions can declare `ui_scale` and `font_manager` in their signatures,
alongside the existing `style_manager` input. Core supplies the current runtime
scale and font manager; explicit scale/font-manager overrides are supported for
previews. These dependencies are excluded from saved view parameters and the
parameter controls. Views can use their own `draw_state.depth_and_layer` for
local drawing depth. This keeps feature presentation independent of `Melty`
lookups without making callers pass the same plumbing repeatedly.

Palette consumers declare `luts`. Core injects the shared `LutPalette` from
`Melty.luts`, or accepts an explicit override, and subscribes cached consumers
before the render-cache gate. `luts.texture(name)` is an integer-like texture ID:
the model handles lazy uploads, updates and per-context storage. There is no
palette host or separate resource service. `GLState` releases context resources
when a surface closes. Palette values and proxies belong in `model/lut_model.py`;
selection and swatches belong in `view/lut_view.py`.

## Why mode has three files

`rendering/mode.py` defines the real enum and its renderer/converter policies. `rendering/modes.py`
provides lazy `Modes.X` handles so decorators can refer to modes before their
renderers finish importing. `rendering/mode_defaults.py` holds shared type-to-mode defaults,
including delayed registration for optional dependencies. Combining these at
import time would recreate the mode/renderer import cycle.

The public package exports remain available from `meltygui`. Historical module
paths resolve to the same canonical module object. A live move adopts existing
definitions and state without executing initialization again. Matching destination
code updates resource paths while preserving live objects. Historical package
names are virtual namespaces, so obsolete directory shells are unnecessary. Saved identifiers
and source navigation use the same canonical names, including in a running session.

See [the move inventory and checks](../../docs/CORE_RELOCATION.md). The mixed
`state/new_core_model.py` and the text-editor implementation await the separate
editor/state refactor.
