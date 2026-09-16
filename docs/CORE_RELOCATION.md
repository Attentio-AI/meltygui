# Shared core relocation

Completed 2026-09-16. Shared runtime modules now live in `meltygui/core/`.
The [core guide](../meltygui/core/README.md) groups the entry points by responsibility.

## Current import policy

The temporary import-compatibility layer has been removed. The library,
`meltygui_pro` and `melty_code_editor` use canonical imports. The alias finder,
its 252-name manifest, forwarding exports and shim modules are gone. Built-in
view registration is explicit in the mode module. Folder window wiring lives
in `core/files/file_core.py`.

`module_names.py` and `module_map.json` only translate saved identifiers;
`definition_hotswap.py` still preserves live definitions and runtime state.
The editor implementation has only mechanical import changes. Validation after
removal: 599 library tests and 391 subtests passed, including CUDA/GL coverage;
208 Pro tests passed with one skipped. The built wheel rejects all 252 historical
module paths while exposing the public views and restoring saved mode identifiers.
A fresh isolated Melt editor rendered and searched a Python file and opened its
file picker. The remaining sections record earlier migration stages,
when temporary aliases existed.

## Responsibility folders

A second pass moved 121 modules within core into 13 responsibility folders. See
[the core guide](../meltygui/core/README.md) for their boundaries and entry points.
`core_render.py`, `melty.py`, the three relocation/name modules and `__init__.py`
remain at the root. Internal imports and resource paths use the new locations.

Compatibility includes both generations of historical names. The finder adopts
any already-loaded alias, updates package search paths and matching destination
code, and preserves module/class/function identity without rerunning module
initialization. Deleted legacy parent packages resolve through virtual namespaces.
The obsolete `rendering/`, `views/`, `widgets/` and `windows/` shells are gone.

A running native app adopted the move while preserving all loaded core module
identities, its edited slider/slice values, LUT texture proxy and injected GLState.
The full suite passed 609 tests and 396 subtests, including CUDA/GL coverage.
Tests cover repeated relocation, removed parent packages, package child paths and
changed resource-path code. The built wheel includes all moved modules and backend
assets; historical imports and resource lookup work from that wheel. A fresh
isolated Melt editor rendered a Python file and handled in-file search. The 17 deferred
editor files remain unchanged.

The sections below record the earlier move into core; their destination links
have been updated to the final grouped locations.

## Scope

- 82 whole modules moved, with line layout preserved for live source relocation.
- Imports in framework code, examples, tools and tests use canonical locations.
- Wayland protocol files and their backend license moved with the backends.
- Font resources remain in the package resources directory; package-root lookup follows the new location.
- Historical imports and saved identifiers resolve to canonical modules and classes.
- Source navigation resolves those same identifiers before looking up files, so
  old imports and cached paths still lead to the moved definitions. A live move
  also updates the held saved-name map in place.
- All 17 editor files, including `view/text_view.py`, are byte-for-byte unchanged.
- `state/new_core_model.py` remains in place because it mixes framework and editor/feature state.

## Validation

- 520 tests and 384 subtests passed. CUDA/direct-GL suites were excluded.
  Final compatibility checks also passed after adding manifest-refresh coverage.
- A process loaded all 82 modules before the move, then adopted the canonical paths: module,
  function, class, mode, renderer-registry and runtime-state identities were preserved.
- All 130 old-to-canonical module aliases (including the earlier view/widget moves) resolve identically.
- New checks cover lazy mode loading, mode/class persistence, editable source addresses,
  old-import navigation, live saved-name maps, fonts and protocol assets.
- Reinstalling aliases in a running process reads the updated manifest and
  preserves already-loaded modules and their runtime state.
- An isolated Melt instance started successfully; text rendering, in-file search
  and the Open File dialog worked.

## Module moves

