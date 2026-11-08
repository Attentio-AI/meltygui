# Declaration reconciliation, line by line

November 7, 2026. Production cache without injection, current shared ImGui
context, 341-view full redraw. CPython 3.12, meltygui-imgui 2.0.4, RTX 4090,
Linux GLFW/OpenGL on a reserved agent desktop.

## What is measurably expensive

Eight independent fixed-scene cuts were run twice, 24 warmup and 200 measured
frames, randomized and frame-interleaved with an intact baseline. The baseline
means were 11.916 and 12.070 ms. These are uninstrumented CPU submission times.

| Source lines | Operation removed/replaced | Net saving ms/frame | Two repeats ms |
| --- | --- | ---: | ---: |
| 246-250 | Native state.configure, including construction of its defaults | **0.419** | 0.376 / 0.461 |
| 251-252 | Four NativeState attribute reads, replaced by mounted rect lookup | **0.411** | 0.399 / 0.422 |
| 263-270 | Unchanged-input/argument checks and associated invalidation decisions | **0.280** | 0.337 / 0.223 |
| 189, 216-218, 240-242, 253-254 | Position and dimension validation guards | **0.185** | 0.131 / 0.239 |
| 271-272 | Invocation copy and caller-key tuple refresh | **0.077** | 0.102 / 0.053 |
| 207, 243 | Caller dictionary copy/filter, replaced by mounted caller lookup | **0.062** | 0.026 / 0.099 |
| 197 | Identity lookup, replaced by mounted handle lookup | **0.002** | 0.072 / -0.068 |
| 256-257 | Argument dictionary construction, replaced by mounted argument lookup | **-0.060** | -0.033 / -0.086 |

The last two replacements have no demonstrated speed benefit. Small deltas
include the cost of looking up the frozen replacement. A negative result does
not prove constructing arguments is free: this particular replacement was
slower. Independent savings are not an additive decomposition.

`state.configure` is already native Rust, but each call still loops over six
default fields. For each it extracts a name, looks up the slot and explicit
caller value, converts a Python value, and compares against the stored value.
The unchanged-value check avoids writing/revision changes, not the work leading
up to that comparison. There are 341 configure calls and 2,046 default-field
iterations per frame. Its Rust implementation is in native/gui_prototype/src/lib.rs:194.

Immediately afterwards, `state.left`, `state.top`, `state.width` and
`state.height` each cross the Python/native attribute interface. NativeState's
`__getattr__` looks up the named slot and converts its Value back to Python.
That is 1,364 separate attribute reads per frame. The replacement still does
two mounted-rectangle lookups, so the 0.411 ms is a net saving, not their absolute
cost. See native/gui_prototype/src/lib.rs:259.

The unchanged-argument check compares dictionary key sets, constructs a generator,
iterates argument names, filters out geometry keys, and calls `_same` for the
remaining values. It still does this in the forced-redraw benchmark, even though
inputs and arguments are unchanged. This cut retains the pending-result lookup,
graph declaration and actual redraw; it removes only the previous-invocation
comparison branch.

The snapshot cut stops refreshing `arguments.copy()` and `tuple(caller)` after
mount. These snapshots are necessary for changing callers in normal use.
The caller-copy cut uses the mounted caller dictionary and avoids both its
initial copy and the later comprehension that removes None geometry values.

## Reading the rest of the wrapper

