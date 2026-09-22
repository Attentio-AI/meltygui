"""Inspection view functions and supporting definitions."""
from enum import Enum
from meltygui.core.melty import Melty
from meltygui.core.melty import SearchTerm
from meltygui.core.rendering.modes import Modes
from meltygui.core.core_render import render_func
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.rendering.window_decoration import window
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.state.inspection_state import ContextMenuState
from meltygui.state.inspection_state import _InfoRow
from meltygui.state.new_core_model import ContextMenuWindowState
from meltygui.state.new_core_model import TabState
from meltygui.core.runtime.toggles import Tint
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.runtime.toggles import hsv_to_rgb
from meltygui.core.runtime.toggles import rgb_to_hsv
import inspect
from pathlib import Path
import meltygui_imgui as imgui
import threading
import types
from meltygui.view.collection_view import fast_draw_collection
from meltygui.view.header_view import draw_header


@render_func(use_cache=False, show_bg=False, disable_scroll=True, shadow=False, selectable=False)
def draw_with_modes(input_value, modes, tab_state: TabState = None, search_text="", draw_state=None, unique=0):
    from meltygui.view.tab_view import draw_tab_bar
    from meltygui.core.rendering.render_dispatch import draw_any
    from meltygui.core.rendering.render_dispatch import input_tab_name
    from meltygui.core.rendering.render_dispatch import tab_names

    if not tab_state.selected_tabs:
        tab_state.selected_tabs = [modes[0]]
    imgui.dummy(0, 5)
    tint_value = 0.0
    tint_saturation = 0.688
    tab_changed, new_tabs = draw_tab_bar(input_value=tab_state.selected_tabs,
                                         tab_height=30, show_bg=False, bg_offset=1,
                                         name=f"tab_bar{unique}", wrap=True,
                                         collection=modes, as_toggles=False)
    if tab_changed:
        tab_state.selected_tabs = new_tabs
        # The compact Info tab's initial fit leaves no room for source rows.
        # Give Inputs a usable viewport when entering it from that small fit.
        if tab_names.index(input_tab_name) in new_tabs:
            minimum_height = min(700, int(imgui.get_io().display_size.y * 0.8))
            if (draw_state.height or 0) < minimum_height:
                draw_state.height = minimum_height
                draw_state.invalidate()

    imgui.dummy(0, 2)
    changed = False
    value = input_value
    for idx, mode in enumerate(tab_state.selected_tabs):
        mode_changed, value = draw_any(input_value, name=f"Mode: {mode} {unique}", mode=mode, selectable=False,
                                       show_name=False,
                                       with_header=None, show_header=False, disable_scroll=False,
                                       indent_size=0, show_bg=False, use_cache=True, shadow=False, column=idx)
        changed |= mode_changed

    return changed, value


@render_func(tint=(0.18, 0.32, 0.55), use_cache=False, show_name=False, show_bg=False)
def draw_view_func_selector(input_value, search_text="", draw_state=None, **kwargs):
    """Select a registered renderer; the caller owns applying the choice."""
    from meltygui.view.dropdown_view import draw_dropdown
    from meltygui.model.search_model import _fuzzy_key_match

    from meltygui.core.input.view_selection import view_func_name
    choices = {name: getattr(RenderFuncs, name)
               for name in sorted(Melty.render_funcs_by_name)
               if not search_text or _fuzzy_key_match(search_text.lower(), name.lower())}
    options = dict(kwargs)
    options.pop("view_func", None)
    options["use_cache"] = False
    options["display_label"] = view_func_name(input_value)
    changed, selected = draw_dropdown(input_value, collection=choices, **options)
    return changed, selected if changed else input_value


@render_func(use_cache=False, show_bg=False, shadow=False, with_header=None,
             show_name=False, selectable=False, is_tree=True, temp=True, searchable=False)
def draw_param_matrix(input_value, wrap=True, search_text="", draw_state=None, source_tints=None, unique=None,
                      source_locations=None, priority_params=(), source_order=(), view_draw_state=None,
                      source_dicts=None, writable_sources=(), source_kinds=None, **kwargs):
    """The inputs-tab parameter screen: ONE parameter at a time, EVERY source.
    A dropdown at the top switches between all the parameters identified for
    the view (its own signature params first, then the @render_func machinery
    kwargs); below it, one row per possible input source — param default
    (signature), caller, mode, class @defaults, function decoration — in
    `source_order`, shown whether or not the source currently sets the value.
    Sources that set the param show their editable value; the rest show a dim
    "not set", so the parameter's full input surface is mapped in one glance.

    Cells are parse FRAGMENTS (leaves pulled out of their codec's parse), so
    they can't naturally adopt the codec tint the way a whole codec-typed
    value does — this view is special: it looks the tint up per source (the
    tab maps each row to its codec) and applies it MANUALLY, alpha-boosted,
    so the data source is highly visible at a glance. Cell edits mutate the
    row in place and report changed, for apply_param_source_matrix write-back.

    `priority_params` (see signature_param_names) orders the view function's
    own signature params to the front of the dropdown list.

    `search_text` (the tab's Ctrl+F find bar) filters the dropdown's param
    list and auto-switches the screen to the best match: exact substring for
    short terms, the shared typo-tolerant matcher (_fuzzy_key_match) for
    longer ones.

    `source_dicts` (the live {source_name: parse dict} mapping) enables the
    +/× buttons: + stamps the param into a source that doesn't set it (seeded
    from the view's live resolved value), × pops it from one that does. Both
    are PLAIN dict mutations — the bubbling wrapper marks the owning host
    dirty and its normal chain_out/save path persists the change. Only
    `writable_sources` (real parse dicts, not the absent-source placeholders)
    get the buttons."""
    from meltygui.core.conversion.cache_tree import UNSET_VALUE
    from meltygui.core.styling.fonts import Font
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.control_view import button
    from meltygui.view.dropdown_view import draw_dropdown
    from meltygui.view.text_view import draw_text
    from meltygui.core.rendering.render_dispatch import _MATRIX_FRAMEWORK_PARAMS
    from meltygui.model.search_model import _fuzzy_key_match
    from meltygui.core.rendering.render_dispatch import draw_any

    changed = False
    tints = source_tints or {}
    locations = source_locations or {}
    plus_icon = "\uf067"  # FA plus -- explicit escape, see jump_to.py
    times_icon = ""  # FA times -- explicit escape, see jump_to.py
    folder_icon = "\uf07b"  # FA folder -- explicit escape, see jump_to.py

    # The dropdown's param list: the view function's own signature params
    # first (plus the MATRIX_DEFAULT_PRIORITY pins, which are listed even
    # when no source row exists for them - their screen just shows every
    # source as not set / +), then the @render_func machinery kwargs.
    # Injected/underscored params are never user inputs, so no screen.
    prio_set = set(priority_params or ())
    params = [p for p in (priority_params or ()) if not p.startswith('_')]
    params += [p for p in input_value
               if p not in prio_set and p not in _MATRIX_FRAMEWORK_PARAMS
               and not p.startswith('_')]

    search_q = str(search_text or "").strip().lower()
    if search_q:
        params = [p for p in params if _fuzzy_key_match(search_q, p.lower())]
    if not params:
        imgui.text_colored(f"no parameters match '{search_q}'", 1, 1, 1, 0.3)
        return False, input_value

    selected = getattr(draw_state, "_selected_param", None)
    if selected not in params:
        # Fresh screen (or stale selection) defaults to tint - the param this
        # menu is reached for most - falling back to the first param if a
        # search filter has excluded it.
        selected = "tint" if "tint" in params else params[0]
        draw_state._selected_param = selected

    draw_text(f"def {view_draw_state._view_func.__name__}",
              is_tree=False, editable=False, width=draw_state.content_width - 14,
              font=Font.JETBRAINS_MONO_30)
    imgui.dummy(0, 4)

    # STABLE identity: the dropdown's name must not change with the selection -
    # its popover is a latching child window, and a name change would orphan
    # the existing popover. Sync the label as an ordinary input.
    dd_res = draw_dropdown(
        selected, collection={p: p for p in params},
        name=f"param_pick##{unique}",
        width=min(280, max(120, draw_state.content_width - 24)),
        show_header=False, return_extras=True, display_label=selected)
    picked_changed, picked = dd_res[0], dd_res[1]
    if picked_changed and picked in params:
        draw_state._selected_param = picked
        selected = picked
        draw_state.invalidate()
    imgui.same_line()
    origin = "renderer" if selected == "view_func" else ("signature" if selected in prio_set else "core_render.py")
    imgui.text_colored(origin, 1, 1, 1, 0.25)
    imgui.dummy(0, 6)

    row = input_value.get(selected)
    row = row if isinstance(row, dict) else {}
    # Every registered source gets a row, set or not; sources present only in
    # the row (unmatched leftovers) append after the canonical order.
    order = [s for s in (source_order or ())]
    order += [s for s in row if s not in order]

    # Uniform label-button width across every row, so values align into a
    # column no matter how long each source's name is.
    btn_w = max((imgui.calc_text_size(f"{folder_icon} {sn}")[0] for sn in order),
                default=0.0) + 15

    def _stamp_value(param):
        """Seed for a + click: the view's LIVE resolved value for the param
        (its stamped kwargs, then the draw_state mirror), falling back to the
        first set source's cell — adding a source changes nothing visually
        until the user edits the new value. Deep-copied so the new source
        never aliases another source's parse node; a copied dict's foreign
        __cst__ would mis-anchor the save, so it's stripped."""
        import copy as _copy
        v = (getattr(view_draw_state, '_kwargs', None) or {}).get(param, UNSET_VALUE)
        if v is UNSET_VALUE:
            try:
                v = getattr(view_draw_state, param, None)
            except Exception:
                v = None
        if v is None:
            v = next((row[sn] for sn in order if sn in row), None)
        try:
            v = _copy.deepcopy(v)
        except Exception:
            pass
        if isinstance(v, dict):
            v.pop('__cst__', None)
            v.pop('__origin__', None)
        return v

    for sname in order:
        tint = tints.get(sname)
        is_set = sname in row
        src = (source_dicts or {}).get(sname)
        can_write = isinstance(src, dict) and sname in (writable_sources or ())

        # The source KIND caption (signature / caller / mode / class default /
        # decoration) sits on its own line above the row; the tinted button
        # below displays the concrete name (def draw_voxels / Mode.WINDOW /
        # @defaults(...)) and IS the jump button - clicking opens the source's
        # file in the IDE; the value sits on the same line with
        # show_name=False - so one element does both labeling and navigation.
        kind = (source_kinds or {}).get(sname)
        if selected == "view_func" and kind == "signature" and not is_set:
            can_write = False  # Adding a new parameter does not select a renderer.
        if kind:
            imgui.text_colored(kind, 1, 1, 1, 0.5)
        tint_kwargs = {"alpha": 0.0, "tint": tint} if tint else {}
        clicked = button(f"{folder_icon} {sname}", width=btn_w, height=22, shadow=False,
                         text_saturation=0.9, use_cache=True, text_align="left",
                         text_value=0.819,
                         name=f"jump_{sname}##{selected}_{unique}", show_button_bg=True,
                         **tint_kwargs)[0]
        loc = locations.get(sname)
        if clicked and loc:
            from meltygui.utils.jump_to_code import open_in_intellij
            threading.Thread(target=open_in_intellij, args=(str(loc[0]),),
                             kwargs={"line_number": loc[1]},
                             daemon=True).start()
        imgui.same_line()

        if is_set:
            # × removes the param from this source's dict. A plain dict
            # mutation: the bubbling invalidate notifies the owning object,
            # which goes dirty and persists via its own chain_out/save.
            if isinstance(src, dict) and not (selected == "view_func" and getattr(src, "direct", False)):
                if button(times_icon, width=30, height=22, shadow=True, use_cache=True,
                          text_value=1.0, name=f"{sname}_delete##{selected}_{unique}",
                          show_button_bg=True, **tint_kwargs)[0]:
                    if selected == "view_func" and view_draw_state is not None:
                        from meltygui.core.rendering.parameter_core import clear_anywhere
                        clear_anywhere(selected, view_draw_state, source=sname)
                    else:
                        src.pop(selected, None)
                        row.pop(sname, None)
                    draw_state.invalidate()
                    request_render()
                    imgui.dummy(0, 3)
                    continue
                imgui.same_line()

            # key routes the cell by ATTRIBUTE name (a tint value gets the
            # swatch/picker, not draw_tuple); the SOURCE stays in the
            # identity via name while the button above displays it.
            cell_view = draw_view_func_selector if selected == "view_func" else draw_any
            ch, nv = cell_view(row[sname], name=f"{sname}##{selected}_{unique}",
                              key=selected,
                              tint=tint,
                              show_name=False, wrap=True,
                              bg_offset=2, z_offset=0, disable_scroll=True, width=167)
            if ch:
                if selected == "view_func" and view_draw_state is not None:
                    from meltygui.core.rendering.parameter_core import set_anywhere
                    set_anywhere(selected, nv, view_draw_state, source=sname)
                else:
                    row[sname] = nv
                    changed = True
        elif can_write:
            # + stamps the param on this source - same plain-dict write the
            # cell editors use, so the same host-dirty/save machinery runs.
            if button(plus_icon, width=31, height=22, shadow=True, use_cache=True,
                      text_value=1.1, name=f"add_{sname}##{selected}_{unique}",
                      show_button_bg=True, **tint_kwargs)[0]:
                if selected == "view_func" and view_draw_state is not None:
                    from meltygui.core.rendering.parameter_core import set_anywhere
                    from meltygui.core.rendering.parameter_core import anywhere_value
                    set_anywhere(selected, anywhere_value(selected, view_draw_state),
                                 view_draw_state, source=sname)
                else:
                    src[selected] = _stamp_value(selected)
                    row[sname] = src.get(selected)
                draw_state.invalidate()
                request_render()
            imgui.same_line()
            imgui.text_colored("not set", 1, 1, 1, 0.5)
        else:
            imgui.text_colored("not set", 1, 1, 1, 0.5)

        imgui.dummy(0, 3)

    return changed, input_value


