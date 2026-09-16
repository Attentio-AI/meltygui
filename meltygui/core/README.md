# Core: rendering plumbing and shared runtime

Core makes reusable render functions work: it supplies their inputs, tracks state,
runs converters, dispatches events, caches drawing and connects windows to the OS.
`Melty` owns the shared runtime. Feature rendering belongs in `view/<feature>_view.py`;
feature adapters belong in `model/<feature>_model.py`; injected feature state belongs
in `state/<feature>_state.py`.

## Where to start

| Responsibility | Files |
|---|---|
| Render-function execution, injection and caching | `core_render.py`, `core_render_helpers.py`, `parameter_core.py` |
| Global registries, scheduling and coordination | `melty.py`, `background.py` |
| Mode definitions, lazy handles and defaults | `mode.py`, `modes.py`, `mode_defaults.py` |
| Renderer registration, metadata and type matching | `render_funcs.py`, `func_metadata.py`, `shaped.py`, `render_dispatch.py` |
| Defaults, invalidation and window decorators | `core_decoration.py`, `invalidation_decoration.py`, `window_decoration.py`, `profile_decoration.py` |
| Generic stateful-data hosting and conversion | `render_host.py`, `bubbling.py`, `cache_tree.py`, `path_finder.py`, `chain.py`, `converter_register.py` |
| Dict-like object behavior and serialization | `dict_conversion.py`, `dict_conversion_util.py`, `data_decoration.py`, `dynamic_obj.py`, `load_save_v2.py`, `graph_compare.py` |
| Saved identifiers and live source relocation | `module_names.py`, `module_map.json`, `missing_saved_class.py`, `module_compatibility.py`, `legacy_modules.json`, `definition_hotswap.py` |
| App and window lifecycle | `app.py`, `app_session.py`, `surface.py`, `lifecycle.py`, `window_api.py`, `window_visibility.py` |
| Native windows and draw-data backends | `backends/`, `os_frame.py`, `titlebar.py`, `wayland_move.py`, `geometry_feed.py` |
| Input delivery and hit testing | `input_handler.py`, `pynput_backend.py`, `touchpad_backend.py`, `space_mouse.py`, `collision.py`, `mouse_cursor.py` |
| Settings, style, fonts and shared GL state | `toggles.py`, `settings.py`, `style.py`, `global_style.py`, `fonts.py`, `gl_state.py` |

The feature-named `*_core.py` files connect their views to shared services and
lifecycle. Keep local drawing and data adaptation in their feature modules.

## Shared presentation inputs

Render functions can declare `ui_scale` and `font_manager` in their signatures,
alongside the existing `style_manager` input. Core supplies the current runtime
scale and font manager; explicit scale/font-manager overrides are supported for
previews. These dependencies are excluded from saved view parameters and the
parameter controls. Views can use their own `draw_state.depth_and_layer` for
local drawing depth. This keeps feature presentation independent of `Melty`
lookups without making callers pass the same plumbing repeatedly.

## Why mode has three files

`mode.py` defines the real enum and its renderer/converter policies. `modes.py`
provides lazy `Modes.X` handles so decorators can refer to modes before their
renderers finish importing. `mode_defaults.py` holds shared type-to-mode defaults,
including delayed registration for optional dependencies. Combining these at
import time would recreate the mode/renderer import cycle.

The public package exports remain available from `meltygui`. Historical module
paths resolve to the same canonical module object. A live move adopts existing
definitions and state rather than executing initialization again. Saved identifiers
and source navigation use the same canonical names, including in a running session.

See [the move inventory and checks](../../docs/CORE_RELOCATION.md). The mixed
`state/new_core_model.py` and the text-editor implementation await the separate
editor/state refactor.