| Lines | Work and interpretation |
| --- | --- |
| 182-193 | Frame/closed-state guards, position-argument validation, identity-frame setup. The benchmark already owns an active host frame. |
| 194-206 | Extract key/name/window/scroll flags, resolve identity, look up binding. Creating native runtimes/renderers/states at 201-205 does not execute after mount. |
| 207-219 | Copy caller arguments, remove control keywords, choose dimensions and height bounds, validate and round bounds. |
| 220-242 | Find parent/allocation, query ImGui cursor, resolve automatic dimensions, validate width/height. Fixed explicit dimensions skip the width=None and auto-height branches. |
| 243-250 | Filter caller fields, construct six defaults and configure native state. |
| 251-255 | Read four native attributes, revalidate resolved geometry, convert/clamp dimensions. |
| 256-259 | Construct canonical arguments, then graph.declare synchronizes/looks up the existing graph node. The earlier broad declaration cut retained graph.declare, so its time is not all removable declaration overhead. |
| 260-270 | Fetch prior invocation/pending status and compare input/arguments; invalidate or dissociate only on change. |
| 271-283 | Refresh snapshots, check first-use records and alias registration. First-use record construction and alias writes are cold in this workload. |
| 284-288 | Bind node geometry and maintain the host's root sets. This is partly layout work, overlapping the earlier layout ablation. |
| 294-297 | Dirty/pending/frozen/scroll/visibility checks deciding whether to execute. Fixed-topology declarations bypass these decisions. |
| 298 | Actual execution, including nested child calls. Excluded from declaration subtotal. |
| 300-301 | Root flush/composition. Excluded from declaration subtotal. |
| 302-313 | Read measured rect and advance the parent's ImGui cursor/layout. Child image drawing is skipped for this scrollable scene. |
| 314-321 | Fetch/consume the return value, handle changed-result propagation and return it. The broad fixed declaration still fetches the graph result. |

The earlier roughly 3.05 ms broad declaration saving also removed geometry
binding, execution-eligibility decisions and parts of placement/control flow.
It was a replacement of the whole declaration wrapper, not the sum of dict-copy
costs. The individual cuts above do not fully reconcile that broad saving.

A useful next experiment would return the resolved rectangle from configure
in one native call while retaining precedence and change semantics, avoiding
four separate attribute reads. That has not been implemented or measured here.

## Line profiler: diagnostic only

Two separate 50-frame line-profile runs followed 24 warmup frames. They increased
CPU submission to 35.23 and 33.52 ms, versus approximately 12 ms uninstrumented.
The declaration subtotal excluding execute/flush was 9.86/9.41 ms under this
instrumentation; it is NOT the actual uninstrumented cost. Do not scale these
numbers to fit the broad removal result.

Below, every wrapper source line is preserved. Times average the two profiles
and include called functions. Multi-line expressions can generate multiple line
hits; hits are not function-call counts. A zero means no line event, which can
also mean a comment/blank/continuation line, not necessarily an unexecuted branch.
Execution recursively includes descendants, so its roughly 132 ms aggregate
exceeds the whole profiled frame: these inclusive recursive intervals overlap.
The high apparent cost of `return result` is not evidence that returning the
value is a major optimization target. Use the uninstrumented cuts above to
validate which regions actually matter.

## Full annotated source

Columns: original source line, events/frame, instrumented microseconds/frame,
then source. NESTED marks call sites excluded from the declaration subtotal.