def draw_lens(lens, draw_state):
    """Render a single Lens against draw_state: resolve its root, then either
    focus the live leaf in place (in-place kinds) or run its generated
    parse→focus→save chain (code kinds). Returns (changed, _)."""
    from meltygui.core.rendering.render_dispatch import draw_any

    from meltygui.code.chain_converters import focus
    root = lens.root(draw_state)
    if root is None:
        imgui.text_colored(f"{lens.kind or lens.label}: n/a here", 0.5, 0.5, 0.5)
        return False, root
    if lens.chain is None:
        return focus(root, path=lens.path, default=lens.default, kind=lens.kind, name=lens.label + lens.name)
    return draw_any(root, chain=lens.chain(root), name=lens.label)


@window
@render_func()
def context_menu_settings(input_value, draw_state):
    from meltygui.code.new_converters import code_file_io

    code_file_io(draw_context_menu, mode=Modes.NEW_CODE)


@render_func(use_cache=False, show_bg=False, show_header=False, show_name=False,
             selectable=False, is_default_for=_InfoRow)
def draw_info_param(input_value, **kwargs):
    """One info-tab row: a source dropdown for LOOKING at the different input
    sources, plus the value stored AT the selected source — editable when
    that source is writable, an inline + when it doesn't set the param there
    yet, read-only text otherwise. Shared per-render state (source registry,
    active map, dropdown option cache) arrives via _InfoRow.ctx; selection
    lives on the TAB's draw_state (misc) — pure view state."""
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.control_view import draw_button
    from meltygui.view.control_view import text
    from meltygui.view.dropdown_view import draw_dropdown
    from meltygui.core.rendering.parameter_core import set_anywhere
    from meltygui.core.rendering.render_dispatch import draw_any

    from meltygui.core.rendering.parameter_core import _ABOVE_DRAW_STATE
    row = input_value
    ctx = row.ctx
    if ctx is None or row.group is None:
        # Cold hit before the tab stamped this row's context (shouldn't
        # happen - rows only render from inside the tab body).
        return False, input_value
    param = row.param
    target = ctx.target
    # Key match/current flags come from draw_collection (it matched our name);
    # forwarded to the value widget so the header carries the match, same as
    # the non-collection rows did.
    _search_kw = {"search_match": kwargs.get("search_match", False),
                  "search_current": kwargs.get("search_current", False)}

    if ctx.parses_ready:
        # The ACTIVE source: the highest-priority setter (precomputed in
        # ctx.active_map, one registry pass) - unless a diverged auto_param
        # outranks it at runtime (the ds replaces every setter not in
        # _ABOVE_DRAW_STATE; the ds row only registers whitelisted
        # attrs, so detect the divergence directly. A bare `param in
        # target.__dict__` would be wrong: DrawState.__init__ sets its
        # own param on every param).
        setting = ctx.active_map.get(param)
        ds_has = param in (getattr(target, "auto_params", None) or {})
        if ds_has and (setting is None
                       or ctx.prio[setting][0] not in _ABOVE_DRAW_STATE):
            active = "draw_state"
        else:
            active = setting
        ctx.active_cache[param] = active
        known = True
    else:
        # ACTIVE-SOURCE CACHE DISABLED (perf A/B): never serve cached
        # picks - loading rows draw no dropdown until sources are live.
        # Re-enable by restoring: known = param in ctx.active_cache;
        # active = ctx.active_cache.get(param)
        known = False
        active = None

    if known:
        # Dropdown of ALL sources in SourcePriority order, active one
        # tinted + row-ted - memoized per current active source
        # (ctx.options_for), not rebuilt per param.
        options, _row_tints = ctx.options_for(active)

        # Selection is per-tab view state; default = the active source.
        sel_key = f"src_sel::{param}"
        sel = ctx.tab_ds.misc.get(sel_key)
        if sel not in options:
            if active is not None:
                sel = active
            elif ctx.parses_ready:
                sel = ctx.default_source_once()
            else:
                sel = "draw_state"

        # Subtle trigger: no button bg/shadow, short, narrow - it's a
        # provenance label with a dropdown, not a primary action.
        # Per-row trash INSIDE the dropdown row clears this param at THAT
        # source without closing the list, so several sources can be
        # cleared in one operation. Only rows that actually SET the param
        # get one (ctx.setters_map, plus the ds when an auto_param diverged);
        # codec rows are excluded - clear_anywhere can't reverse the
        # codec's per-file/render_kwargs fanout yet.
        def _clear_at(src, _p=param):
            from meltygui.core.rendering.parameter_core import clear_anywhere
            if clear_anywhere(_p, target, str(src)) is not None:
                target.invalidate()
                ctx.tab_ds.invalidate()

        _row_actions = {s: _clear_at for s in ctx.setters_map.get(param, ())
                        if ctx.srcs["kinds"].get(s) != "codec"}
        if param in (getattr(target, "auto_params", None) or {}):
            _row_actions["draw_state"] = _clear_at
        pick_changed, new_pick = draw_dropdown(
            options.get(sel, sel), collection=options, width=181, z_offset=0,
            shadow=False, show_button_bg=False, trigger_height=22, show_bg=False,
            text_pad=3, row_tints=_row_tints, row_actions=_row_actions,
            text_toward_bg=ctx.text_toward_bg,
            name=f"src_{param}_dd", show_header=False)
        if pick_changed and new_pick:
            sel = str(new_pick)
            ctx.tab_ds.misc[sel_key] = sel
    else:
        sel = None
        imgui.dummy(181, 22)  # hold the dropdown slot so no reflow when it appears

    imgui.same_line()

    any_changed = False
    if not ctx.parses_ready:
        # Sources still loading: stored-at-source reads would hit
        # placeholders - bind the widget to the RESOLVED value and route
        # edits through the automatic pick until the sources are real.
        item_return = draw_any(row.group.get(param), name=param,
                               show_bg=False, show_header=True, **_search_kw)
        item_changed, out_val = item_return[0], item_return[1]
        if item_changed:
            row.group[param] = out_val
            any_changed = True
    else:
        # The value AT the selected source (not the resolved value) - that's
        # what looking at a source means. draw_state reads the direct attr.
        sdict = ctx.srcs["sources"].get(sel)
        stored = sdict.get(param) if isinstance(sdict, dict) else None
        if sel == "draw_state" and stored is None:
            stored = (getattr(target, "auto_params", None) or {}).get(
                param, target.__dict__.get(param))
        sel_writable = sel in ctx.writable or sel == "draw_state"

        # In-flight display cache (_sa_pending - the same one anywhere_value
        # serves): a slow-source write, and EVERY write while a drag is underway
        # (deferred), hasn't reached the source dict yet - a raw stored read
        # snaps the slider back to the stale value next frame ("stuck").
        # Serve the pending UI value while the trip is in flight, but only
        # when this row's selected source is the one the write targeted.
        # The tab's proxy refresh runs anywhere_value per param, which
        # retires it once the stored value moves off its at-set baseline.
        _pending = getattr(target, "_sa_pending", None)
        if _pending and param in _pending:
            _lastsrc = getattr(target, "_sa_last_source", None) or {}
            if _lastsrc.get(param, sel) == sel:
                stored = _pending[param][0]

        if stored is None:
            if sel_writable:
                # Selected source doesn't set the param yet: + stamps a value
                # into it (creating the entry); next frame the widget takes
                # over. draw_button is the most-compatible in-line button
                # (draw_float's shape) - same header chrome as widget rows.
                clicked, _ = draw_button("+", name=f"+##add_{param}",
                                         label="+", display_name=param,
                                         show_name=True, show_bg=False,
                                         wrap=True, min_width=24, **_search_kw)
                if clicked:
                    # Resolved value when there is one; a None (header params
                    # nothing sets) stamps the DECLARED signature default -
                    # stamping None would create an entry that still reads
                    # as None ("the + does nothing" feel).
                    stamp = row.group.get(param)
                    if stamp is None:
                        from meltygui.core.rendering.parameter_core import signature_default_for
                        stamp = signature_default_for(param, target)
                    set_anywhere(param, stamp, target, allow_any=True,
                                 ds_fallback=True, source=sel)
                    any_changed = True
            else:
                text(f"{param}: not set here", name=f"ro_{param}",
                     editable=False, **_search_kw)
        elif sel_writable:
            item_return = draw_any(stored, name=param,
                                   show_bg=False, show_header=True, **_search_kw)
            item_changed, out_val = item_return[0], item_return[1]
            if item_changed:
                set_anywhere(param, out_val, target, allow_any=True,
                             ds_fallback=True, source=sel)
                any_changed = True
        else:
            text(f"{param}: {stored}", name=f"ro_{param}", editable=False,
                 **_search_kw)

    if any_changed:
        target.invalidate()
        # The rows themselves live under the (cached) tab: re-render so the
        # active tint and stored values reflect the write this frame
        # (parse-dict writes are synchronous; the hosts' notify triggers the
        # later save/hotswap).
        ctx.tab_ds.invalidate()
        request_render()
    return False, input_value


