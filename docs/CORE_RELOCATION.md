# Shared core relocation

Completed 2026-09-16. Shared runtime modules now live in `meltygui/core/`.
The [core guide](../meltygui/core/README.md) groups the entry points by responsibility.

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
| `meltygui.app` | [`meltygui.core.app`](../meltygui/core/app.py) |
| `meltygui.app_session` | [`meltygui.core.app_session`](../meltygui/core/app_session.py) |
| `meltygui.background` | [`meltygui.core.background`](../meltygui/core/background.py) |
| `meltygui.code.bubbling` | [`meltygui.core.bubbling`](../meltygui/core/bubbling.py) |
| `meltygui.code.cache_tree` | [`meltygui.core.cache_tree`](../meltygui/core/cache_tree.py) |
| `meltygui.code.chain` | [`meltygui.core.chain`](../meltygui/core/chain.py) |
| `meltygui.code.converter_register` | [`meltygui.core.converter_register`](../meltygui/core/converter_register.py) |
| `meltygui.code.path_finder` | [`meltygui.core.path_finder`](../meltygui/core/path_finder.py) |
| `meltygui.code.render_host` | [`meltygui.core.render_host`](../meltygui/core/render_host.py) |
| `meltygui.collection_action` | [`meltygui.core.collection_action`](../meltygui/core/collection_action.py) |
| `meltygui.collision` | [`meltygui.core.collision`](../meltygui/core/collision.py) |
| `meltygui.debug.attribute_churn` | [`meltygui.core.attribute_churn`](../meltygui/core/attribute_churn.py) |
| `meltygui.debug.framebuffer_recorder` | [`meltygui.core.framebuffer_recorder`](../meltygui/core/framebuffer_recorder.py) |
| `meltygui.debug.invalidation_tracker` | [`meltygui.core.invalidation_tracker`](../meltygui/core/invalidation_tracker.py) |
| `meltygui.debug.mode` | [`meltygui.core.mode`](../meltygui/core/mode.py) |
| `meltygui.events.input_handler` | [`meltygui.core.input_handler`](../meltygui/core/input_handler.py) |
| `meltygui.events.pynput_backend` | [`meltygui.core.pynput_backend`](../meltygui/core/pynput_backend.py) |
| `meltygui.events.space_mouse` | [`meltygui.core.space_mouse`](../meltygui/core/space_mouse.py) |
| `meltygui.events.touchpad_backend` | [`meltygui.core.touchpad_backend`](../meltygui/core/touchpad_backend.py) |
| `meltygui.extensions` | [`meltygui.core.extensions`](../meltygui/core/extensions.py) |
| `meltygui.fonts` | [`meltygui.core.fonts`](../meltygui/core/fonts.py) |
| `meltygui.func_metadata` | [`meltygui.core.func_metadata`](../meltygui/core/func_metadata.py) |
| `meltygui.gc_manager` | [`meltygui.core.gc_manager`](../meltygui/core/gc_manager.py) |
| `meltygui.geometry_feed` | [`meltygui.core.geometry_feed`](../meltygui/core/geometry_feed.py) |
| `meltygui.gl_state` | [`meltygui.core.gl_state`](../meltygui/core/gl_state.py) |
| `meltygui.global_style` | [`meltygui.core.global_style`](../meltygui/core/global_style.py) |
| `meltygui.gpu_frame_timer` | [`meltygui.core.gpu_frame_timer`](../meltygui/core/gpu_frame_timer.py) |
| `meltygui.hypr_left_drag` | [`meltygui.core.hypr_left_drag`](../meltygui/core/hypr_left_drag.py) |
| `meltygui.lifecycle` | [`meltygui.core.lifecycle`](../meltygui/core/lifecycle.py) |
| `meltygui.mcp_eval` | [`meltygui.core.mcp_eval`](../meltygui/core/mcp_eval.py) |
| `meltygui.mcp_hotswap` | [`meltygui.core.mcp_hotswap`](../meltygui/core/mcp_hotswap.py) |
| `meltygui.mcp_query` | [`meltygui.core.mcp_query`](../meltygui/core/mcp_query.py) |
| `meltygui.mcp_server` | [`meltygui.core.mcp_server`](../meltygui/core/mcp_server.py) |
| `meltygui.melty` | [`meltygui.core.melty`](../meltygui/core/melty.py) |
| `meltygui.mode_defaults` | [`meltygui.core.mode_defaults`](../meltygui/core/mode_defaults.py) |
| `meltygui.models.core_decoration` | [`meltygui.core.data_decoration`](../meltygui/core/data_decoration.py) |
| `meltygui.models.dynamic_obj` | [`meltygui.core.dynamic_obj`](../meltygui/core/dynamic_obj.py) |
| `meltygui.modes` | [`meltygui.core.modes`](../meltygui/core/modes.py) |
| `meltygui.mouse_cursor` | [`meltygui.core.mouse_cursor`](../meltygui/core/mouse_cursor.py) |
| `meltygui.notifications` | [`meltygui.core.notifications`](../meltygui/core/notifications.py) |
| `meltygui.os_frame` | [`meltygui.core.os_frame`](../meltygui/core/os_frame.py) |
| `meltygui.paths` | [`meltygui.core.paths`](../meltygui/core/paths.py) |
| `meltygui.perf_trace` | [`meltygui.core.perf_trace`](../meltygui/core/perf_trace.py) |
| `meltygui.rendering.core_render` | [`meltygui.core.core_render`](../meltygui/core/core_render.py) |
| `meltygui.rendering.core_render_helpers` | [`meltygui.core.core_render_helpers`](../meltygui/core/core_render_helpers.py) |
| `meltygui.rendering.decorators.core_decoration` | [`meltygui.core.core_decoration`](../meltygui/core/core_decoration.py) |
| `meltygui.rendering.decorators.invalidation_decoration` | [`meltygui.core.invalidation_decoration`](../meltygui/core/invalidation_decoration.py) |
| `meltygui.rendering.decorators.profile_decoration` | [`meltygui.core.profile_decoration`](../meltygui/core/profile_decoration.py) |
| `meltygui.rendering.decorators.window_decoration` | [`meltygui.core.window_decoration`](../meltygui/core/window_decoration.py) |
| `meltygui.rendering.render_funcs` | [`meltygui.core.render_funcs`](../meltygui/core/render_funcs.py) |
| `meltygui.rendering.shaped` | [`meltygui.core.shaped`](../meltygui/core/shaped.py) |
| `meltygui.resize_trace` | [`meltygui.core.resize_trace`](../meltygui/core/resize_trace.py) |
| `meltygui.scene_target` | [`meltygui.core.scene_target`](../meltygui/core/scene_target.py) |
| `meltygui.screenshot` | [`meltygui.core.screenshot`](../meltygui/core/screenshot.py) |
| `meltygui.session_status` | [`meltygui.core.session_status`](../meltygui/core/session_status.py) |
| `meltygui.settings` | [`meltygui.core.settings`](../meltygui/core/settings.py) |
| `meltygui.shader_func` | [`meltygui.core.shader_func`](../meltygui/core/shader_func.py) |
| `meltygui.state.dict_conversion` | [`meltygui.core.dict_conversion`](../meltygui/core/dict_conversion.py) |
| `meltygui.state.dict_conversion_util` | [`meltygui.core.dict_conversion_util`](../meltygui/core/dict_conversion_util.py) |
| `meltygui.state.graph_compare` | [`meltygui.core.graph_compare`](../meltygui/core/graph_compare.py) |
| `meltygui.state.load_save_v2` | [`meltygui.core.load_save_v2`](../meltygui/core/load_save_v2.py) |
| `meltygui.state.missing_saved_class` | [`meltygui.core.missing_saved_class`](../meltygui/core/missing_saved_class.py) |
| `meltygui.state.module_names` | [`meltygui.core.module_names`](../meltygui/core/module_names.py) |
| `meltygui.style` | [`meltygui.core.style`](../meltygui/core/style.py) |
| `meltygui.surface` | [`meltygui.core.surface`](../meltygui/core/surface.py) |
| `meltygui.text_texture` | [`meltygui.core.text_texture`](../meltygui/core/text_texture.py) |
| `meltygui.titlebar` | [`meltygui.core.titlebar`](../meltygui/core/titlebar.py) |
| `meltygui.titlebar_buttons` | [`meltygui.core.titlebar_buttons`](../meltygui/core/titlebar_buttons.py) |
| `meltygui.toggles` | [`meltygui.core.toggles`](../meltygui/core/toggles.py) |
| `meltygui.utils.glfw_utils` | [`meltygui.core.glfw_utils`](../meltygui/core/glfw_utils.py) |
| `meltygui.utils.singleton` | [`meltygui.core.singleton`](../meltygui/core/singleton.py) |
| `meltygui.utils.thread_safe_bool` | [`meltygui.core.thread_safe_bool`](../meltygui/core/thread_safe_bool.py) |
| `meltygui.utils.thread_signal` | [`meltygui.core.thread_signal`](../meltygui/core/thread_signal.py) |
| `meltygui.warm_start` | [`meltygui.core.warm_start`](../meltygui/core/warm_start.py) |
| `meltygui.wayland_color` | [`meltygui.core.wayland_color`](../meltygui/core/wayland_color.py) |
| `meltygui.wayland_move` | [`meltygui.core.wayland_move`](../meltygui/core/wayland_move.py) |
| `meltygui.window_api` | [`meltygui.core.window_api`](../meltygui/core/window_api.py) |
| `meltygui.window_constants` | [`meltygui.core.window_constants`](../meltygui/core/window_constants.py) |
| `meltygui.window_visibility` | [`meltygui.core.window_visibility`](../meltygui/core/window_visibility.py) |
| `meltygui.windows.backends.imgui_renderer` | [`meltygui.core.backends.imgui_renderer`](../meltygui/core/backends/imgui_renderer.py) |
| `meltygui.windows.backends.native_wayland` | [`meltygui.core.backends.native_wayland`](../meltygui/core/backends/native_wayland.py) |
| `meltygui.windows.backends.wayland_protocol` | [`meltygui.core.backends.wayland_protocol`](../meltygui/core/backends/wayland_protocol.py) |