```text
 182 |      0.0 |       0.00 |                @wraps(func)
 183 |      0.0 |       0.00 |                def wrapper(input_value=None, **kwargs):
 184 |    341.0 |      71.27 |                    if self.graphics and not self.stack:
 185 |      1.0 |       0.41 |                        import meltygui_imgui as imgui
 186 |      1.0 |       0.25 |                        if not imgui.is_frame_active():
 187 |      0.0 |       0.00 |                            with self.capture_frame():
 188 |      0.0 |       0.00 |                                return wrapper(input_value, **kwargs)
 189 |    341.0 |     236.19 |                    _check_position_arguments(kwargs)
 190 |    341.0 |      50.55 |                    if self.closed:
 191 |      0.0 |       0.00 |                        raise RuntimeError('retained runtime is closed')
 192 |    341.0 |      51.09 |                    if self.frame_roots is None and not self.identities.active:
 193 |      0.0 |       0.00 |                        self.identities.begin_frame()
 194 |    341.0 |      50.02 |                    keyed = 'key' in kwargs or 'key' in decoration
 195 |    341.0 |      65.06 |                    key = kwargs.pop('key', decoration.get('key'))
 196 |    341.0 |      53.31 |                    name = kwargs.get('name', decoration.get('name'))
 197 |    341.0 |     172.67 |                    identity_handle = self.identities.handle(renderer_id, key, name, keyed)
 198 |    341.0 |      52.50 |                    portal = kwargs.pop('melty_window', False)
 199 |    341.0 |      51.20 |                    scrollable = kwargs.pop('scroll', scroll)
 200 |    341.0 |      64.76 |                    if identity_handle not in self.bindings:
 201 |      0.0 |       0.00 |                        native = _new_native(graphics=self.graphics, identities=self.identities)
 202 |      0.0 |       0.00 |                        renderer = _bind_native(native, func, inject=inject, events=events,
 203 |      0.0 |       0.00 |                                                state_id=state_id, no_save=no_save, **decoration)
 204 |      0.0 |       0.00 |                        unique = self.identities.unique(identity_handle)
 205 |      0.0 |       0.00 |                        self.bindings[identity_handle] = native, renderer, func, native.state(unique), unique
 206 |    341.0 |     104.53 |                    native, renderer, _, state, unique = self.bindings[identity_handle]
 207 |    341.0 |      98.18 |                    caller = dict(kwargs)
 208 |    341.0 |      53.39 |                    if kwargs.pop('glfw_window', False):
 209 |      0.0 |       0.00 |                        raise NotImplementedError('This experiment implements internal windows; native child surfaces are not connected yet')
 210 |    341.0 |      60.09 |                    initial = kwargs.pop('initial', decoration.get('initial', {}))
 211 |    341.0 |      48.68 |                    extras = kwargs.pop('return_extras', False)
 212 |    341.0 |      51.83 |                    width_given, height_given = 'width' in kwargs, 'height' in kwargs
 213 |    341.0 |      61.11 |                    w, h = kwargs.pop('width', width), kwargs.pop('height', height)
 214 |    341.0 |      47.75 |                    minimum = kwargs.pop('min_height', min_height)
 215 |    341.0 |      48.24 |                    maximum = kwargs.pop('max_height', max_height)
 216 |    341.0 |      80.51 |                    if (not math.isfinite(minimum) or not math.isfinite(maximum)
 217 |    341.0 |      58.17 |                            or not 0 <= minimum <= maximum <= 8192):
 218 |      0.0 |       0.00 |                        raise ValueError('height bounds must satisfy 0 <= min_height <= max_height <= 8192')
 219 |    341.0 |      89.87 |                    minimum, maximum = math.ceil(minimum), math.ceil(maximum)
 220 |    341.0 |      68.26 |                    parent = self.stack[-1] if self.stack else 0
 221 |    341.0 |     277.82 |                    allocation = self.geometry.allocation(parent) if not portal else None
 222 |    341.0 |      53.36 |                    if self.graphics and (parent or self.auto_layout) and not portal:
 223 |    340.0 |      78.22 |                        import meltygui_imgui as imgui
 224 |    340.0 |     254.69 |                        x, y = imgui.get_cursor_screen_pos()
 225 |      0.0 |       0.00 |                    else:
 226 |      1.0 |       0.27 |                        x, y = (0, 0)
 227 |    341.0 |      43.98 |                    if allocation is not None:
 228 |      0.0 |       0.00 |                        ax, ay, w, h = allocation
 229 |      0.0 |       0.00 |                        px, py = self._position(parent)
 230 |      0.0 |       0.00 |                        x, y = ax-px, ay-py
 231 |    341.0 |      42.42 |                    if w is None:
 232 |      0.0 |       0.00 |                        if parent:
 233 |      0.0 |       0.00 |                            w = self.graph.constraints(parent)[0] - x
 234 |      0.0 |       0.00 |                        elif self.graphics:
 235 |      0.0 |       0.00 |                            w = imgui.get_content_region_available()[0]
 236 |      0.0 |       0.00 |                        else:
 237 |      0.0 |       0.00 |                            raise ValueError('width=None requires a parent or graphics host')
 238 |    341.0 |      43.40 |                    auto_height = h is None
 239 |    341.0 |      43.83 |                    if auto_height: h = state.get('height', 0.) or 0.
 240 |    341.0 |      73.04 |                    if (not math.isfinite(w) or not 0 < w <= 8192
 241 |    341.0 |      56.71 |                            or not math.isfinite(h) or h < 0):
 242 |      0.0 |       0.00 |                        raise ValueError('cached boundaries require positive width and nonnegative height')
 243 |    341.0 |     173.59 |                    caller = {k: v for k, v in caller.items() if not (k in ('left', 'top', 'width', 'height') and v is None)}
 244 |    341.0 |      42.31 |                    if allocation is not None:
 245 |      0.0 |       0.00 |                        caller.update(width=w, height=h, left=x, top=y)
 246 |   1023.0 |     718.68 |                    state.configure({'width': w, 'height': h,
 247 |    341.0 |      47.83 |                                     'left': float(x) if left is None else left,
 248 |    341.0 |      43.97 |                                     'top': float(y) if top is None else top,
 249 |    341.0 |      50.55 |                                     'scroll_offset': (0., 0.), 'content_offset': (0., 28. if portal else 0.)},
 250 |    341.0 |      42.91 |                                    caller, initial)
 251 |    341.0 |     352.91 |                    x, y = state.left, state.top
 252 |    341.0 |     239.96 |                    w, h = state.width, state.height
 253 |    341.0 |      89.72 |                    if not math.isfinite(w) or not 0 < w <= 8192 or not math.isfinite(h) or h < 0:
 254 |      0.0 |       0.00 |                        raise ValueError('cached boundaries require positive width and nonnegative height')
 255 |    341.0 |     133.62 |                    w, h = float(w), min(maximum, max(minimum, float(h)))
 256 |    682.0 |     176.71 |                    arguments = {**decoration, **kwargs, 'width': w, 'height': None if auto_height else h,
 257 |    341.0 |      41.90 |                                 'min_height': minimum, 'max_height': maximum}
 258 |    682.0 |     341.23 |                    node = self.graph.declare(parent, 0, identity_handle, input_value, arguments,
 259 |    341.0 |      61.86 |                                              (float(x), float(y), w, -1. if auto_height else h), portal, state, scrollable)
 260 |    341.0 |      73.53 |                    previous = self.invocations.get(node)
 261 |    341.0 |      80.06 |                    pending = self.graph.pending(node)
 262 |    341.0 |      44.70 |                    if previous is not None:
 263 |    341.0 |      60.97 |                        old_input, old_args = previous
 264 |    341.0 |     179.43 |                        changed_input = not pending and not _same(old_input, input_value)
 265 |    341.0 |     146.91 |                        if ((changed_input and not _same(self.graph.result(node, False)[1], input_value)) or old_args.keys() != arguments.keys()
 266 |    341.0 |    1078.80 |                                or any(not _same(old_args[k], v) for k, v in arguments.items()
 267 |      0.0 |       0.00 |                                       if k not in ('left','top','width','height','scroll_offset','content_offset'))):
 268 |      0.0 |       0.00 |                            self.graph.invalidate_node(node)
 269 |    341.0 |      48.39 |                        if not pending and old_input is not input_value:
 270 |      0.0 |       0.00 |                            self.graph.dissociate(node, _alias(old_input))
 271 |    341.0 |      81.81 |                    self.invocations[node] = input_value, arguments.copy()
 272 |    341.0 |     135.73 |                    self.caller_keys[node] = tuple(caller)
 273 |    341.0 |      63.25 |                    if node not in self.records:
 274 |      0.0 |       0.00 |                        self.uniques[node] = unique
 275 |      0.0 |       0.00 |                        self.records[node] = native, renderer, func
 276 |      0.0 |       0.00 |                        self.states[node] = state
 277 |      0.0 |       0.00 |                        self.associate(func, node)
 278 |      0.0 |       0.00 |                        self.associate(wrapper, node)
 279 |      0.0 |       0.00 |                        self.graph.associate(node, f'renderer:{renderer_id}', func)
 280 |      0.0 |       0.00 |                    # Identity aliases remain registered until replacement or retirement.
 281 |      0.0 |       0.00 |                    # Path-like values can supply changing text, so resolve those again.
 282 |    341.0 |      87.84 |                    if previous is None or previous[0] is not input_value or isinstance(input_value, (str, Path)):
 283 |      0.0 |       0.00 |                        self.associate(input_value, node)
 284 |    341.0 |     356.62 |                    self.geometry.bind(node, parent, portal=portal)
 285 |    341.0 |      47.29 |                    if parent == 0:
 286 |      1.0 |       0.22 |                        self.root_ids.add(node)
 287 |      1.0 |       0.15 |                        if self.frame_roots is not None:
 288 |      1.0 |       0.16 |                            self.frame_roots.add(node)
 289 |      0.0 |       0.00 |                    # An independently produced return is consumed before reexecuting.
 290 |      0.0 |       0.00 |                    # In particular, an old scalar caller argument cannot overwrite it.
 291 |      0.0 |       0.00 |                    # Cull whole scroll tiles. A non-scrolling parent's packet needs
 292 |      0.0 |       0.00 |                    # its children's textures even outside the ancestor viewport.
 293 |      0.0 |       0.00 |                    # Unmeasured auto-height tiles need one capture before culling.
 294 |    341.0 |      94.56 |                    if (self.graph.dirty(node) and not pending and node not in self.geometry.frozen
 295 |    341.0 |      73.10 |                            and (not parent or not self.graph.is_scrollable(parent)
 296 |    340.0 |      40.49 |                                 or (auto_height and node not in self.backgrounds)
 297 |    340.0 |     217.14 |                                 or self.graph.visible(node))):
 298 |    341.0 |  132149.59 | NESTED                 self._execute(node, independent=False)
 299 |      0.0 |       0.00 |                    # Root layout settles before the immediate caller places later items.
 300 |    341.0 |      49.26 |                    if not parent:
 301 |      1.0 |    1727.33 | NESTED                 self.flush()
 302 |    341.0 |     117.94 |                    _, _, w, h = self.graph.rect(node)
 303 |    341.0 |      51.44 |                    if self.graphics:
 304 |    341.0 |      46.82 |                        if parent and not portal:
 305 |    340.0 |      76.29 |                            import meltygui_imgui as imgui
 306 |    340.0 |      70.00 |                            imgui.set_cursor_screen_pos((x, y))
 307 |    340.0 |      76.26 |                            if not self.graph.is_scrollable(parent):
 308 |      0.0 |       0.00 |                                self._draw_image(imgui.get_window_draw_list(),node,x,y,w,h)
 309 |    340.0 |      76.27 |                            imgui.dummy(w, h)
 310 |      1.0 |       0.19 |                        elif self.auto_layout and not parent and not portal:
 311 |      0.0 |       0.00 |                            import meltygui_imgui as imgui
 312 |      0.0 |       0.00 |                            imgui.set_cursor_screen_pos((x, y))
 313 |      0.0 |       0.00 |                            imgui.dummy(w, h)
 314 |    341.0 |      95.22 |                    result = self.graph.result(node, consume=True)
 315 |    341.0 |      47.46 |                    if result[0] and parent:
 316 |      0.0 |       0.00 |                        # The caller may have drawn a label before assigning this return.
 317 |      0.0 |       0.00 |                        # One signalled refresh displays its post-edit state consistently.
 318 |      0.0 |       0.00 |                        self.graph.invalidate_node(parent)
 319 |    341.0 |      40.99 |                    if extras:
 320 |      0.0 |       0.00 |                        return *result, self.states[node]
 321 |    341.0 |     661.96 |                    return result
 322 |      0.0 |       0.00 |        
```