@render_func(use_cache=True, show_bg=False, show_header=False, disable_scroll=False,
             searchable=True, show_name=False, selectable=False)
@window
def draw_info_tab(input_value, search_text='', draw_state=None, unique=None, **kwargs):
    """One row per view param: a source dropdown for LOOKING at the different
    input sources, plus the value stored at the selected source — editable
    when that source is writable, an inline + when it doesn't set the param
    there yet. The dropdown defaults to the source actively driving the
    param (yellow row/trigger); switching it never deletes or moves
    anything, it just changes which source you're viewing/editing.
    Selection lives on THIS tab's draw_state (misc) — pure view state.

    The source machinery is debug-gated: by default the tab renders just the
    grouped param values (cheap — no registry parses) and the Debug button
    switches to the dropdown rows. The header group starts collapsed in
    both modes."""
    from meltygui.state.inspection_state import _InfoRow
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.collection_view import draw_collection
    from meltygui.view.control_view import draw_button
    from meltygui.core.rendering.parameter_core import _source_priority
    from meltygui.core.rendering.parameter_core import _sources_for
    from meltygui.core.rendering.parameter_core import default_write_source
    from meltygui.core.rendering.render_dispatch import _INFO_GROUP_OVERRIDES
    from meltygui.core.rendering.render_dispatch import _SourceItem

    if input_value is None:
        return False, None
    from meltygui.core.rendering.parameter_core import _unset_value
    target = input_value
    # locate_all_params: the view's own params PLUS the header's
    # (with_header function inputs - icon, show_name, name_color, etc),
    # deduped, view params first. Same read/write semantics.
    proxy = target.locate_all_params

    # ── Search - the rows render through draw_collection, so it owns key
    # matching: count claims (its _search_matcher over the row keys = the
    # param names), the match/current glow kwargs, and scroll-to-match.
    # Just resolve what to FORWARD: a term/SearchTerm passed in search_text
    # (the menu's search box / an ancestor session), else this tab's OWN
    # find-bar session - the search_text kwarg is NEVER injected from an
    # ancestor session, so a self-hosted Ctrl+F never arrives through it.
    draw_state._search_matcher = None  # pre-collection matcher (stale after hotswap)
    _term = search_text or (draw_state.search_text if draw_state.search_active else "")
    if isinstance(_term, SearchTerm):
        child_search = _term
    elif _term and draw_state.search_active and draw_state._search_session is not None:
        # The session (a SearchTerm) carries term + current/scroll_to state.
        child_search = draw_state._search_session
    else:
        child_search = ""

    # ── Debug gate - the source registry is EXPENSIVE (_sources_for spawns
    # render-func/class/call-site parses, and the codeCM guard below
    # pulses re-renders until they land). Skip ALL of it until asked: by
    # default the tab is just the grouped values (edits route through
    # ParamProxy.__setitem__ → set_anywhere on the driving source); the
    # Debug button fills in the per-param source dropdowns. The pick is
    # per-tab view state, same slot as src_sel.
    debug = bool(draw_state.misc.get("info_sources"))
    clicked, _ = draw_button("Debug", name="dbg_btn", show_name=False,
                             label="Hide sources" if debug else "Debug",
                             min_width=110)
    if clicked:
        debug = not debug
        draw_state.misc["info_sources"] = debug
        draw_state.invalidate()
        request_render()

    if not debug:
        # Same grouped shape as the debug mirror, but with the live
        # ParamProxy groups themselves. A fresh outer dict, so the proxy
        # never carries the __overrides__ entry (GroupedParamProxy.refresh
        # would break on a non-proxy value).
        outer = {}
        for _gkey in ("params", "header"):
            _group = proxy.get(_gkey)
            if _group:
                outer[_gkey] = _group
        outer["__overrides__"] = _INFO_GROUP_OVERRIDES
        fast_draw_collection(outer, name="rows", use_cache=False, show_bg=False,
                        show_header=False, shadow=False, selectable=False,
                        item_spacing_y=2, child_kwargs={"show_system": True,
                                                        "initial":{"expanded":False}
                                                        },
                        search_text=child_search)
        return False, input_value

    srcs = _sources_for(target)
    writable = set(srcs["writable"])

    # ONE pass over the registry per render - the active source is simply
    # the highest-priority writable source with a SET value. The old shape
    # re-walked every source dict per PARAM (_setting_source per row, plus
    # the default_write_source cue re-counting key overlaps), which is
    # O(params × sources × keys) through lazy bubbling parse wrappers.
    _prio = {s: _source_priority(srcs["kinds"].get(s)) for s in srcs["sources"]}
    _ordered_all = sorted(srcs["sources"], key=_prio.get)
    active_map = {}  # param -> highest-priority source
    setters_map = {}  # param -> [every source setting it, priority order]
    for _s in _ordered_all:
        if _s not in writable:
            continue
        _sd = srcs["sources"][_s]
        if not isinstance(_sd, dict):
            continue
        for _k, _v in _sd.items():
            if _unset_value(_v):
                continue
            if _k not in active_map:
                active_map[_k] = _s
            setters_map.setdefault(_k, []).append(_s)

    # The + default for params the source sets (the other-params cue) is
    # param-independent to first order - compute at most once per render,
    # not per row (its overlap counting walks every source dict).
    _cue = []

    def _default_source_once():
        if not _cue:
            _cue.append(default_write_source("", target, srcs=srcs))
        return _cue[0]

    # Dropdown styling from Toggles.ContextMenu (live-editable): the active
    # source's yellow, and how far source-row text pulls toward the menu bg.
    _active_tint = tuple(Toggles.ContextMenu.active_source_tint)
    _text_toward_bg = float(Toggles.ContextMenu.source_text_toward_bg)

    # Dropdown options/row-tints are IDENTICAL for every param sharing the
    # same active source - and a view usually has only one or two distinct
    # actives. Build once per distinct active, not per param (the per-row
    # dict of _SourceItems was N_params × N_sources object churn per render).
    _row_cache = {}

    def _options_for(active):
        hit = _row_cache.get(active)
        if hit is None:
            options = {s: _SourceItem(s, _active_tint if s == active else None)
                       for s in _ordered_all}
            options.setdefault(
                "draw_state",
                _SourceItem("draw_state",
                            _active_tint if active == "draw_state" else None))
            row_tints = ({str(active): _active_tint}
                         if active is not None else None)
            hit = (options, row_tints)
            _row_cache[active] = hit
        return hit

    # _sources_for's keep-alive registers the TARGET as the code hosts'
    # consumer; this tab is cached separately, so register it too - a parse
    # landing (an editor's save/hotswap + an external change) then invalidates
    # these rows and the active-source cache tracks the live registry.
    _cm = getattr(target, "_sa_cm_state", None)
    if _cm is not None:
        for _h in (_cm.render_func_dict, _cm.class_dict, _cm.mode_dict,
                   *[dh for (_sh, dh) in (_cm.call_site_hosts or [])]):
            if _h is not None:
                _h.notify_on_change(draw_state)

    # Cold-session guard: while the render-func parse hasn't materialized
    # (signature row still the unwritable placeholder), keep re-rendering.
    # A cached tab stops pulsing the registry and its consumer stamp goes stale,
    # and the parse-landing notify can miss it - the tab then shows "not
    # set here" placeholders forever on a fresh session.
    _sig = next((s for s, k in srcs["kinds"].items() if k == "signature"), None)
    parses_ready = _sig in writable
    if not parses_ready:
        draw_state.invalidate()
        request_render()

    # Active-source cache on the TARGET ds: parses take a time on a cold
    # open, and provenance shouldn't hide while they load. Ready registry →
    # recompute and refresh the cache; loading → serve the cached pick, and
    # a param with NO cached pick hides the dropdown until the registry is
    # known (the value widget still draws, bound to the resolved value).
    _active_cache = getattr(target, "_sa_active_src", None)
    if _active_cache is None:
        _active_cache = {}
        target._sa_active_src = _active_cache

    # Everything a row needs to draw its dropdown + stored-value widget,
    # computed ONCE per tab render and shared by every row via _InfoRow.ctx.
    ctx = types.SimpleNamespace(
        target=target, tab_ds=draw_state, srcs=srcs, writable=writable,
        prio=_prio, active_map=active_map, setters_map=setters_map,
        options_for=_options_for, default_source_once=_default_source_once,
        text_toward_bg=_text_toward_bg, parses_ready=parses_ready,
        active_cache=_active_cache)

    # Mirror the grouped proxy ({'params': {...}, 'header': {...}}) with
    # stable _InfoRow leaves: one row object per param, kept across frames
    # so the child draw_states maintain their identity, rebuilt in proxy order
    # (in place; group dicts' identity is stable too) and pruned as params
    # vanish.
    rows_store = getattr(draw_state, "_info_rows", None)
    if rows_store is None:
        rows_store = {}
        draw_state._info_rows = rows_store
    for _gkey in ("params", "header"):
        _group = proxy.get(_gkey)
        if not _group:
            rows_store.pop(_gkey, None)
            continue
        rows = rows_store.setdefault(_gkey, {})
        _params = list(_group.keys())
        if list(rows.keys()) != _params:
            _prev = dict(rows)
            rows.clear()
            for _p in _params:
                rows[_p] = _prev.get(_p) or _InfoRow(_p)
        for _row in rows.values():
            _row.group = _group
            _row.ctx = ctx

    # ONE draw_collection over the whole grouped dict - leaf groups render as
    # nested tabs (the point of the grouped shape), leaf rows type-route to
    # draw_info_param and own their writes (always returning changed=False,
    # so nothing gets written back into the mirror). use_cache=False: the
    # tab's own cache is the only gate, as before. Named apart from the
    # edit-mode "rows" so each mode owns its own draw_state subtree.
    rows_store["__overrides__"] = _INFO_GROUP_OVERRIDES  # header starts collapsed
    draw_collection(rows_store, name="src_rows", use_cache=False,
                    show_bg=False, show_header=False, shadow=False,
                    selectable=False, item_spacing_y=2,
                    child_kwargs={"show_system": True},
                    search_text=child_search)
    return False, input_value