| Previous module | Canonical module |
|---|---|
| `meltygui.app` | [`meltygui.core.runtime.app`](../meltygui/core/runtime/app.py) |
| `meltygui.app_session` | [`meltygui.core.runtime.app_session`](../meltygui/core/runtime/app_session.py) |
| `meltygui.background` | [`meltygui.core.runtime.background`](../meltygui/core/runtime/background.py) |
| `meltygui.code.bubbling` | [`meltygui.core.conversion.bubbling`](../meltygui/core/conversion/bubbling.py) |
| `meltygui.code.cache_tree` | [`meltygui.core.conversion.cache_tree`](../meltygui/core/conversion/cache_tree.py) |
| `meltygui.code.chain` | [`meltygui.core.conversion.chain`](../meltygui/core/conversion/chain.py) |
| `meltygui.code.converter_register` | [`meltygui.core.conversion.converter_register`](../meltygui/core/conversion/converter_register.py) |
| `meltygui.code.path_finder` | [`meltygui.core.conversion.path_finder`](../meltygui/core/conversion/path_finder.py) |
| `meltygui.code.render_host` | [`meltygui.core.conversion.render_host`](../meltygui/core/conversion/render_host.py) |
| `meltygui.collection_action` | [`meltygui.core.automation.collection_action`](../meltygui/core/automation/collection_action.py) |
| `meltygui.collision` | [`meltygui.core.input.collision`](../meltygui/core/input/collision.py) |
| `meltygui.debug.attribute_churn` | [`meltygui.core.diagnostics.attribute_churn`](../meltygui/core/diagnostics/attribute_churn.py) |
| `meltygui.debug.framebuffer_recorder` | [`meltygui.core.graphics.framebuffer_recorder`](../meltygui/core/graphics/framebuffer_recorder.py) |
| `meltygui.debug.invalidation_tracker` | [`meltygui.core.cache.invalidation_tracker`](../meltygui/core/cache/invalidation_tracker.py) |
| `meltygui.debug.mode` | [`meltygui.core.rendering.mode`](../meltygui/core/rendering/mode.py) |
| `meltygui.events.input_handler` | [`meltygui.core.input.input_handler`](../meltygui/core/input/input_handler.py) |
| `meltygui.events.pynput_backend` | [`meltygui.core.input.pynput_backend`](../meltygui/core/input/pynput_backend.py) |
| `meltygui.events.space_mouse` | [`meltygui.core.input.space_mouse`](../meltygui/core/input/space_mouse.py) |
| `meltygui.events.touchpad_backend` | [`meltygui.core.input.touchpad_backend`](../meltygui/core/input/touchpad_backend.py) |
| `meltygui.extensions` | [`meltygui.core.runtime.extensions`](../meltygui/core/runtime/extensions.py) |
| `meltygui.fonts` | [`meltygui.core.styling.fonts`](../meltygui/core/styling/fonts.py) |
| `meltygui.func_metadata` | [`meltygui.core.rendering.func_metadata`](../meltygui/core/rendering/func_metadata.py) |
| `meltygui.gc_manager` | [`meltygui.core.runtime.gc_manager`](../meltygui/core/runtime/gc_manager.py) |
| `meltygui.geometry_feed` | [`meltygui.core.windowing.geometry_feed`](../meltygui/core/windowing/geometry_feed.py) |
| `meltygui.gl_state` | [`meltygui.core.graphics.gl_state`](../meltygui/core/graphics/gl_state.py) |
| `meltygui.global_style` | [`meltygui.core.styling.global_style`](../meltygui/core/styling/global_style.py) |
| `meltygui.gpu_frame_timer` | [`meltygui.core.diagnostics.gpu_frame_timer`](../meltygui/core/diagnostics/gpu_frame_timer.py) |
| `meltygui.hypr_left_drag` | [`meltygui.core.input.hypr_left_drag`](../meltygui/core/input/hypr_left_drag.py) |
| `meltygui.lifecycle` | [`meltygui.core.runtime.lifecycle`](../meltygui/core/runtime/lifecycle.py) |
| `meltygui.mcp_eval` | [`meltygui.core.automation.mcp_eval`](../meltygui/core/automation/mcp_eval.py) |
| `meltygui.mcp_hotswap` | [`meltygui.core.automation.mcp_hotswap`](../meltygui/core/automation/mcp_hotswap.py) |
| `meltygui.mcp_query` | [`meltygui.core.automation.mcp_query`](../meltygui/core/automation/mcp_query.py) |
| `meltygui.mcp_server` | [`meltygui.core.automation.mcp_server`](../meltygui/core/automation/mcp_server.py) |
| `meltygui.melty` | [`meltygui.core.melty`](../meltygui/core/melty.py) |
| `meltygui.mode_defaults` | [`meltygui.core.rendering.mode_defaults`](../meltygui/core/rendering/mode_defaults.py) |
| `meltygui.models.core_decoration` | [`meltygui.core.conversion.data_decoration`](../meltygui/core/conversion/data_decoration.py) |
| `meltygui.models.dynamic_obj` | [`meltygui.core.conversion.dynamic_obj`](../meltygui/core/conversion/dynamic_obj.py) |
| `meltygui.modes` | [`meltygui.core.rendering.modes`](../meltygui/core/rendering/modes.py) |
| `meltygui.mouse_cursor` | [`meltygui.core.input.mouse_cursor`](../meltygui/core/input/mouse_cursor.py) |
| `meltygui.notifications` | [`meltygui.core.diagnostics.notifications`](../meltygui/core/diagnostics/notifications.py) |
| `meltygui.os_frame` | [`meltygui.core.windowing.os_frame`](../meltygui/core/windowing/os_frame.py) |
| `meltygui.paths` | [`meltygui.core.runtime.paths`](../meltygui/core/runtime/paths.py) |
| `meltygui.perf_trace` | [`meltygui.core.diagnostics.perf_trace`](../meltygui/core/diagnostics/perf_trace.py) |
| `meltygui.rendering.core_render` | [`meltygui.core.core_render`](../meltygui/core/core_render.py) |
| `meltygui.rendering.core_render_helpers` | [`meltygui.core.rendering.core_render_helpers`](../meltygui/core/rendering/core_render_helpers.py) |
| `meltygui.rendering.decorators.core_decoration` | [`meltygui.core.rendering.core_decoration`](../meltygui/core/rendering/core_decoration.py) |
| `meltygui.rendering.decorators.invalidation_decoration` | [`meltygui.core.cache.invalidation_decoration`](../meltygui/core/cache/invalidation_decoration.py) |
| `meltygui.rendering.decorators.profile_decoration` | [`meltygui.core.diagnostics.profile_decoration`](../meltygui/core/diagnostics/profile_decoration.py) |
| `meltygui.rendering.decorators.window_decoration` | [`meltygui.core.rendering.window_decoration`](../meltygui/core/rendering/window_decoration.py) |
| `meltygui.rendering.render_funcs` | [`meltygui.core.rendering.render_funcs`](../meltygui/core/rendering/render_funcs.py) |
| `meltygui.rendering.shaped` | [`meltygui.core.rendering.shaped`](../meltygui/core/rendering/shaped.py) |
| `meltygui.resize_trace` | [`meltygui.core.diagnostics.resize_trace`](../meltygui/core/diagnostics/resize_trace.py) |
| `meltygui.scene_target` | [`meltygui.core.graphics.scene_target`](../meltygui/core/graphics/scene_target.py) |
| `meltygui.screenshot` | [`meltygui.core.graphics.screenshot`](../meltygui/core/graphics/screenshot.py) |
| `meltygui.session_status` | [`meltygui.core.diagnostics.session_status`](../meltygui/core/diagnostics/session_status.py) |
| `meltygui.settings` | [`meltygui.core.runtime.settings`](../meltygui/core/runtime/settings.py) |
| `meltygui.shader_func` | [`meltygui.core.graphics.shader_func`](../meltygui/core/graphics/shader_func.py) |
| `meltygui.state.dict_conversion` | [`meltygui.core.conversion.dict_conversion`](../meltygui/core/conversion/dict_conversion.py) |
| `meltygui.state.dict_conversion_util` | [`meltygui.core.conversion.dict_conversion_util`](../meltygui/core/conversion/dict_conversion_util.py) |
| `meltygui.state.graph_compare` | [`meltygui.core.conversion.graph_compare`](../meltygui/core/conversion/graph_compare.py) |
| `meltygui.state.load_save_v2` | [`meltygui.core.conversion.load_save_v2`](../meltygui/core/conversion/load_save_v2.py) |
| `meltygui.state.missing_saved_class` | [`meltygui.core.conversion.missing_saved_class`](../meltygui/core/conversion/missing_saved_class.py) |
| `meltygui.state.module_names` | [`meltygui.core.module_names`](../meltygui/core/module_names.py) |
| `meltygui.style` | [`meltygui.core.styling.style`](../meltygui/core/styling/style.py) |
| `meltygui.surface` | [`meltygui.core.windowing.surface`](../meltygui/core/windowing/surface.py) |
| `meltygui.text_texture` | [`meltygui.core.graphics.text_texture`](../meltygui/core/graphics/text_texture.py) |
| `meltygui.titlebar` | [`meltygui.core.windowing.titlebar`](../meltygui/core/windowing/titlebar.py) |
| `meltygui.titlebar_buttons` | [`meltygui.core.windowing.titlebar_buttons`](../meltygui/core/windowing/titlebar_buttons.py) |
| `meltygui.toggles` | [`meltygui.core.runtime.toggles`](../meltygui/core/runtime/toggles.py) |
| `meltygui.utils.glfw_utils` | [`meltygui.core.windowing.glfw_utils`](../meltygui/core/windowing/glfw_utils.py) |
| `meltygui.utils.singleton` | [`meltygui.core.runtime.singleton`](../meltygui/core/runtime/singleton.py) |
| `meltygui.utils.thread_safe_bool` | [`meltygui.core.runtime.thread_safe_bool`](../meltygui/core/runtime/thread_safe_bool.py) |
| `meltygui.utils.thread_signal` | [`meltygui.core.runtime.thread_signal`](../meltygui/core/runtime/thread_signal.py) |
| `meltygui.warm_start` | [`meltygui.core.styling.warm_start`](../meltygui/core/styling/warm_start.py) |
| `meltygui.wayland_color` | [`meltygui.core.graphics.wayland_color`](../meltygui/core/graphics/wayland_color.py) |
| `meltygui.wayland_move` | [`meltygui.core.windowing.wayland_move`](../meltygui/core/windowing/wayland_move.py) |
| `meltygui.window_api` | [`meltygui.core.windowing.window_api`](../meltygui/core/windowing/window_api.py) |
| `meltygui.window_constants` | [`meltygui.core.windowing.window_constants`](../meltygui/core/windowing/window_constants.py) |
| `meltygui.window_visibility` | [`meltygui.core.windowing.window_visibility`](../meltygui/core/windowing/window_visibility.py) |
| `meltygui.windows.backends.imgui_renderer` | [`meltygui.core.windowing.backends.imgui_renderer`](../meltygui/core/windowing/backends/imgui_renderer.py) |
| `meltygui.windows.backends.native_wayland` | [`meltygui.core.windowing.backends.native_wayland`](../meltygui/core/windowing/backends/native_wayland.py) |
| `meltygui.windows.backends.wayland_protocol` | [`meltygui.core.windowing.backends.wayland_protocol`](../meltygui/core/windowing/backends/wayland_protocol.py) |