## Verification and reproduction

All cut and profile runs passed exact initial/final pixels, 341 executed bodies,
checksum 58,311, 341 packet uploads and one raster refresh per frame, stable
capture identities and GPU resource sizes. Twenty-eight focused tests also
passed. Scope is fixed topology, dimensions and idle input on desktop OpenGL;
not active interaction, CUDA, native Wayland presentation, macOS/iOS or release
wheels. No production behavior was changed.

The line profiler is optional developer tooling (`line-profiler==5.1.0` was
installed into the local development venv), not a runtime/package dependency.

```sh
.venv/bin/python tests/benchmark_gui_cache_ablation.py \
  --native-path /tmp/codex-cache-ablation-current-native/target/release/lib_gui_native.so \
  --mode declaration --frames 200 --warm 24 --seed 1027 --output /tmp/declaration/cuts-1027.json
.venv/bin/python tests/profile_gui_declaration_lines.py \
  --native-path /tmp/codex-cache-ablation-current-native/target/release/lib_gui_native.so \
  --frames 50 --warm 24 --output /tmp/declaration/lines-1.json
```

Use a reserved agent desktop and isolated XDG directories. Repeat the cut run
with seed 2027 and the profiler with a distinct output name. Raw JSON, `.lprof`
files and the exact runner are in `/tmp/codex-declaration-lines/`.