@render_func(use_cache=True, show_bg=False, show_header=False, show_name=False, selectable=False)
def draw_config_tab(input_value, **kwargs):
    """List the inspected view function's configurable parameters and their
    current values (kwarg override, else signature default)."""
    from meltygui.view.control_view import text
    from meltygui.view.text_view import draw_text
    from meltygui.core.rendering.render_dispatch import draw_any

    view_func = input_value._view_func
    if view_func is None:
        text("No view function")
        return False, input_value
    imgui.dummy(0, 0)

    # Unwrap the @render_func wrapper to read the original signature.
    raw_func = getattr(view_func, '__wrapped__', view_func)
    sig = inspect.signature(raw_func)
    ds_kwargs = input_value._kwargs or {}
    # Framework-injected params the user doesn't configure.
    skip_params = {"input_value", "draw_state", "args", "o_kwargs",
                   "kwargs", "meta", "viewstate", "self"}

    for param_name, param in sig.parameters.items():
        if param_name in skip_params:
            if param_name in ds_kwargs:
                param_value = ds_kwargs[param_name]
                draw_text(f"{param.__class__.__name__}", name=param_name,
                          editable=False, tint=(0.8, 0.8, 0.2))
            continue

        if param.kind in (inspect.Parameter.VAR_POSITIONAL,
                          inspect.Parameter.VAR_KEYWORD):
            continue

        # Current value: kwarg override, else the signature default.
        if param_name in ds_kwargs:
            param_value = ds_kwargs[param_name]
        elif param.default is not inspect.Parameter.empty:
            param_value = object()
        else:
            param_value = None

        if isinstance(param_value, (int, float, str, bool, Enum)):
            draw_text(f"{param_value}", name=param_name, editable=False)
        else:
            draw_any(param_value, name=param_name,
                     show_name=True, show_header=True,
                     show_add_delete=False, draw=True)
    return False, input_value


@render_func(use_cache=True, show_bg=False, live=False, mode=Modes.WINDOW, show_header=False, show_name=False,
             selectable=False)
def draw_live_tab(input_value, **kwargs):
    """List the inspected view function's configurable parameters and their
    current values (kwarg override, else signature default)."""
    from meltygui.view.control_view import text

    imgui.text("Re-renders view frequently, bad for performance but good for debugging")

    params_to_view = ["unique", ("abs_left", "left_offset"), ("abs_top", "top_offset"), "scroll_offset",
                      ("abs_top_true", "top_offset_true"), ("width", "height"), ("content_width", "content_height"),
                      "layer", "z_offset"]
    for to_view in params_to_view:
        if isinstance(to_view, str):
            value = getattr(input_value, to_view, 'N/A')
            text(f"{to_view}: {value}", name=to_view, editable=False)
        else:
            values = [getattr(input_value, attr, 'N/A') for attr in to_view]
            text(f"{', '.join(to_view)}: {', '.join(str(v) for v in values)}", name=", ".join(to_view), editable=False)

    if isinstance(input_value._raw_input_value, (dict, list, tuple)):
        text(f"Length: {len(input_value._raw_input_value)}", name="raw_input_length", editable=False)

    # imgui.text_colored(f"Unique {input_value.unique}", *(0.5, 0.01, 0.6))
    # imgui.dummy(0,2)
    #
    # imgui.text_colored(f"Top, Left {input_value.abs_top}, {input_value.abs_left}", *(0.5, 0.5, 0.0))
    # imgui.dummy(0, 2)
    #
    # #Width, height
    # imgui.text_colored(f"Width, Height {input_value.width}, {input_value.height}", *(0.5, 0.01, 0.6))
    # imgui.dummy(0, 2)
    #
    # imgui.text_colored(f"Unique {input_value.unique}", *(0.5, 0.01, 0.6))
    # imgui.dummy(0, 2)

    return False, input_value


def _function_source_path(view_func):
    """The file `view_func` is defined in (unwrapped: a render_func wrapper
    reports its own module otherwise), or None for a builtin / interactive
    definition."""
    try:
        return inspect.getsourcefile(inspect.unwrap(view_func))
    except (TypeError, OSError):
        return None


def draw_func_tab(input_value, name=None, disable_scroll=True, width=None,
                  height=None, select_line=None, select_seq=0, draw_state=None,
                  crumb_height=24.0, **kwargs):
    """Editable source of the inspected view function; hotswaps on save.
    Under the file browser's crumb strip (`draw_breadcrumbs`, `crumb_height`
    tall, drawn into the host `draw_state`'s tile when one is given): the
    function's file, every segment a dropdown over its directory, the
    crumbs in their painted file-meta tints; a picked file opens in the
    code editor.
    Routes through Mode.FILE_TREE — the same cache-backed code_file_io path a
    folder-files leaf uses — so all editors share one code path.

    A plain function, not a @render_func: the wrapper added a full pass +
    tile layer around a single dispatch (the standing `draw_func_tab` line
    in the frame profiles) and the editor child does its own caching. The
    caller's `name` rides into the child as its `key` so two func tabs
    showing the same function keep distinct draw_states — the wrapper's
    per-tab name used to provide that separation.

    code_file_io is called DIRECTLY with Mode.FILE_TREE's override kwargs
    (chain idiom) instead of via draw_any(mode=FILE_TREE): the mode pins
    disable_scroll=True and mode kwargs win over call kwargs, so the
    caller's disable_scroll=False could never reach the editor — the old
    wrapper was the scroll container, and removing it killed scrolling
    until this bypass."""
    from meltygui.view.control_view import draw_str

    view_func = input_value._view_func
    if view_func is not None:
        from meltygui.code.new_converters import code_file_io
        from meltygui.view.code_view import draw_text_from_code_cache
        view_func_name = view_func.__name__ if hasattr(view_func, '__name__') else str(view_func)
        if draw_state is not None:
            from meltygui.view.file_view import draw_breadcrumbs
            source_path = _function_source_path(view_func)
            if source_path is not None:
                picked, target = draw_breadcrumbs(
                    source_path, draw_state, width=width, crumb_height=crumb_height,
                    name=f"func crumbs {name}")
                if picked and not Path(target).is_dir():
                    from meltygui.core.runtime.extensions import open_source as open_in_editor
                    open_in_editor(target)
                if height is not None:
                    height = max(60.0, height - Melty.px(crumb_height))
        _kw = {}
        if width is not None:
            _kw["width"] = width
        if height is not None:
            _kw["height"] = height
        if name is not None:
            _kw["key"] = name
        if select_line is not None:
            # File-absolute line to auto-select (scope-up nav) — rides through
            # code_file_io's child_kwargs into draw_text_from_code_cache, which
            # consumes it against the span buffer. select_seq (the key-press
            # generation) keys the one-shot so each press re-selects.
            _kw["child_kwargs"] = {"select_line": select_line,
                                   "select_seq": select_seq}
        code_file_io(view_func, auto_load_edits=True,
                     view_func=draw_text_from_code_cache,
                     disable_scroll=disable_scroll, show_name=False,
                     is_tree=False, name=view_func_name, **_kw)
    else:
        draw_str("No view function specified", name="View Function", editable=False)
    return False, input_value


@render_func(use_cache=True, show_bg=False, show_header=False, show_name=False, selectable=False)
def draw_eval_tab(input_value, draw_state, unique=None, enter_key_down=None,
                  menu_draw_state=None, **kwargs):
    """Arbitrary-code REPL scoped to the inspected view function. This tab is just
    an editor + trigger: it stashes the snippet and a pending flag on the TARGET
    widget's draw_state. The eval itself runs back in that widget's render wrapper
    (core_render), right before it calls the view func -- so the snippet sees the
    view function's real call-time locals. We read the result back off the same
    draw_state."""
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.control_view import button
    from meltygui.view.text_view import draw_text
    from meltygui.core.rendering.render_dispatch import eval_input_scope
    import meltygui.core.windowing.window_api as glfw

    target = input_value  # (possibly walked-up) target's draw_state
    eval_view_func = target._view_func

    # Autocomplete scope. The exact call-time locals are only known once an eval
    # actually fires (core_scoped_eval pulls them then). To give type-aware
    # suggestions BEFORE the first eval, pre-record an initial scope from what
    # the target draw_state already exposes -- input_value/value, draw_state/ds,
    # and every explicit kwarg. core_scoped_eval refines this to exact on first eval.
    from meltygui.core.rendering.func_metadata import FuncsMetadata
    from meltygui.core.rendering.func_metadata import eval_completion_source
    _scope = eval_input_scope(target)
    if isinstance(target._kwargs, dict):
        _scope.update(target._kwargs)
    _scope.update({"input_value": target._raw_input_value, "value": target._raw_input_value,
                   "draw_state": target, "ds": target})
    FuncsMetadata.record(eval_view_func, _scope)

    # The snippet lives on the TARGET's draw_state as a persisted `eval_code`
    # field (excluded from auto-invalidation: typing must not re-render the
    # inspected view), so it reopens with what was last typed for this view.
    code = target.eval_code
    if code is None:
        code = "input_value"
    # Single-line editor: Enter never reaches the editor as a newline (its newline
    # handler is gated on `not single_line`); instead the menu claims the
    # enter-down event via its on_enter_key_down param and uses it to fire the
    # eval below.
    # return_extras gives the code box's draw_state so we can tell when it holds
    # text focus (and thus when Enter should fire the eval -- see below).
    box = draw_text(
        code, name=f"eval_code##{unique}", padding_right=100,
        single_line=True, show_bg=True, show_header=False,
        completion_source=eval_completion_source(eval_view_func),
        return_extras=True, tint=(0.05, 0.15, 0.08))
    code_changed, new_code = box[0], box[1]
    code_ds = box[2] if len(box) > 2 else None
    if code_changed:
        target.eval_code = new_code
        code = new_code

    def _fire_eval():
        # Stash the snippet + arm the trigger, then force the target to actually
        # re-render (bypassing its cache) so its wrapper runs func() -- and our
        # eval hook -- this/next frame.
        target.eval_code = code
        target._eval_pending = True
        target._eval_request_gen = getattr(target, '_eval_generation', 0) + 1
        target.invalidate()
        target._parent.invalidate_up(max_depth=5)
        request_render()

    run_clicked = button("Run", height=30, name=f"eval_run##{unique}",
                         color=(0.2, 0.7, 0.3), factor=0.8)[0]
    # Enter fires the eval. Listen for it directly (the way draw_text reads keys
    # off the frame queue) instead of relying on the menu to forward it: while the
    # single-line code box holds text focus, Enter never reaches it as a newline,
    # so we claim it here when that box is the focused editor. But when the
    # autocomplete popup is open, Enter ACCEPTS the highlighted suggestion (the box
    # consumes it + splices text),), so we must NOT also fire the eval that press.
    # `code_changed` is the reliable gate: a "run" Enter (popup closed) leaves the
    # single-line buffer untouched, while an accept always rewrites it. (Reading
    # code_ds._popup_open here is too late; draw_text already cleared it on accept.)
    enter_pressed = (code_ds is not None and not code_changed
                     and Core.melty.text_focused_ds is code_ds
                     and any(k in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER)
                             for k, _ in Core.melty.frame_key_events))
    if run_clicked or enter_pressed:
        _fire_eval()

    # The eval lands in the target's wrapper, a separate render pass. Until the
    # requested generation is served, keep THIS tab (and the owning menu) live so
    # we re-run and re-read the fresh result rather than serving a stale cache.
    if getattr(target, '_eval_generation', 0) < getattr(target, '_eval_request_gen', 0):
        draw_state.invalidate()  # this tab's own draw_state
        if menu_draw_state is not None:
            menu_draw_state.invalidate()  # the menu so it re-calls this tab
        request_render()

    eval_result = getattr(target, '_eval_result', None)
    if eval_result:
        # Titled with the snippet that produced it (stamped beside the result
        # by the wrapper's eval hook), so a finished run is visible at a
        # glance; no tree collapser - the result is always open.
        result_title = getattr(target, '_eval_result_code', None) or "eval"
        draw_text(eval_result, name=f"eval_result##{unique}",
                  display_name=result_title, is_tree=False,
                  show_bg=True, show_header=True,
                  show_name=True, editable=False, height=400, min_height=400,
                  wrap=True, bg_offset=-100, width=draw_state.content_width,
                  tint=(0.0, 0.0, 0.0))
    return False, input_value


@render_func(use_cache=False, show_bg=False, show_header=False, show_name=False, selectable=False, disable_scroll=False,
             temp=True, searchable=True)
def draw_input_tab(input_value, cm_state: ContextMenuState, draw_state, wrap=True, unique=None, class_to_show=None,
                   enter_key_pressed=None, **kwargs):
    """The three editable sources behind this view, in dispatch order:

      1. RENDER FUNCTION — the render_func whose body produced the view, edited
         whole via FunctionCodec. It isn't on the captured stack (the capture runs
         in the wrapper BEFORE the body executes, so the innermost frame is the
         filtered wrapper), so it's read from `_view_func`.
      2. CALLER — the direct `draw_x(...)` call site that invoked this view, edited
         via CallerCodec (spans just the call expression). `_call_site` is the
         nearest real caller (filename, lineno), captured once on menu-open with the
         render-dispatch machinery already filtered out (caller_site, core_render).
      3. DECORATIONS — the `@...` block on the value's class, edited via
         DecorationsCodec. `class_to_show` is resolved by draw_context_menu (the
         value's own class, or the nearest parent with source for a primitive
         field); only classes carry decorations, so it's skipped otherwise.
      4. MODE — the ACTIVE mode's entry kwargs in its enum class source
         (mode.py), edited via ModeCodec. Which member to show comes from the
         target's _kwargs ('current_mode', stamped by the wrapper when a mode
         config matched); skipped when no mode drove this view."""
    from meltygui.code.new_converters import host_code_state
    from meltygui.code.new_converters import recompile_button
    from meltygui.code.new_converters import recompile_status
    from meltygui.code.new_converters import run_recompile
    from meltygui.core.rendering.parameter_core import _source_priority
    from meltygui.core.rendering.render_dispatch import apply_param_source_matrix
    from meltygui.core.rendering.render_dispatch import collect_input_sources
    from meltygui.core.rendering.render_dispatch import param_source_matrix
    from meltygui.core.rendering.render_dispatch import signature_param_names


    srcs = collect_input_sources(input_value, cm_state, class_to_show)
    sources, source_tints = srcs["sources"], srcs["tints"]
    source_locations, source_kinds = srcs["locations"], srcs["kinds"]
    writable_sources = srcs["writable"]

    # ── Recompile (hotswap) - the same Run path code_file_io draws on a file
    # leaf (folder_files / FILE_TREE). A matrix edit saves SOURCE to disk via
    # the hosts' chain_out, but the live view function keeps its old defaults
    # until a hotswap. The button/Run work against the view_host's own
    # CodeState, the Run compiles the host's live root (the edit already
    # merged in), not a possibly-stale disk copy. None until the lazy host has
    # drawn/updated - the button appears a beat after the menu opens.
    code_state = host_code_state(cm_state.render_func_str)
    if code_state is not None and code_state.address is not None:
        clicked = recompile_button(code_state, unique=unique)
        recompile_status(code_state, draw_state)
        # Alt+Enter (or the editor's usual Ctrl+Enter) while over the tab -
        # enter_key_pressed is the menu-subscribed Enter-down InputEvent, same
        # mechanism as code_file_io's hotkey; modifiers ride on the event.
        hotkey = bool(enter_key_pressed and (enter_key_pressed.alt or enter_key_pressed.ctrl))
        run_recompile(input_value._view_func, code_state, draw_state,
                      start=clicked or hotkey, name=f"recompile{unique}")

    # ── Search - the STANDARD searchable path, no special filter box. The tab
    # is searchable=True, so Ctrl+F on it opens the framework's floating
    # find bar (core_render's searchable block), which maintains the term on
    # this draw_state.search_text and invalidates this subtree per keystroke.
    # That same text hits draw_param_matrix's live param filter below (which
    # auto-switches the screen to the best match), and the search session on
    # Melty.search_stack reaches the matrix cells' strings for in-place
    # highlighting like any other searchable widget.

    from meltygui.core.rendering.parameter_core import anywhere_value
    anywhere_value("view_func", input_value)
    if sources:
        _, matrix = param_source_matrix(sources, func=input_value._view_func,
                                        include_unmatched=True)
        changed, value = draw_param_matrix(matrix, source_tints=source_tints,
                                           source_locations=source_locations,
                                           # Rows in SourceKind order, highest
                                           # (the first candidates) on top;
                                           # registration order breaks ties
                                           # (sorted is stable).
                                           source_order=tuple(sorted(
                                               sources,
                                               key=lambda n: _source_priority(
                                                   source_kinds.get(n)))),
                                           source_dicts=sources,
                                           writable_sources=tuple(writable_sources),
                                           source_kinds=source_kinds,
                                           view_draw_state=input_value, wrap=True,
                                           width=draw_state.content_width - 0,
                                           priority_params=tuple(signature_param_names(input_value._view_func)),
                                           search_text=str(draw_state.search_text or ""),
                                           name=f"{input_value._view_func.__name__} inputs##matrix{unique}",
                                           disable_scroll=True)
        if changed and isinstance(value, dict):
            # Pure write-back (no UI) - call the bare function, not this wrapper.
            apply_param_source_matrix.__wrapped__(value, ref=sources, changed=True)

    # Each *_dict RenderHost parses its source on a background worker in its OWN draw
    # loop; this tab merely READS the materialized value (h.deep....) and draws it. When
    # a parse lands, the host's after_render() runs the loop but can't reach this
    # cached subtree - so register this tab's draw_state as a listener and the host
    # invalidates us when its value changes. Replaces the old "invalidate for the first
    # 10ms" guess, which expired before the ~400ms chain_in debounce, leaving the
    # dict blank until a manual mouse-over.
    caller_dict_hosts = [dh for (_sh, dh) in cm_state.call_site_hosts]
    for _h in (cm_state.render_func_dict, cm_state.decoration_dict, cm_state.class_dict,
               *caller_dict_hosts, cm_state.mode_dict):
        if _h is not None:
            _h.notify_on_change(draw_state)

    # common = dict(mode=Modes.NEW_CODE, min_width=100, max_height=300, fill_height=False)
    #
    # view_func = getattr(input_value, "_view_func", None)
    # if inspect.isfunction(view_func):
    #     draw_any(view_func, name=f"{view_func.__name__} (self)##self_{unique}", **common)

    # call_site = getattr(input_value, "_call_site", None)
    # if call_site is not None:
    #     filename, lineno = call_site
    #     draw_any(CallSite(filename, lineno), name=f"caller:{lineno}##caller_{unique}", **common)
    #
    # if isinstance(class_to_show, type):
    #     draw_any(Decorations(class_to_show),
    #              name=f"{class_to_show.__name__} decorations##deco_{unique}", **common)

    return False, input_value


@render_func(use_cache=True, show_bg=False, show_header=False, disable_scroll=False, show_name=False, selectable=False)
def draw_class_tab(input_value, class_to_show=None, class_is_parent=False, class_name='', **kwargs):
    """Editable class source. For a primitive field this is the parent object's
    class (e.g. Lora for a Lora.rank float) -- labelled so the source is clear.
    Routes through Mode.FILE_TREE — the same cache-backed code_file_io path a
    folder-files leaf uses — so all editors share one code path."""
    from meltygui.view.control_view import text
    from meltygui.core.rendering.render_dispatch import draw_any

    from meltygui.core.rendering.mode import Mode
    if class_is_parent:
        text(f"Parent type of {class_name}", name="Source",
             editable=False, tint=(1.0, 0.64, 0.113))
    cls_change, new_cls = draw_any(class_to_show, mode=Mode.FILE_TREE,
                                   name=class_to_show.__name__)
    return False, input_value


@render_func(use_cache=True, show_bg=False, show_header=False, show_name=False, selectable=False)
def draw_mode_tab(input_value, draw_state, current_mode=None, **kwargs):
    """Show the current Mode's value string."""
    from meltygui.view.control_view import text

    if current_mode is not None:
        mode_change, new_mode = text(str(current_mode.value), width=draw_state.content_width,
                                     name=str(current_mode))
    return False, input_value


def draw_context_menu_items(draw_state, items, right_click, name, unique):
    """The `context_menu={label: callable}` popover of a view: the dropdown's
    own menu (draw_dd_menu — rows, hover, keys, click-away) opened AT THE
    POINTER by a right-click instead of under a trigger button. Called by
    the wrapper (core_render's context-menu block) on every body run of the
    view, `right_click` = a right-click landed on it this run.

    Open/closed is the dropdowns' slot: the VIEW holds Melty.popover_focused_ds
    while its menu is open, exactly as a dropdown trigger does, so a click
    anywhere outside the menu hands the slot on (the wrapper's click-away
    clear_focus, which also re-runs this view) and the next call draws the
    menu closed. The menu is a latching window: it is called EVERY run with
    `closed=` so its window re-registers on each open (a window drawn only
    while its draw_state is already closed never registers — the reopen
    that died 09-10).

    A picked row runs its callable here and closes the menu; the Inspect row
    at the bottom reports INSPECT so the wrapper opens the inspector in the
    menu's place. Returns INSPECT, the picked label, or None."""
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.dropdown_view import draw_dd_menu
    from meltygui.core.rendering.render_dispatch import INSPECT
    from meltygui.model.dropdown_model import _dd_as_tuple
    from meltygui.core.layout.dropdown_core import _dd_close
    from meltygui.model.dropdown_model import _dd_label_for_path
    from meltygui.core.layout.dropdown_core import _dd_update_menu_size
    from meltygui.core.layout.dropdown_core import _ds_in_subtree
    import meltygui.core.windowing.window_api as glfw

    from meltygui.state.new_core_model import ContextMenuItemsState
    # [tint=(0.85, 0.75, 0.05)]
    inspect_label = f" Inspect"
    inspect_tint = (0.42, 0.24, 0.06)
    state_key = "context_menu_items_state"

    # The menu's state lives in the view's misc, where injected states go,
    # so it persists with the draw_state (DropDownState: cursor/open paths,
    # the drag-resized menu_size) and carries where the menu opened.
    state = draw_state.misc.get(state_key)
    if not isinstance(state, ContextMenuItemsState):
        state = draw_state.misc[state_key] = ContextMenuItemsState()
    draw_state.misc_used.add(state_key)

    is_open = Melty.popover_focused_ds is draw_state
    if right_click:
        if is_open:
            Melty.popover_focused_ds = None
            _dd_close(state)
        else:
            Melty.popover_focused_ds = draw_state
            Melty._popover_open_frame = Melty.frame_count  # grace the opening click
            mouse_x, mouse_y = imgui.get_mouse_pos()
            state.open_at = (mouse_x - draw_state.abs_left, mouse_y - draw_state.abs_top)
            _dd_close(state)
            state._kbd_mode = False
            state._last_mouse = None
        is_open = not is_open
        draw_state.invalidate()
        request_render()

    collection = {str(label): action for label, action in items.items()}
    collection[inspect_label] = INSPECT

    # Popover size: content-fit until the user drag-resizes it - the same
    # mechanism as draw_dropdown (see its popover-size comment): a forced
    # size goes on before the window begins, else a fit is stamped after
    # every open frame and a size that differs is the resize handle's work.
    menu_ds = state._menu_ds
    menu_size = state.menu_size
    if is_open and menu_ds is not None:
        opening = getattr(Melty, "_popover_open_frame", None) == Melty.frame_count
        if opening and menu_size is not None:
            menu_ds.width, menu_ds.height = menu_size
            state._menu_fit = tuple(menu_size)
        if menu_ds.width is None or menu_ds.width < 5:
            menu_ds.width = Toggles.Dropdown.min_width
    # The menu's top-left sits this far RIGHT of the pointer, so the pointer
    # rests on the first row rather than on the popover's left edge (the
    # wrapper's resize handle) — Lukas 09-10.
    # [tint=(0.939, 0.453, 0.245)]
    pointer_inset_x = 8
    # Below the pointer unless that runs past the display edge - then above it.
    open_x, open_y = state.open_at
    open_x += pointer_inset_x
    if menu_ds is not None and menu_ds.height:
        display_h = imgui.get_io().display_size[1]
        if draw_state.abs_top + open_y + menu_ds.height > display_h - 10:
            open_y -= menu_ds.height
    # The pointer is the anchor through the CURSOR (draw_tuple_fast's picker
    # does the same), never through window_pos: the wrapper folds a nested
    # window's window_pos offset into its content measure, so an offset
    # menu grew by that offset every frame. Restored after — the wrapper
    # reads the cursor right after this block for the view's own header.
    # Row labels are Tint.dd_text over the menu tint (its value × 2.16). A view
    # sitting in a dark-tinted window hands over a dark tint and the labels
    # came out dim against the popover (Lukas 09-10), so the tint's value is
    # floored here — raise the floor for brighter labels. The popover bg is
    # darkened by depth from the same tint and barely moves with it.
    # [tint=(0.994, 0.872, 0.0)]
    menu_tint_value_floor = 0.5
    menu_tint = draw_state.tint
    if menu_tint is not None:
        hue, saturation, value = rgb_to_hsv(*menu_tint[:3])
        if value < menu_tint_value_floor:
            menu_tint = hsv_to_rgb(hue, saturation, menu_tint_value_floor)
    cursor = imgui.get_cursor_screen_pos()
    imgui.set_cursor_screen_pos((draw_state.abs_left + open_x, draw_state.abs_top + open_y))
    changed, picked, menu_ds = draw_dd_menu(
        collection, tint=menu_tint,
        name=f"{name}##context_menu_items_{unique}",
        closed=not is_open, temp=True, shadow=False, auto_resize=False,
        window_pos=(0, 0), max_height=Toggles.Dropdown.max_height,
        parent_window=draw_state, swoosh=False, disable_scroll=False,
        show_search=False, row_tints={INSPECT: inspect_tint},
        root_state=state, path_prefix=(), return_extras=True)
    imgui.set_cursor_screen_pos(cursor)
    state._menu_ds = menu_ds
    if not is_open or menu_ds is None:
        return None

    _dd_update_menu_size(state, menu_ds)

    def _dismiss():
        Melty.popover_focused_ds = None
        _dd_close(state)
        draw_state.invalidate()
        request_render()

    if changed:
        label = _dd_label_for_path(collection, _dd_as_tuple(state._picked_path))
        _dismiss()
        if picked is INSPECT:
            return INSPECT
        if callable(picked):
            picked()
        return label

    if any(k == glfw.KEY_ESCAPE for k, _ in Core.melty.frame_key_events):
        _dismiss()
        return None

    # Click-outside dismissal - the MENU is the inside (a click back to the
    # view header closes it too, as a native context menu does).
    if imgui.is_mouse_clicked(0):
        mouse_x, mouse_y = imgui.get_mouse_pos()
        under = Core.melty.bvh_query(mouse_x, mouse_y)
        if not any(_ds_in_subtree(ds, menu_ds) for ds in under):
            _dismiss()
    return None


@render_func(use_cache=False, disable_scroll=True, show_header=False,
             header_same_line=False, show_tint=False, show_name=False, is_tree=False)
def draw_context_menu(input_value, draw_state, cursor_hover_inverted, func, unique=None, search_text='',
                      search_active=False,
                      enter_key_down=None, tab_state: TabState = None,
                      menu_state: ContextMenuWindowState = None, **kwargs):
    from meltygui.core.conversion.cache_tree import UNSET_VALUE
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.color_view import draw_tint_context
    from meltygui.view.header_view import flat_button
    from meltygui.view.tab_view import draw_tab_bar
    from meltygui.core.rendering.render_dispatch import _ancestor_call_line
    from meltygui.core.rendering.render_dispatch import _deferred_ancestors
    from meltygui.core.rendering.render_dispatch import _merged_call_stack_frames
    from meltygui.core.rendering.render_dispatch import _request_deferred_stacks

    if input_value is None:
        return False, None
    context_menu_offset = input_value.context_menu_offset
    # imgui.text(type(input_value._input_value).__name__)
    imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] - 1, imgui.get_cursor_screen_pos()[1] - 18))
    # if up_key_pressed:
    #     print("Up key pressed")g
    fa_up_arrow = ""
    fa_down_arrow = ""
    # The toolbar buttons are flat_buttons (draw-list, no @render_func
    # wrapper) styled to match `button` exactly: same text_value /
    # text_saturation / hover boosts, and the shadow lift = button's
    # z_offset 3 + the wrapper's shadow +1. The fill hue comes from the
    # tint (button's factor=1.0 makes `color` moot), so each call pushes the
    # tint the old button carried — the decorator's blue for the arrows.
    # [tint=(0.0, 0.241, 0.556)]
    button_default_tint = (0.0, 0.241, 0.556)

    def _menu_button(label, view_id, height, tint=button_default_tint):
        style_manager = Melty.style_manager
        previous = style_manager.push_tint_fields(*tint[:4])
        try:
            return flat_button(label, draw_state, view_id=view_id, height=height,
                               text_value=0.694, text_saturation=1.2,
                               hover_text_boost=1.5, shadow_offset=4.0,
                               event="left_mouse_down", style_manager=style_manager)
        finally:
            style_manager.pop_tint_fields(previous)

    if input_value._parent.id is not None:
        if _menu_button(fa_up_arrow, f"ctx_menu_up##{unique}", height=50):
            input_value.context_menu_offset += 1
            # Nav generation; rides into the func tab's select_line guard so
            # EVERY arrow press re-applies the auto-selection, even when
            # returning to a level whose line was selected before (the previous
            # ds persists, and its one-shot marker would otherwise skip it).
            input_value._scope_nav_seq = getattr(input_value, "_scope_nav_seq", 0) + 1
            Core.melty.cache.invalidate_up(draw_state._tile_id, max_depth=5)
            Core.melty.cache.invalidate_up(input_value._tile_id, max_depth=5)

        imgui.same_line()
    if input_value.context_menu_offset > 0:
        if _menu_button(fa_down_arrow, f"ctx_menu_down##{unique}", height=50):
            input_value.context_menu_offset = max(0, input_value.context_menu_offset - 1)
            input_value._scope_nav_seq = getattr(input_value, "_scope_nav_seq", 0) + 1
            Core.melty.cache.invalidate_up(draw_state._tile_id, max_depth=5)
            Core.melty.cache.invalidate_up(input_value._tile_id, max_depth=5)
    else:
        imgui.dummy(30, 30)

    imgui.same_line()
    imgui.text_colored(f"{context_menu_offset}", 1, 1, 1, 0.3)
    imgui.same_line()

    # Screenshot this menu's parent view, top of the menu below the nav arrows.
    # Deferred so the menu isn't in the shot: front the owning window (so the
    # view is visible), queue the view capture, hide this menu (asking to reopen
    # it afterward), then let screenshot.process_take_screenshot_flags grab the
    # view's shot a few frames later, open the shot in nemo, and reopen the menu.
    if _menu_button(f" ", f"screenshot_window##{unique}", height=30, tint=(0, 0, 0, 1.0)):
        from meltygui.core.graphics.screenshot import request_view_capture

        def _open_in_nemo(shot_path):
            # Full path + close_fds=False => posix_spawn, not fork (forking this
            # CUDA/GL process stalls the render thread).
            import shutil, subprocess
            nemo = shutil.which("nemo") or "/usr/bin/nemo"
            subprocess.Popen([nemo, shot_path], close_fds=False)

        view_ds = input_value  # the view this menu is for (offset-walked)
        Core.melty.move_window_to_front(view_ds.root_window)
        draw_state._reopen = True
        request_view_capture(view_ds, Core.melty.frame_count, reopen_menu_ds=draw_state,
                             on_captured=_open_in_nemo)
        draw_state.closed = True
        request_render()

    imgui.same_line()

    # Same deferred screenshot, then hand it to Claude: once the shot lands,
    # boot a fresh claude-d session pre-typed (NOT sent) with the shot path +
    # the view's render function (the same function the Input tab edits), and
    # open the Claude Terminals window so the new session's terminal comes up.
    if _menu_button(f" claude", f"claude_session##{unique}", height=30, tint=(0, 0, 0, 1.0)):
        from meltygui.core.graphics.screenshot import request_view_capture
        view_ds = input_value
        # Resolve the menu's offset-walked target so the shot + function match
        # what the tabs will show (the walk proper happens above the buttons).
        for _ in range(context_menu_offset):
            if view_ds._parent is None or view_ds._parent is view_ds:
                break
            view_ds = view_ds._parent
        fn = inspect.unwrap(view_ds._view_func)
        fn_name = getattr(fn, "__name__", "?")
        try:
            fn_file = inspect.getsourcefile(fn)
        except Exception:
            fn_file = None
        fn_line = getattr(getattr(fn, "__code__", None), "co_firstlineno", None)
        loc = f"{fn_file}:{fn_line}" if fn_file else "unknown location"

        def _start_claude(shot_path, _name=fn_name, _loc=loc):
            from meltygui.core.services.claude_terminal_core import launch_claude_session
            from meltygui.core.services.claude_terminal_core import open_claude_terminals_window
            launch_claude_session(
                f"Take a look at this screenshot of a view in the studio: {shot_path} "
                f"It is rendered by the function `{_name}` in {_loc}. ")
            open_claude_terminals_window()

        Core.melty.move_window_to_front(view_ds.root_window)
        draw_state._reopen = True
        request_view_capture(view_ds, Core.melty.frame_count, reopen_menu_ds=draw_state,
                             on_captured=_start_claude)
        draw_state.closed = True
        request_render()

    imgui.same_line()

    # Recapture the caller trace: the stack - and everything riding it
    # (caller f_locals types + stack-snapshot live values, the target's
    # in-scope publish, the one-shot body-locals capture) - is grabbed
    # ONCE per session and kept until restart. This button-arms the one-shot
    # gates so the target's next inline render captures fresh, and
    # invalidates up so the ancestors actually re-render: a cache-replayed
    # target renders without its parents on the stack, which would capture
    # a chain that bottoms out in dispatch machinery.
    bug_icon = ""  # fa-bug - red as a debug affordance
    if _menu_button(f"{bug_icon}", f"recapture_trace##{unique}", height=30,
                    tint=(0.42, 0.24, 0.06, 1.0)):
        _rc_target = input_value
        for _ in range(context_menu_offset):
            if _rc_target._parent is None or _rc_target._parent is _rc_target:
                break
            _rc_target = _rc_target._parent
        _rc_target._call_site_captured = False
        _rc_target._call_site_requested = True
        # Every deferred layer on the chain recaptures its queue-time stack
        # too, so the Code tab's splice is built from fresh parts.
        for _deferred in _deferred_ancestors(_rc_target):
            _deferred._deferred_call_stack_frames = None
            _deferred._deferred_stack_requested = True
            Core.melty.cache.invalidate_up(_deferred._tile_id, max_depth=5)
        Core.melty.cache.invalidate_up(_rc_target._tile_id, max_depth=5)
        request_render()

    imgui.same_line()

    offset_ds = input_value
    for i in range(context_menu_offset):
        if offset_ds._parent is None:
            break
        offset_ds = offset_ds._parent

    # The menu walked up to an ancestor (offset > 0). That ancestor never had its
    # OWN context menu open, so the context_menu_open capture gate never ran for
    # it and its _call_site is None — caller lenses come up empty. Ask its next
    # inline render to capture the site (lazy, one-shot), and invalidate it so it
    # re-renders fresh rather than from cache (where the capture line is skipped).
    if (offset_ds is not input_value and not offset_ds._call_site_captured
            and not offset_ds._call_site_requested):
        offset_ds._call_site_requested = True
        if Core.melty.cache is not None:
            Core.melty.cache.invalidate_up(offset_ds._tile_id, max_depth=5)
        request_render()
    # Same lazy one-shot for the queue-time stacks of every deferred layers
    # above the target: the Code tab splices them onto the target's stack.
    _request_deferred_stacks(input_value)

    # Scope-up auto-select: when the menu is walked up to an ancestor, resolve
    # the line inside the ancestor's view function that drew the ORIGINAL
    # element (from the original target's cached _call_stack, available
    # immediately, no waiting on the ancestor's lazy capture above), and have
    # the func tab select it.
    func_tab_select_line = None
    if offset_ds is not input_value:
        func_tab_select_line = _ancestor_call_line(input_value, offset_ds)
    # Arrow-press generation: part of the selection's one-shot key, so each
    # press re-selects but keeping the resolved line (and editor tab) changes.
    func_tab_select_seq = getattr(input_value, "_scope_nav_seq", 0)

    input_value._offset_ds = offset_ds
    input_value = offset_ds

    # Font awesome info icon unicode: \uf05a
    gear_icon = f"\uf013"
    config_icon_fa = f"{gear_icon} Config"
    info_icon_fa = " Info"
    view_func_name = offset_ds._view_func.__name__
    class_name = type(input_value._raw_input_value).__name__
    paint_brush_icon = f"\uf1fc"
    tint_tab_name = f"{paint_brush_icon} Tint"
    terminal_icon = f"\uf120"  # fa-terminal
    eval_tab_name = f"{terminal_icon} Eval"
    keyboard_icon = f"\uf11c"  # fa-keyboard
    input_tab_name = f"{keyboard_icon} Input"
    # Lightning bolt
    live_icon = f"\uf0e7"
    live_tab = f"{live_icon} Live"
    code_stack_icon = f"\uf121"
    code_stack_tab = f"{code_stack_icon} Code"

    # Resolve which class's source to show in the class tab.
    # For a non-primitive value that's just the value's own class. For a
    # primitive field (e.g. a float `rank`) the value itself has no source,
    # so walk up the parent chain to the nearest object that does have source
    # code -- so e.g. Lora.rank still shows Lora's class, labelled as parent.
    raw_value = input_value._raw_input_value
    class_to_show = None
    class_is_parent = False
    # A bubbling tree node's type is a runtime-generated `Bubbling_<Base>` with no source
    # — resolve its real base (e.g. GeneralParse) so the Class/Decorations tabs resolve
    # rather than erroring.
    from meltygui.core.conversion.bubbling import base_of_bubbling
    # Use exact-type matching, not isinstance: a subclass of a primitive
    # (e.g. CodeLine(str)) DOES have its own source, so it should show its
    # own class tab rather than being treated as a bare primitive.
    if isinstance(raw_value, type):
        # The view is a CLASS itself (e.g. the Toggles window draws the
        # Toggles class via @window). The class whose source to show is the
        # value, not its metaclass - type(Toggles) is `type`, a builtin with
        # no source, which would blank every right-side source row (class
        # var / @defaults / @window on the class).
        class_to_show = base_of_bubbling(raw_value)
    elif type(raw_value) not in (int, float, str, bool):
        class_to_show = base_of_bubbling(type(raw_value))
    else:
        max_walk = 4
        ancestor = input_value._parent
        while ancestor is not None and max_walk > 0:
            a_raw = getattr(ancestor, '_raw_input_value', UNSET_VALUE)
            a_type = base_of_bubbling(type(a_raw)) if a_raw is not UNSET_VALUE else None
            if a_type is not None and getattr(a_type, '__module__', None) \
                    not in (None, 'builtins', '_collections_abc'):
                class_to_show = a_type
                class_is_parent = True
                break
            ancestor = ancestor._parent
            max_walk -= 1

    # Font awesome: fa-code () for the view function, fa-cube () for the class.
    func_tab = f" {view_func_name}"
    if class_to_show is not None:
        class_tab = f" {class_to_show.__name__}" + (" (parent)" if class_is_parent else "")
    else:
        class_tab = f" {class_name}"

    # Static tint colors for the fixed Config / Info tabs; remaining tabs use the neutral grey.
    config_tint = (0.12, 0.38, 0.772)
    info_tint = (0.545, 0.469, 0.012)

    tab_names = []
    tab_tints = []
    tab_names.append(info_icon_fa)
    tab_tints.append(info_tint)
    tab_names.append(config_icon_fa)
    tab_tints.append(config_tint)
    tab_names.append(func_tab)
    tab_tints.append(None)
    tab_names.append(eval_tab_name)
    tab_tints.append((0.2, 0.7, 0.3))  # green for the eval/REPL tab
    tab_names.append(input_tab_name)
    tab_tints.append((0.4, 0.2, 0.7))  # purple for the input tab
    tab_names.append(tint_tab_name)
    tab_tints.append(Core.melty._saturated_rgb(draw_state.tint))  # orange tint for the tint tab
    if class_to_show is not None:
        tab_names.append(class_tab)
        tab_tints.append(None)

    tab_names.append(live_tab)
    tab_tints.append((0.7, 0.0, 0.0))
    tab_names.append(code_stack_tab)
    tab_tints.append((0.9, 0.35, 0.28))  # the code trace view's own tint

    indices = list(range(len(tab_names)))

    if not tab_state.selected_tabs:
        tab_state.selected_tabs = [indices[Toggles.ContextMenu.default_tab]]

    current_mode = input_value._kwargs.get('mode', None)
    mode_tab = str(current_mode)
    if current_mode is not None:
        tab_names.append(mode_tab)

    # Indices list

    imgui.dummy(0, 1)
    imgui.same_line()
    tab_changed, new_tabs = draw_tab_bar(tab_state.selected_tabs, names=tab_names, wrap=True, tab_height=40,
                                         tint_value=0.7,
                                         # 282 = nav arrows + counter + shot +
                                         # claude; +46 for the + (trace
                                         # recapture) button.
                                         width=max(50, draw_state.content_width - 328),
                                         show_bg=True, name=f"tab_bar#{view_func_name}{unique}",
                                         z_offset=-0.5, bg_offset=-7, draw=True,
                                         collection=indices, tints=tab_tints, as_toggles=False)
    if tab_changed:
        tab_state.selected_tabs = new_tabs
        # The compact input tab's initial fit leaves no room for source rows.
        # Give Inputs a usable viewport when selecting it from that small fit.
        if tab_names.index(input_tab_name) in new_tabs:
            minimum_height = min(700, int(imgui.get_io().display_size.y * 0.8))
            if (draw_state.height or 0) < minimum_height:
                draw_state.height = minimum_height
                draw_state.invalidate()

    imgui.dummy(0, 2)

    # Columns stripped: render the selected tab(s) stacked at the menu's full
    # width - one all across the menu, several fill the height. No Column calls,
    # so nothing here triggers the window-frame edge system (the source of the
    n_sel = max(1, len(tab_state.selected_tabs))
    full_w = draw_state.content_width
    clip = draw_state.abs_clip_rect
    top_y = imgui.get_cursor_screen_pos()[1]
    # First-load auto-fit (same pattern as draw_global_search's height
    # auto-fit): until the menu has been fitted once, hand the tab bodies NO
    # height so they render at their natural extent - the usual tab_h derives
    # from the current window clip rect which is circular while we're still
    # choosing the window height. fit_done PERSISTS with the menu's
    # draw_state (ContextMenuWindowState): a menu restored open at boot
    # already had its size, and re-fitting it overwrote that size.
    fitting = not menu_state.fit_done
    tab_h = max(60.0, (clip[3] - top_y) / n_sel - 6) if (clip is not None and not fitting) else None

    for t_idx, static_tab in enumerate(tab_state.selected_tabs):
        size_kw = {"width": full_w}
        if tab_h is not None:
            size_kw["height"] = tab_h
        if static_tab >= len(tab_names):
            # A persisted selection that outran the current tab set: fall back
            # to the func tab rather than indexing out of range.
            draw_func_tab(input_value, name=f"func_tab_{t_idx}##{unique}",
                          disable_scroll=True, select_line=func_tab_select_line,
                          select_seq=func_tab_select_seq, draw_state=draw_state, **size_kw)
            continue
        this_tab = tab_names[static_tab]
        if this_tab == tint_tab_name:
            draw_tint_context(input_value, name=f"Context Tint##{unique}", **size_kw)

        elif this_tab == info_icon_fa:
            # Resolve the effective search term (menu kwarg, else its search box).
            info_search = search_text if search_text != "" else draw_state.search_text
            draw_info_tab(input_value, search_text=info_search, unique=unique,
                          name=f"info_tab_{t_idx}##{unique}", **size_kw)

        elif this_tab == config_icon_fa:
            draw_config_tab(input_value, name=f"config_tab_{t_idx}##{unique}", **size_kw)

        elif this_tab == func_tab:
            draw_func_tab(input_value, name=f"func_tab_{t_idx}##{unique}",
                          disable_scroll=False, select_line=func_tab_select_line,
                          select_seq=func_tab_select_seq, draw_state=draw_state, **size_kw)

        elif this_tab == eval_tab_name:
            draw_eval_tab(input_value, unique=unique, enter_key_down=enter_key_down,
                          menu_draw_state=draw_state,
                          name=f"eval_tab_{t_idx}##{unique}", **size_kw)

        elif this_tab == input_tab_name:
            draw_input_tab(input_value, class_to_show=class_to_show,
                           name=f"input_tab_{t_idx}##{unique}", wrap=False,
                           disable_scroll=False, **size_kw)


        elif this_tab == class_tab:
            draw_class_tab(input_value, class_to_show=class_to_show, class_is_parent=class_is_parent,
                           class_name=class_name, name=f"class_tab_{t_idx}##{unique}", **size_kw)

        elif this_tab == mode_tab:
            draw_mode_tab(input_value, current_mode=current_mode,
                          name=f"mode_tab_{t_idx}##{unique}", **size_kw)

        elif this_tab == live_tab:
            draw_live_tab(input_value, name=f"live_tab_{t_idx}##{unique}", **size_kw)

        elif this_tab == code_stack_tab:
            # The stack captured at menu open (core_render's one-shot grab -
            # `_call_stack_frames`: (path, lineno, func_name, locals) tuples,
            # outermost first) with its deferred ancestor's queue-time
            # stack spliced in (_merged_call_stack_frames), rendered as a
            # stack trace view. Values come from the frames' captured locals
            # through pane-LOCAL stores - nothing published, nothing global.
            # The debug (bug) button above recaptures a fresh stack.
            from meltygui.view.trace_view import draw_stack_trace
            captured_stack = _merged_call_stack_frames(input_value, menu_state)
            if captured_stack:
                # indent_views=False: the tab is narrow - panes slide left
                # instead of the inlined call-chain slide.
                draw_stack_trace(
                    captured_stack, indent_views=False,
                    hide_dispatch=Toggles.ContextMenu.code_tab_hide_dispatch,
                    crumb_headers=True,
                    name=f"code_tab_{t_idx}##{unique}", **size_kw)
            else:
                RenderFuncs.draw_text(
                    "No captured stack for this view yet — press the bug "
                    "button above to recapture the trace.",
                    name=f"code_tab_empty_{t_idx}##{unique}", show_bg=False,
                    editable=False, tint=Tint.subtle_text())
    imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] - 1, imgui.get_cursor_screen_pos()[1] - 18))
    imgui.text(f"{input_value._raw_input_value.__class__.__name__}")

    imgui.dummy(0, 30)

    # First-load fit, two phases. Phase 0: set the default width - width
    # drives wrap, so a height measure is only honest once the content has
    # rendered AT that width; invalidate unconditionally (use_cache would
    # otherwise replay the tile and skip phase 1). Phase 1: the tab bodies
    # just rendered at no height, so the cursor bottom IS the content
    # extent. Write the size immediately - the wrapper's measured item_rect
    # can't shrink a fixed-width window - cap at most of the display, and fit
    # ONCE per menu draw_state: the ds persists across open/close, so reopens
    # and tab switches keep the user's size.
    if fitting:
        if menu_state.fit_phase == 0:
            menu_state.fit_phase = 1
            if (draw_state.width or 0) < 520:
                draw_state.width = 520
                draw_state._source["width"] = "context menu first-load default width"
            draw_state.invalidate()
            request_render()
        else:
            content_bottom = imgui.get_cursor_screen_pos()[1]
            new_h = max(100, int(content_bottom - draw_state._abs_top() + 10))
            disp_h = imgui.get_io().display_size.y
            if disp_h > 0:
                new_h = min(new_h, int(disp_h * 0.8))
            if draw_state.height is None or abs(draw_state.height - new_h) > 1:
                draw_state.height = new_h
                draw_state._source["height"] = "context menu first-load auto-fit"
                draw_state.invalidate()
                request_render()
            menu_state.fit_done = True

    # Never open the menu partially off-display. While the window offset is
    # still the fresh default reset (0,0) - core_render does that on every
    # right-click open - shift window_pos (the additive offset in the pinned
    # branch of _abs_left/_abs_top) so the whole window fits inside the
    # display. A user drag writes window_pos and ends this clamping; blit
    # placement follows abs pos anyway, so no invalidate is needed for a move.
    wp = draw_state.window_pos or (0, 0)
    if not fitting and tuple(wp) == (0, 0):
        disp = imgui.get_io().display_size
        x0, y0 = draw_state._abs_left(), draw_state._abs_top()
        w = draw_state.width or draw_state.content_width or 0
        h = draw_state.height or 0
        dx = dy = 0.0
        if disp.x > 0:
            if x0 + w > disp.x:
                dx = disp.x - (x0 + w)
            if x0 + dx < 0:
                dx = -x0
        if disp.y > 0:
            if y0 + h > disp.y:
                dy = disp.y - (y0 + h)
            if y0 + dy < 0:
                dy = -y0
        if abs(dx) > 0.5 or abs(dy) > 0.5:
            draw_state.window_pos = (wp[0] + dx, wp[1] + dy)
            request_render()
    return False, input_value


# The type renderers: a type's name (the default for `type` values) and its
# class variables as an editable collection.
@render_func(is_default_for=(type), tint=(0.928, 0.836, 0.655, 0.308), use_cache=True,
             header_single_line=True, show_name=True, temp=True, is_tree=False, shadow=False,
             show_bg=True, with_header=draw_header)
def draw_type_name(input_value, **kwargs):
    try:
        if isinstance(input_value, str):
            imgui.text(f"{input_value}")

        else:
            imgui.text(f"{input_value.__name__}")
    except Exception as e:
        imgui.text(f"Error displaying type: {e}")


@render_func(show_bg=True, align_header=False, use_cache=True, shadow=False,
             with_header=draw_header)
def draw_type(input_value: type, **kwargs):
    from meltygui.view.collection_view import draw_collection

    try:
        class_vars = {**{k: getattr(input_value, k) for k in vars(input_value)}}

        changed, new_dict = draw_collection(class_vars, real_type=input_value, disable_scroll=True,
                                            name=f"Class: {input_value.__name__}")

        if changed:
            for k, v in new_dict.items():
                if k.startswith("_"):
                    continue
                try:
                    imgui.text(f"Setting attribute {k} to value {v} on class {input_value.__name__}")
                    setattr(input_value, k, v)
                except Exception as e:
                    imgui.text(f"Error setting attribute {k} on class {input_value.__name__}: {e}")
    except Exception as e:
        imgui.text(f"Error rendering type {input_value}: {e}")
