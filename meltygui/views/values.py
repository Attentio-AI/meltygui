import inspect
import os
import re
import sys
import threading
import time
import traceback
import types
from collections import deque, defaultdict, namedtuple
from collections.abc import MutableMapping
from enum import Enum
from inspect import Parameter
from math import sqrt
from pathlib import Path
from types import NoneType
from typing import Any

import OpenGL.GL as gl
import glfw
import numpy
import torch
from imgui.core import _DrawList

from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.global_style import GlobalStyle
from src.lsd.gl_gui.melty import Melty, CollectionAction, ManagedWindow, SearchTerm
from src.lsd.gl_gui.model.core_model.draw_state import ZoomState, TileMode, DrawState, TabState, DropDownState, ExpandMode
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.notifications import display
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Toggles, Tint, mix
from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, push_style_var, \
    pop_style_var, end, begin
from src.lsd.gl_gui.utils.glfw_utils import print_stack_trace, request_render
from src.lsd.gl_gui.view.core_conversion.bubbling import _BubblingDict, _DeepPath
from src.lsd.gl_gui.view.core_conversion.cache_tree import UNSET_VALUE
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import Comment, GeneralParse, UsageRef, CallParse, \
    ClassParse, EnumParse, FunctionParse, SymbolUsage, cst_module_to_dict, dict_to_cst_module
from src.lsd.gl_gui.view.core_conversion.new_codecs import CallSite
from src.lsd.gl_gui.view.core_conversion.new_converters import code_file_io, convert_in_and_out_value, \
    cst_module_to_string, string_to_cst_module, code_hosts_for, host_code_state, \
    recompile_button, recompile_status, run_recompile
from src.lsd.gl_gui.view.core_conversion.path_finder import Pending
from src.lsd.gl_gui.view.core_views.basic_view_utils import same_line
from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int, add_shadow
from src.lsd.gl_gui.view.core_views.codec_register import registry as FILE_CODECS
from src.lsd.gl_gui.view.core_views.core_render import render_func, render_func_kwarg_names
from src.lsd.gl_gui.view.core_views.anywhere import SourcePriority, _source_priority, _sources_for, \
    _driving_source, _setting_source, default_write_source, get_value_for_source, get_source_for, \
    from_anywhere, anywhere_value, set_anywhere, \
    flush_deferred_writes, \
    SET_ANYWHERE_PARAMS
from src.lsd.gl_gui.view.core_views.core_undo import NavUndo, UndoManager
# Module import (not "from ... import DragDrop`) so hotswaps rebind cleanly.
from src.lsd.gl_gui.view.core_views import drag_drop as _drag_drop
from src.lsd.gl_gui.view.core_views.cst_proxy import *
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import hotkey, Core
from src.lsd.gl_gui.view.core_views.decoration.invalidation_decoration import live
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.headers import draw_header, draw_header_end, draw_footer, render_search, \
    annotation_item_type, flat_button
from src.lsd.gl_gui.view.core_views.inspect_utils import set_fn_defaults
from src.lsd.gl_gui.view.core_views.text_editor import draw_text, _scroll_into_view, _brightness_clamp
from src.lsd.gl_gui.view.core_views.search_glow import draw_search_highlight
from src.shader_library.shader_manager.texture_manager import PendingTexture
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults



@render_func(use_cache=True, show_bg=True, width=20, height=22, tile_mode=TileMode.MAX,
             auto_resize=False, just_shadow=True, selectable=False, no_cursor=True, temp=True)
def empty(input_val):
    pass


@render_func(is_default_for=(types.FrameType), use_cache=True, tint=(0.6, 0.2, 0.0),
             header_same_line=False, show_bg=False, align_header=False, closed=False,
             shadow=True, selectable=False, wrap=False, with_header=draw_header,
             indent_size=5, searchable=True, shaodw=False, bg_offset=3)
def draw_frame(input_value: types.FrameType, draw_state, **kwargs):
    file_name_truncated = Path(input_value.f_code.co_filename).name
    imgui.text(f"{file_name_truncated}:{input_value.f_lineno} in {input_value.f_code.co_name}")

    # Jump-to-error: open the frame's source file at the failing line. Same
    # threaded open_in_intellij pattern the jump-to-caller button uses.
    if button(f"{file_name_truncated}:{input_value.f_lineno}",
              height=30, value=0.4, saturation=1.5, name="jump_to_frame")[0]:
        from src.lsd.gl_gui.utils.jump_to_code import open_in_intellij
        threading.Thread(
            target=open_in_intellij,
            args=(str(input_value.f_code.co_filename),),
            kwargs={"line_number": input_value.f_lineno},
            daemon=True).start()
    # Loop over the frame's local variables, which are the most relevant to debugging.

    draw_text("Locals", name="Locals", show_header=False,
              font=Font.JETBRAINS_MONO_40, bg_offset=3)

    for var_name, var_value in input_value.f_locals.items():
        # Display the variable name and its value.
        imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] + 40, imgui.get_cursor_screen_pos()[1]))
        imgui.begin_group()
        draw_any(var_value, with_header=draw_header, show_header=True, name=var_name, mode=Modes.READ_ONLY)
        imgui.end_group()


@render_func(is_default_for=types.ModuleType, use_cache=True,
             show_bg=True, with_header=draw_header, with_footer=draw_footer)
def draw_module(input_value: types.ModuleType, draw_state, **kwargs):
    imgui.text(f"Module: {input_value.__name__}")


@render_func(is_default_for=(type), tint=(0.93, 0.56, 0.23, 0.308), use_cache=True,
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


def _collection_match_keys(input_value, keys, excluded, show_excluded):
    """The (index, lowercased key string) pairs draw_collection renders and
    searches, in key order — the basis for both counting key matches and
    resolving which key holds the current match, without rendering. `index` is
    the position in `keys`, so it lines up with the render loop. Mirrors the
    loop's key-string derivation and skip filters."""
    out = []
    parent_cls_name = input_value.__class__.__name__
    excl_attrs = getattr(type(input_value), "__excluded_attrs__", None)
    for idx, key in enumerate(keys):
        if isinstance(key, (float, Enum, NoneType)):
            key_str = parent_cls_name
        elif isinstance(key, int):
            key_str = f"{key}"
        else:
            key_str = str(key)
        if str(key).split("##")[0] in excluded:
            continue
        if (not show_excluded and excl_attrs is not None
                and not Toggles.show_excluded and str(key) in excl_attrs):
            continue
        if not show_excluded and (key_str.startswith("_") or key_str.endswith("_")):
            continue
        out.append((idx, key_str.lower()))
    return out


def _fuzzy_substring_distance(q, k):
    """Min edit distance between `q` and any substring of `k` (the k-differences
    DP: row 0 is all zeros so the match may start anywhere in k). Damerau/OSA, so
    an adjacent transposition — the most common typo — costs 1, not 2. Both
    lowercase."""
    m, n = len(q), len(k)
    if m == 0:
        return 0
    prev2 = None
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        qi = q[i - 1]
        cur = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if qi == k[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (prev2 is not None and j > 1
                    and qi == k[j - 2] and q[i - 2] == k[j - 1]):
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
        prev2 = prev
        prev = cur
    return min(prev)


def _fuzzy_key_match(q, k):
    """Does query `q` match candidate `k` (both lowercase), tolerating a few
    typos? Exact substring first (fast, also covers short queries); for longer
    queries fall back to approximate substring matching with a small edit budget
    that scales with length (~1 typo per 4 chars). The single predicate the key
    match's count, current index and highlight all share, so they stay in sync."""
    if not q:
        return False
    if q in k:
        return True
    if len(q) < 4:
        return False
    return _fuzzy_substring_distance(q, k) <= max(1, len(q) // 4)


# --- Global search indexes ---------------------------------------------------
# Global search queries a fixed set of index providers instead of walking the
# live draw_state tree. Each provider returns SearchHit entries — a display
# label, a row tint, and an activate() that performs the jump — so a data
# source is searchable whether or not it's on screen (or exists as draw_states
# at all). To add a data source, register a provider with @search_index.
# `match` (optional) is the string queries run against when the label carries
# extra context — e.g. a file hit's label is the full relative path but its
# match key is the bare filename. None → match the label itself.
# `state` (optional) is a zero-arg callable returning the hit's current VALUE —
# a row that has one renders it live: a bool as an on/off switch, an
# int/float/str as its editing widget (`set_state(value)` writes an edit back).
# `keep_open` marks a hit whose activation is an EDIT rather than a jump: the
# search window stays up afterwards so a run of settings can be changed
# without reopening it.
# `icon` (optional) is a glyph string drawn before the label in the result row
# (e.g. an action's @defaults(icon=...)).
# `parts` (optional) is a pre-coloured render plan for the row's text:
# [(segment, color_u32 | None, wash_u32 | None)] — color None means "the
# row's own tint"; a non-None wash paints the editor's definition block-wash
# band behind the segment (2-tuples are accepted, wash None). Text hits use
# it to draw snippets exactly as the editor renders them; rows without parts
# draw the plain label.
# `goto` (optional) is a zero-arg callable jumping to the hit's DEFINITION —
# Shift+Enter on the row — for hits whose plain activation does something
# other than jump (actions run, toggles edit; both goto their toggles.py code).
# `code_row` (optional) = (path, line, prefix, code, suffix): render the code
# through the REAL editor (draw_text + the file's live cst-dict parse), so a
# search row is pixel-identical to the editor at the jump target — washes,
# token colors, everything. `parts` stays as the fallback plan when the
# editor path fails.
# `file` (optional) = the BASENAME of the file the hit lives in, drawn as a
# dim suffix after the label (skipped on Files rows — there the label IS the
# file). Display-only: label keeps the full identity/match text.
SearchHit = namedtuple("SearchHit", "label tint activate kind match state keep_open set_state icon parts goto code_row file",
                       defaults=("", None, None, False, None, None, None, None, None, None))

# The combined meta-category: every category's best hits interleaved
# (see _all_tab_items). Always a selector chip; never a hit's `kind`.
ALL_CATEGORY = "All"


def _search_cats(extra_kinds=()):
    """Selector order: the All tab, then Toggles.GlobalSearch.search_priority,
    then any hit kinds the priority list doesn't name (under their own name)."""
    cats = [ALL_CATEGORY] + [str(k) for k in Toggles.GlobalSearch.search_priority]
    cats += [k for k in extra_kinds if k not in cats]
    return cats

_WINDOW_CAT_TINT = (0.55, 0.9, 0.65)
_FILE_CAT_TINT = (0.72, 0.62, 0.35)
_TEXT_CAT_TINT = (0.38, 0.68, 0.72)
_TOGGLE_CAT_TINT = (0.63, 0.47, 0.86)
_ACTION_CAT_TINT = (0.92, 0.58, 0.25)


# FontAwesome glyphs (merged into the UI fonts) shown before a search
# row's label. A hit's own icon (e.g. an action's @defaults icon) wins.
_CATEGORY_ICONS = {
    "Toggles": "",    # sliders
    "Actions": "",    # bolt
    "Windows": "",    # window-maximize
    "Files": "",      # file
    "Text": "",       # align-left
    "Classes": "",    # cube
    "Functions": "",  # code
}


def _category_icon(kind):
    return _CATEGORY_ICONS.get(kind)


def _cst_parse_tint(*parse_types):
    """The @defaults tint the cst-dict view renders these parse types with
    (first type declaring one wins — default_kwargs_by_type is an exact-type
    lookup, so fallbacks are listed explicitly). Class/function search rows
    reuse it so a symbol hit is coloured exactly like its parse in the dict
    pane, and stays in sync if those @defaults change. RGB only — the parse
    tints' alpha is bg-wash opacity, meaningless on a search row."""
    for t in parse_types:
        tint = Melty.default_kwargs_by_type.get(t, {}).get("tint")
        if tint:
            return tuple(tint[:3])
    return (0.009, 0.2495, 0.39)  # FunctionParse's declared rgb, without a default


def _category_tint(kind):
    if kind == "Classes":
        return _cst_parse_tint(ClassParse, EnumParse, GeneralParse)
    if kind == "Functions":
        return _cst_parse_tint(FunctionParse)
    if kind == "Windows":
        return _WINDOW_CAT_TINT
    if kind == "Files":
        return _FILE_CAT_TINT
    if kind == "Text":
        return _TEXT_CAT_TINT
    if kind == "Toggles":
        return _TOGGLE_CAT_TINT
    if kind == "Actions":
        return _ACTION_CAT_TINT
    return (1, 1, 1)


# (id(class) → rgb tint or None) - cleared whenever the symbol hits
# rebuild, so a tint edit shows up within one _src_mod_map TTL.
_symbol_tint_memo = {}


def _class_source_tint(obj, path):
    """The tint a class's SOURCE declares — a `@window(tint=...)`-style
    decorator kwarg, a `# [tint=...]` override comment above the def, or a
    class-body `tint = (...)` — via the editor's _scan_def_tint_lines, the
    same resolution definition tints use (and the same stores the cst-dict
    parse merges into __overrides__). Covers root AND nested classes: the def
    line is found by scanning for `class <name>` at any indent. Lazy +
    memoized — call it only for rows actually displayed."""
    key = id(obj)
    if key in _symbol_tint_memo:
        return _symbol_tint_memo[key]
    tint = None
    try:
        from src.lsd.gl_gui.view.core_views.text_editor import _scan_def_tint_lines
        src = Melty.read_code(path)
        if src:
            lines = src.splitlines()
            last = obj.__qualname__.rsplit(".", 1)[-1]
            pat = re.compile(rf"^\s*class\s+{re.escape(last)}\b")
            for i, ln in enumerate(lines):
                if pat.match(ln):
                    res = _scan_def_tint_lines(lines, i + 1, last)
                    if res is not None:
                        tint = tuple(res[0][:3])
                    break
    except Exception:
        tint = None
    _symbol_tint_memo[key] = tint
    return tint


def _hit_own_tint(hit):
    """True when the hit carries its OWN tint — a window's real tint, a class's
    source tint, a toggle group's @defaults tint, a file's FileMeta tint —
    rather than the bare category fallback colour. The All tab ranks these
    ahead of untinted hits."""
    tint = hit.tint() if callable(hit.tint) else hit.tint
    if not (isinstance(tint, (tuple, list)) and len(tint) >= 3):
        return False
    return tuple(tint[:3]) != tuple(_category_tint(hit.kind)[:3])


# Group label for the All tab's leading block (every category's #1 hit).
# Not a hit kind - _category_tint falls through to white for it.
TOP_GROUP = "Top"


def _all_tab_items(by_kind):
    """(rows, groups) for the All tab — `groups` is parallel to `rows` and
    names each row's label line: a "Top" block first (every category's #1
    hit, priority order), then each category's next-best hits (up to 3 per
    category total) under the category's own label. Categories follow
    Toggles.GlobalSearch.search_priority. Own-tinted hits (see _hit_own_tint)
    ALWAYS outrank untinted ones: within a category (so they claim its 3
    slots) and within the Top block. Sorts are stable, so rank order and
    priority order hold within a tint tier."""
    order = [k for k in _search_cats(by_kind) if k != ALL_CATEGORY and by_kind.get(k)]
    ranked = {k: sorted(by_kind[k], key=lambda h: not _hit_own_tint(h))[:3]
              for k in order}
    tops = sorted((ranked[k][0] for k in order), key=lambda h: not _hit_own_tint(h))
    rows, groups = list(tops), [TOP_GROUP] * len(tops)
    for k in order:
        rest = ranked[k][1:]
        rows.extend(rest)
        groups.extend([k] * len(rest))
    return rows, groups


GLOBAL_SEARCH_INDEXES = []


def search_index(fn):
    GLOBAL_SEARCH_INDEXES.append(fn)
    return fn


def _launch_window(name):
    """Launch a registered window by name: open (un-hide), summon it next to
    the search window — so it comes to you instead of appearing at its old,
    maybe off-screen, spot — raise it, and sole-select it so it shows Melty's
    selection outline."""
    target = Core.melty.find_window(name)
    if target is None:
        return
    target.closed = False
    gs = Core.melty.find_window("GlobalSearch")
    if gs is not None and gs.abs_left is not None:
        Core.melty.summon_window(target, gs.abs_left, gs.abs_top)
    else:
        Core.melty.move_window_to_front(target)
    Core.melty.focused_ds = target
    Core.melty.selected = {target}
    Core.melty.last_selected = target
    request_render()


@search_index
def window_index():
    """Every window registered with the window manager — open or closed, dock
    displayed or not — minus the manager's excluded list and the search window
    itself. Activating a hit launches the window."""
    from src.lsd.gl_gui.toggles import WindowManager
    hits = []
    for mw in list(Core.melty.registered_windows.values()):
        wds = mw.draw_state
        name = getattr(mw, 'name', None) or (wds.name if wds is not None else None)
        if not name or str(name) in WindowManager.excluded_windows:
            continue
        label = str(name).split("##")[0].strip()
        if not label or label == "GlobalSearch" or not any(c.isalnum() for c in label):
            continue
        # Tint: the window draw_state's RESOLVED tint (what it actually renders
        # with), falling back to the @window registration - its kwargs, then
        # the registered class's own tint attr.
        tint = (wds._kwargs or {}).get("tint") if wds is not None else None
        reg = (Melty.annotated_window_classes.get(label)
               or Melty.annotated_window_classes.get(str(name)))
        if not tint and reg is not None:
            w_cls, w_kwargs = reg
            tint = w_kwargs.get("tint") or getattr(w_cls, "tint", None)
        hits.append(SearchHit(label, tint, lambda n=str(name): _launch_window(n),
                              kind="Windows"))
    return hits


def _symbol_def_line(obj):
    """The definition line of a live function/class, DISK coordinates (None if
    unresolvable). Functions read co_firstlineno off the unwrapped object —
    cheap; classes fall back to inspect.getsourcelines, which parses the whole
    module, so only call this off the render thread (activation runs it on the
    IDE-opener daemon thread)."""
    try:
        obj = inspect.unwrap(obj)
    except Exception:
        pass  # no proxy fabricating @wrapped__ - use the object as-is
    try:
        # getsourcelines (and co_firstlineno) start at the first DECORATOR
        # line - land on the `def`/`class` statement itself instead.
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import _def_stmt_line
        lines, start = inspect.getsourcelines(obj)
        return _def_stmt_line(lines, start)
    except Exception:
        code = getattr(obj, "__code__", None)
        return code.co_firstlineno if code is not None else None


def _jump_to_symbol_def(obj, path):
    """Open a symbol's definition in the in-app code editor (AppModel.open_files
    → draw_code_editor). The file opens immediately on the render thread (host
    creation is cheap; the load itself is background). obj=None opens the file
    with no line — the Files category's jump. Line resolution for classes
    parses the whole module, so it stays on a daemon thread and lands on
    open_files.jump_to_line once known."""
    from src.lsd.gl_gui.view.playground.open_files import open_in_editor
    host = open_in_editor(path)
    if obj is None or host is None:
        return

    def _go():
        line = _symbol_def_line(obj)
        if line is None:
            return
        root = getattr(Melty.vis, "root", None)
        open_files = getattr(root, "open_files", None)
        if open_files is not None and open_files.selected_path == str(path):
            open_files.jump_to_line = line
            request_render()
    threading.Thread(target=_go, daemon=True, name="search-symbol-jump").start()


# (module-set signature, hits) - the symbol sweep costs a few ms over every
# loaded module, so hits are memoized on _src_mod_map's CONTENT (its key
# tuple). The map object itself is rebuilt on a 5s TTL with identical content,
# so an identity memo would re-sweep (and wipe its class-tint memo) every 5s
# mid-typing; the content key only rebuilds when a module actually (un)loads.
_symbol_hits_memo = (None, None)

@search_index
def symbol_index():
    """Every function and class defined in a loaded src module — module-level
    defs plus one level of class members (methods, nested classes) — labelled
    `qualname — module`. Activating a hit jumps to the definition in IntelliJ,
    like the editor's Ctrl+B. Covers loaded modules only: the file universe is
    the symbol index's _src_mod_map, so a file nothing imports is invisible."""
    global _symbol_hits_memo
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import _src_mod_map
    mod_map = _src_mod_map()
    mod_sig = tuple(mod_map)
    memo_sig, memo_hits = _symbol_hits_memo
    if memo_sig == mod_sig:
        return memo_hits

    hits = []

    class_tint = _category_tint("Classes")
    fn_tint = _category_tint("Functions")
    _symbol_tint_memo.clear()

    def add(obj, path, stem):
        qn = getattr(obj, "__qualname__", None) or obj.__name__
        if "<" in qn:  # lambdas / <locals> - no stable, jumpable name
            return
        is_class = isinstance(obj, type)
        # Plain ASCII separator: an em dash renders as the missing-glyph "?"
        # in the UI font. A class's tint is its OWN source tint (decorator /
        # override comment / class var) - resolved lazily at render time (the
        # hit's tint is a CALLABLE), since it needs a file read and scan and
        # only displayed rows should pay it.
        tint = ((lambda o=obj, p=path: _class_source_tint(o, p) or class_tint)
                if is_class else fn_tint)
        hits.append(SearchHit(f"{qn} - {stem}", tint,
                              lambda o=obj, p=path: _jump_to_symbol_def(o, p),
                              kind="Classes" if is_class else "Functions",
                              file=path.name))

    for path, mod in mod_map.items():
        mod_name = mod.__name__
        stem = path.stem
        for obj in list(vars(mod).values()):
            if (not isinstance(obj, (types.FunctionType, type))
                    or getattr(obj, "__module__", None) != mod_name):
                continue  # imported name, not a definition in this file
            add(obj, path, stem)
            if isinstance(obj, type):
                for member in list(vars(obj).values()):
                    if isinstance(member, (staticmethod, classmethod)):
                        member = member.__func__
                    if (isinstance(member, (types.FunctionType, type))
                            and getattr(member, "__module__", None) == mod_name):
                        add(member, path, stem)
    _symbol_hits_memo = (mod_sig, hits)
    return hits


# Same content-signature memo as the symbol sweep: keeps the hits list's
# IDENTITY stable across _src_mod_map's 5s TTL rebuilds, which is what lets
# the scorer's per-provider corpus memo hold.
_file_hits_memo = (None, None)


@search_index
def file_index():
    """Every loaded src file, labelled by its src-relative path. Activating a
    hit opens the file in the in-app code editor. Same loaded-module universe
    as the symbol index (_src_mod_map)."""
    global _file_hits_memo
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import _src_mod_map
    mod_map = _src_mod_map()
    mod_sig = tuple(mod_map)
    memo_sig, memo_hits = _file_hits_memo
    if memo_sig == mod_sig:
        return memo_hits
    tint = _category_tint("Files")
    hits = []
    for path in mod_map:
        label = path.as_posix().split("/src/", 1)[-1]
        hits.append(SearchHit(label, tint,
                              lambda p=path: _jump_to_symbol_def(None, p),
                              kind="Files", match=path.name))
    _file_hits_memo = (mod_sig, hits)
    return hits


# --- Text category (full-text trigram search) --------------------------------
# Unlike the other categories, full text can't have a static SearchHit corpus -
# hits are (file, line) pairs that only exist on query. So Text bypasses the
# scorer entirely: a query change kicks a debounced background search against
# the mmapped trigram index (text_index.py - pending files are the source of
# truth there), pre-made SearchHits land on GlobalSearch.text_results, and
# run_global_search injects them into its by_kind buckets. Picks are never
# recorded in the GlobalSearchStore: a "path:line: snippet" label is a
# per-query string, useless as a popularity signal.

def _jump_to_text_hit(path, line):
    from src.lsd.gl_gui.view.playground.open_files import open_in_editor
    open_in_editor(str(path), line)


def _file_meta_tint(path):
    """The tint the user painted on this file (FileMeta — same store the
    editor tabs and folder tree read), or None."""
    col = getattr(getattr(Melty.vis, "root", None), "file_meta_collection", None)
    meta = getattr(col, "file_meta", None) or {}
    entry = meta.get(str(path))
    tint = entry.get("tint") if isinstance(entry, dict) else None
    return tuple(tint[:3]) if tint else None


class _RowSpan:
    """jump_to shim for a one-line search-row buffer: draw_text reads `.start`
    (0-based file line of the buffer's first line) to offset every tree-derived
    wash into buffer space. `path` stays None on purpose — our line numbers are
    already PENDING coordinates, and a real path would run the disk→pending
    delta bridge a second time. `source` mirrors Address's slot (None) for any
    duck-typed reader; the jump-to BAR itself is off (show_jump_bar=False)."""
    __slots__ = ("start", "path", "end", "source")

    def __init__(self, start):
        self.start = start
        self.end = start + 1
        self.path = None
        self.source = None


def _row_code_hosts(path):
    """(code_dict, dict_host) for a search row's file from the SHARED
    code-host cache — the same parse the editor renders with. Creating a host
    is cheap; its whole-file parse runs in the background and rows repaint
    when it lands (notify_on_change on the row's ds). Only called for rows
    actually drawn (≤ max_visible), and hosts are cached across queries.
    Non-Python files get (None, None) — plain syntax colors."""
    if not str(path).endswith(".py"):
        return None, None
    try:
        from src.lsd.gl_gui.view.core_conversion.new_converters import code_hosts_for
        _str_host, dict_host = code_hosts_for(Path(path))
        code_dict = dict_host._held()
        return (code_dict if isinstance(code_dict, dict) else None), dict_host
    except Exception:
        traceback.print_exc()
        return None, None


def _def_wash_u32(tint):
    """The exact block-wash color the text editor paints behind a tinted
    definition's lines: the same _bg_adjust hsv/brightness transform under
    the same Toggles.TextEditor knobs, packed ABGR at def_block_alpha — so a
    washed search row previews precisely what jump-to will show."""
    from src.lsd.gl_gui.view.core_views.text_editor import _bg_adjust
    TE = Toggles.TextEditor
    r, g, b = _bg_adjust(tuple(tint[:3]),
                         (TE.bg_tint_saturation, TE.bg_tint_value,
                          TE.bg_min_brightness, TE.bg_max_brightness))
    a = max(0.0, min(1.0, TE.def_block_alpha))
    return ((int(a * 255) << 24) | (int(b * 255) << 16)
            | (int(g * 255) << 8) | int(r * 255))


def _text_hit(row, show_path=False):
    """A text-index row dict → SearchHit. The ROW is always coloured by the
    FILE's tint (FileMeta — the colour its editor tab wears), so hits group
    visually by file. The definition tint renders INSIDE the line instead,
    the way the editor will show it on jump: code segments carry the
    definition's block wash behind the editor's tokenize()/COLORS syntax
    palette. Locations render as bare `name.py:line` — the full relative
    path only when `show_path` (this result set has another file with the
    same basename). Labels keep the full path either way (stable identity).
    Location prefixes stay in the row (file) tint, dim suffixes in the
    gutter gray."""
    from src.lsd.gl_gui.view.core_views.text_editor import tokenize, COLORS
    kind, rel, line, text = row["kind"], row["rel"], row["line"], row["text"]
    tint = _file_meta_tint(row["path"]) or _TEXT_CAT_TINT
    wash = _def_wash_u32(row["tint"]) if row.get("tint") else None
    loc = rel if show_path else os.path.basename(rel)
    disp = text.strip()

    def _tokens(code):
        try:
            toks = [(tok, COLORS.get(ck, COLORS["default"]))
                    for tok, ck in tokenize(code)]
        except Exception:
            toks = [(code, COLORS["default"])]
        return [(t, c, wash) for t, c in toks]

    code_row = None
    if kind == "file":
        parts = [(os.path.basename(rel), None, None)]
        if show_path:
            parts.append((f"   {rel}", COLORS["line_no"], None))
        label = rel
    elif kind == "symbol":
        # `text` is the RAW def line (indent kept) - draw_text maps the
        # file's tree-derived washes by column, so the code must match.
        code_row = (row["path"], line, "", text[:200], f"{loc}:{line}")
        parts = _tokens(disp[:90]) + [(f"   {loc}:{line}", COLORS["line_no"], None)]
        label = f"{disp[:90]} - {rel}:{line}"
    else:
        code_row = (row["path"], line, f"{loc}:{line}: ", text[:200], "")
        parts = [(f"{loc}:{line}: ", None, None)] + _tokens(disp[:90])
        label = f"{rel}:{line}: {disp[:90]}"
    return SearchHit(label, tint,
                     (lambda p=row["path"], l=line: _jump_to_text_hit(p, l)),
                     kind="Text", match=text, parts=parts, code_row=code_row)


def _kick_text_search(q):
    """Start a background full-text search when the query changed. Debounced by
    a generation counter: the thread sleeps briefly, and a newer keystroke's
    generation abandons the older thread both before and after the search."""
    if q == GlobalSearch._text_query:
        return
    GlobalSearch._text_query = q
    GlobalSearch._text_gen += 1
    gen = GlobalSearch._text_gen
    if len(q) < 3:                      # below the trigram minimum
        GlobalSearch.text_results = []
        return

    def _run():
        time.sleep(0.18)
        if GlobalSearch._text_gen != gen:
            return
        try:
            from src.lsd.gl_gui import text_index
            rows = text_index.search(q)
        except Exception:
            traceback.print_exc()
            rows = []
        if GlobalSearch._text_gen != gen:
            return
        # Bare `name.py:line` locations, full paths only where this result
        # set has two files sharing a basename - then we spell it out.
        rels_by_name = {}
        for row in rows:
            rels_by_name.setdefault(os.path.basename(row["rel"]), set()).add(row["rel"])
        GlobalSearch.text_results = [
            _text_hit(row, show_path=len(rels_by_name[os.path.basename(row["rel"])]) > 1)
            for row in rows]
        _repaint_global_search()

    threading.Thread(target=_run, daemon=True, name="global-text-search").start()


# --- Toggles category -------------------------------------------------------
# A Toggles hit doesn't jump anywhere: it justS the setting through the shared
# Toggles cst-dict host (code_hosts_for(Toggles)) - the exact same write the
# Toggles window's dict pane does, so the value applies live AND round-trips
# into toggles.py through the host's normal chain_out/save. Nothing here knows
# about libcst, saving or hotswap; it's one item-set in the bubbling host.
#
# bool rows flip on activation; int/float/str rows carry a real widget
# (drag_int / drag_float / input_text, via the framework's draw_int /
# draw_float / draw_str) and activation puts the caret in it.

_MISSING = object()
# The only types a setting row can render and write back.
_SETTING_TYPES = (bool, int, float, str)

# path -> (value, deadline_frame) of writes made before the host's last
# parse landed. Drained per frame from draw_main (drain_pending_settings).
_pending_setting_writes = {}


def _setting_paths(cls, prefix=""):
    """Dotted paths of every bool/int/float/str class-var under `cls`,
    descending into its nested setting groups
    (`SearchSettings.ActiveElement.opacity`). Definition order, since vars()
    on a class preserves it."""
    out = []
    for k, v in vars(cls).items():
        if not isinstance(k, str) or k.startswith("_"):
            continue
        if isinstance(v, _SETTING_TYPES):
            out.append(prefix + k)
        elif (isinstance(v, type)
              and getattr(v, "__qualname__", "").startswith(cls.__qualname__ + ".")):
            out.extend(_setting_paths(v, f"{prefix}{k}."))
    return out


def _setting_live(path):
    """The LIVE value of a setting by dotted path (None if it no longer
    exists). Read off the class objects, not the parse, so a row shows its
    real value whether or not the host has parsed yet — and updates the
    instant live_apply_edits lands an edit."""
    obj = Toggles
    for p in path.split("."):
        obj = getattr(obj, p, _MISSING)
        if obj is _MISSING:
            return None
    return obj if isinstance(obj, _SETTING_TYPES) else None


def _setting_group_tint(path):
    """The @defaults tint of the group a setting lives in (Toggles.TextEditor's
    for `TextEditor.*`), so a row is coloured like its pane in the Toggles
    window and tracks a tint edit. Flat vars get the category tint."""
    owner = Toggles
    for p in path.split(".")[:-1]:
        owner = getattr(owner, p, None)
        if not isinstance(owner, type):
            return _TOGGLE_CAT_TINT
    tint = Melty.default_kwargs_by_type.get(owner, {}).get("tint") if owner is not Toggles else None
    return tuple(tint[:3]) if tint else _TOGGLE_CAT_TINT


def _setting_def_line(setting_path, path):
    """The 1-based line of a setting's assignment in toggles.py (None if not
    found): anchor on `class Toggles`, walk each nested group's `class <name>`
    line in path order, then take the first `<attr> =` / `<attr>:` after it.
    Scans Melty.read_code — the PENDING text, i.e. what the editor buffer
    shows — so the jump line matches the buffer even with unsaved edits."""
    src = Melty.read_code(path)
    if not src:
        return None
    lines = src.splitlines()
    parts = setting_path.split(".")
    idx = 0
    for name in [Toggles.__name__] + parts[:-1]:
        pat = re.compile(rf"^\s*class\s+{re.escape(name)}\b")
        idx = next((i + 1 for i in range(idx, len(lines)) if pat.match(lines[i])), None)
        if idx is None:
            return None
    attr_pat = re.compile(rf"^\s*{re.escape(parts[-1])}\s*[:=]")
    return next((i + 1 for i in range(idx, len(lines)) if attr_pat.match(lines[i])), None)


def _jump_to_setting_def(setting_path):
    """Open toggles.py in the in-app editor at a setting's assignment — the
    Shift+Enter jump on a Toggles row. Same open-now/resolve-line-off-thread
    shape as _jump_to_symbol_def (the line scan reads the whole file)."""
    import src.lsd.gl_gui.toggles as _toggles_mod
    from src.lsd.gl_gui.view.playground.open_files import open_in_editor
    path = Path(_toggles_mod.__file__)
    open_in_editor(path)

    def _go():
        line = _setting_def_line(setting_path, path)
        if line is None:
            return
        root = getattr(Melty.vis, "root", None)
        open_files = getattr(root, "open_files", None)
        if open_files is not None and open_files.selected_path == str(path):
            open_files.jump_to_line = line
            request_render()
    threading.Thread(target=_go, daemon=True, name="search-setting-jump").start()


def _toggles_dict_host():
    """The shared Toggles cst-dict host — the SAME pair the Toggles window
    draws from (code_hosts_for caches per class), so a search flip and the
    window's dict pane edit exactly one parse."""
    host = code_hosts_for(Toggles)[1]
    host.register()
    return host


def _write_setting(path, value):
    """Set one setting in the parse. Returns False while the host's background
    parse hasn't landed yet (the caller parks the write and retries).

    The item-set is all the persistence there is: the parse dict bubbles the
    host dirty, which chain_outs dict -> cst -> source and queues the save.
    live_apply_edits is the same immediate preview draw_code_tabs_from_cache
    runs on a dict-pane edit — without it the edit wouldn't reach the live
    class until a recompile."""
    from src.lsd.gl_gui.view.core_conversion.chain_converters import live_apply_edits
    gp = _toggles_dict_host()._held()
    node = gp.get(Toggles.__name__) if isinstance(gp, dict) else None
    parts = path.split(".")
    for p in parts[:-1]:
        node = node.get(p) if isinstance(node, dict) else None
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    node[parts[-1]] = value
    live_apply_edits(Toggles, gp)
    return True


def _repaint_global_search():
    """Repaint the search window's tile after state it draws changed outside
    its body (a row's switch / value) — a cached blit would keep showing the
    old value."""
    if GlobalSearch.window_ds is not None:
        GlobalSearch.window_ds.invalidate()
    request_render()


def _set_setting(path, value):
    """Write a setting, parking it for retry while the host is still parsing
    (picking a row moments after the first keystroke), so an early edit isn't
    silently dropped."""
    if not _write_setting(path, value):
        _pending_setting_writes[path] = (value, Melty.frame_count + 600)
    _repaint_global_search()


def _begin_editing(path):
    """Hand the keyboard to a row's value widget: remember the row, arm the
    one-shot focus grab, and release Melty's text focus so the search box
    stops eating keys. _end_editing hands the box its caret back."""
    GlobalSearch.editing = path
    GlobalSearch._edit_focus = True
    Core.melty.text_focused_ds = None


def _end_editing():
    """Leave value editing and put the caret back in the search box."""
    if GlobalSearch.editing is None:
        return
    GlobalSearch.editing = None
    GlobalSearch._edit_focus = False
    GlobalSearch._focus_requested = True
    _repaint_global_search()


def _activate_setting(path):
    """Activate a setting row: a bool flips in place, anything else opens its
    widget for typing (drag_int / drag_float / input_text)."""
    cur = _setting_live(path)
    if cur is None:
        return
    if isinstance(cur, bool):
        _set_setting(path, not cur)
    else:
        _begin_editing(path)
        _repaint_global_search()


def _value_widget(hit, value, r_edge, ry, row_h, width):
    """Draw a search row's editable value as a REAL framework widget —
    draw_int / draw_float (imgui drag widgets) or draw_str (a text field) —
    right-aligned in the row. Returns (left edge to keep packing leftwards
    from, the widget's rect).

    Placement is by screen position, since the rows themselves are a raw
    draw-list block; the caller parks the imgui cursor afterwards. Drag speed
    scales with the value's magnitude and the bounds are deliberately wide —
    these settings run from 0.07 to 131072, and the render funcs' own default
    min/max would silently clamp them."""
    h = imgui.get_frame_height()
    wx, wy = r_edge - width, ry + (row_h - h) / 2.0
    imgui.set_cursor_screen_pos((wx, wy))
    editing = GlobalSearch.editing == hit.label
    if editing and GlobalSearch._edit_focus:
        # One-shot focus grab. On a drag widget imgui moves keyboard focus into
        # its type-a-value text input, so Enter finishes typing on ints and
        # floats exactly like it does on a text field.
        imgui.set_keyboard_focus_here()
        GlobalSearch._edit_focus = False
    common = dict(name=f"gs_value##{hit.label}", show_header=False,
                  show_name=False, selectable=False, wrap=True, min_width=width)
    if isinstance(value, int):
        changed, new_value = draw_int(value, speed=max(0.1, abs(value) * 0.005),
                                      min_value=-10 ** 9, max_value=10 ** 9,
                                      **common)
    elif isinstance(value, float):
        changed, new_value = draw_float(value, speed=max(0.001, abs(value) * 0.005),
                                        min_value=-1e9, max_value=1e9, **common)
    else:
        changed, new_value = draw_str(str(value), **common)
    if changed and new_value != value:
        hit.set_state(new_value)
    # A change from the KEYBOARD ends editing and hands the caret back to the
    # search box; a drag keeps the row live so it can keep dragging.
    if changed and editing and not imgui.is_mouse_down(0):
        _end_editing()
    return wx - 6, (wx, wy, wx + width, wy + h)


def drain_pending_settings():
    """Retry setting writes parked while the host was still parsing. Called
    every frame from draw_main; free when nothing is parked."""
    if not _pending_setting_writes:
        return
    for path, (value, deadline) in list(_pending_setting_writes.items()):
        if _write_setting(path, value):
            del _pending_setting_writes[path]
            _repaint_global_search()
        elif Melty.frame_count > deadline:
            # The parse never landed (load error / the setting vanished) -
            # forget it rather than retry forever, and say so instead of
            # no-oping.
            del _pending_setting_writes[path]
            from src.lsd.gl_gui.notifications import notify
            notify(f"setting '{path}' never landed — Toggles parse unavailable",
                   tag="global search")
    if _pending_setting_writes:
        request_render()  # keep frames coming until the parse lands


# (path signature, hits) - the paths are re-walked per query (cheap: a list
# looping class attrs) but the HITS list identity is kept stable so the
# scorer's per-provider corpus cache holds. It only rebuilds when a path is
# added/removed/renamed - a value change never lands here, since a row's state
# and tint are live callables.
_toggle_hits_memo = (None, None)


@search_index
def toggle_index():
    """Every bool/int/float/str setting under Toggles — flat class vars plus
    nested groups, labelled by dotted path (`TextEditor.enable_spell_check`).
    Activating a hit edits it in place and leaves the search open."""
    global _toggle_hits_memo
    # Warm the shared host so the parse is ready by the time a row is picked,
    # and register the search window as its consumer - that keeps it alive
    # while the search is open and lets the idle sweep reclaim it after.
    _toggles_dict_host().notify_on_change(GlobalSearch.window_ds)
    paths = _setting_paths(Toggles)
    sig = tuple(paths)
    memo_sig, memo_hits = _toggle_hits_memo
    if memo_sig == sig:
        return memo_hits
    hits = [SearchHit(p, (lambda pp=p: _setting_group_tint(pp)),
                      (lambda pp=p: _activate_setting(pp)), kind="Toggles",
                      match=p.rsplit(".", 1)[-1],
                      state=(lambda pp=p: _setting_live(pp)), keep_open=True,
                      set_state=(lambda v, pp=p: _set_setting(pp, v)),
                      goto=(lambda pp=p: _jump_to_setting_def(pp)),
                      file="toggles.py")
            for p in paths]
    _toggle_hits_memo = (sig, hits)
    return hits


# Derived match corpus per provider: (low_label, low_key, key_charset, hit)
# tuples, computed once per HIT-LIST IDENTITY instead of per keystroke. The
# scorer loops thousands of hit labels with every query change, and the
# repeated .lower()/set() allocations were the high-latency hot spot. A
# provider that returns a fresh list each call (windows - it's small and its
# tints/registrations change live) still rebuilds its corpus each time.
_scorer_corpus_memo = {}


def _provider_corpus(provider):
    hits = provider()
    name = getattr(provider, "__name__", str(provider))
    ent = _scorer_corpus_memo.get(name)
    if ent is not None and ent[0] is hits:
        return ent[1]
    corpus = []
    for h in hits:
        low = h.label.lower()
        key = h.match.lower() if h.match else low
        corpus.append((low, key,
                       frozenset(key[i:i + 2] for i in range(len(key) - 1)), h))
    _scorer_corpus_memo[name] = (hits, corpus)
    return corpus


def _ensure_search_store():
    """The live root's GlobalSearchStore, creating and attaching it when the
    root predates the field (a root built before the attr existed can NEVER
    gain it on its own — __init__ doesn't re-run). Called every frame from
    draw_main so the store exists no matter which load path produced the root
    and whether the search window has rendered yet. Returns the store (None
    only when there is no root at all)."""
    root = getattr(Melty.vis, "root", None)
    if root is None:
        return None
    store = getattr(root, "global_search_store", None)
    if store is None:
        from src.lsd.gl_gui.model.app_model import GlobalSearchStore
        store = root.global_search_store = GlobalSearchStore()
        print("GlobalSearchStore: backfilled onto live root (root predated the field)")
    return store


def _activate_hit(hit, store):
    """Activate a search hit AND record the pick in the persistent
    GlobalSearchStore (AppModel.global_search_store, via the injected vis) —
    the popularity signal that ranks often-used results first and fills the
    empty-query view."""
    if store is None:
        print("GlobalSearchStore: pick NOT recorded — store is None "
              "(Core.melty.vis or root.global_search_store missing)")
    elif hit.kind == "Text":
        pass  # per-query "path:line:col" hits - churn, not popularity
    else:
        try:
            store.record(hit.kind, hit.label)
            print(f"GlobalSearchStore: recorded pick {hit.kind}:{hit.label} -> "
                  f"{store.counts.get(f'{hit.kind}:{hit.label}')}")
        except Exception:
            traceback.print_exc()  # visible in logs; never blocks the activation
    hit.activate()


def _popular_hits(store, limit=60):
    """The most-selected hits over time, best-first with the same per-category
    cap as the scorer — what global search shows while the query box is still
    empty. Only hits that still exist in an index appear (stale store entries
    just never match)."""
    if store is None or not store.counts:
        return []
    counts = store.counts
    ranked = []
    for provider in GLOBAL_SEARCH_INDEXES:
        for _low, _key, _bi, hit in _provider_corpus(provider):
            c = counts.get(f"{hit.kind}:{hit.label}", 0)
            if c > 0:
                ranked.append((c, hit))
    ranked.sort(key=lambda t: (-t[0], t[1].label))
    out, per_kind = [], {}
    for _c, hit in ranked:
        n = per_kind.get(hit.kind, 0)
        if n < limit:
            per_kind[hit.kind] = n + 1
            out.append(hit)
    return out


def global_search_results(q, store=None, limit=60):
    """Query every registered search index and return the hits matching `q`,
    best-first — the global-search result list.

    Matching runs against the hit's `match` key when it has one (a file's
    BASENAME, so partial file names hit and the directory prefix doesn't
    dilute fuzzy tolerance or ranking), while a plain substring of the full
    label still counts (so directory queries like "gl_gui" list files too).
    Ranking tiers: exact substring ahead of fuzzy (typo) hits, PREFIX matches
    ahead of mid-string ones, most-picked (GlobalSearchStore) first within a
    tier, then shorter match keys — so the top hit is the most-used result
    whose name starts with exactly what's been typed, and the limit trims the
    long fuzzy tail. Dedupes by label across providers; the limit applies
    PER CATEGORY — the display is per category, so a category with many
    short-labelled hits must not starve the others out of the list."""
    tol = max(1, len(q) // 4)
    q_bigrams = frozenset(q[i:i + 2] for i in range(len(q) - 1))
    bigram_budget = 2 * tol
    use_fuzzy = len(q) >= 4
    scored = []
    seen = set()
    for provider in GLOBAL_SEARCH_INDEXES:
        for low, key, key_bigrams, hit in _provider_corpus(provider):
            if low in seen:
                continue
            if q in key or q in low:
                dist = 0
            elif use_fuzzy:
                # Bigram lower bound before the O(len(q)-len(key)) edit-
                # distance DP: one edit disturbs at most 2 of the query's
                # bigrams, so more than 2-tol missing bigrams cannot be
                # within tolerance. Prunes *all of the symbol index's
                # thousands of matches at C-set speed (the plain char-set
                # bound was too weak - long labels have most letters).
                if len(q_bigrams - key_bigrams) > bigram_budget:
                    continue
                dist = _fuzzy_substring_distance(q, key)
                if dist > tol:
                    continue
            else:
                continue
            seen.add(low)
            # PREFIX matches - the key is a perfect match up to the current
            # character index - form a tier above other exact-substring hits,
            # so with popularity applied within tiers, the top hit is the
            # most-used result whose name starts with what's been typed.
            prefix = 0 if (key.startswith(q) or low.startswith(q)) else 1
            scored.append((dist, prefix, len(key), hit))
    # Popularity tier: within a (distance, prefix) tier, results picked often
    # over time (GlobalSearchStore counts) rank ahead of never-picked ones.
    counts = store.counts if store is not None else {}
    scored.sort(key=lambda t: (t[0], t[1],
                               -counts.get(f"{t[3].kind}:{t[3].label}", 0),
                               t[2], t[3].label))
    out, per_kind = [], {}
    for _dist, _prefix, _len, hit in scored:
        c = per_kind.get(hit.kind, 0)
        if c < limit:
            per_kind[hit.kind] = c + 1
            out.append(hit)
    return out


def _dismiss_global_search():
    """Close the GlobalSearch window and release the box's text focus — called
    after a result is activated (clicked or Enter), so picking a result also
    dismisses the search. Also clears the query, so the next open starts fresh
    (Esc, which doesn't call this, leaves the query for resuming)."""
    win = Core.melty.find_window("GlobalSearch")
    if win is not None:
        win.closed = True
    GlobalSearch.query = ""
    GlobalSearch._last_query = None
    GlobalSearch.selected = 0
    GlobalSearch.editing = None
    GlobalSearch._edit_focus = False
    Core.melty.clear_focus()
    request_render()


@render_func(use_cache=False, show_bg=False, indent_size=2, disable_scroll=True,
             shadow=False, selectable=False, bg_offset=1)
def draw_collection_as_tabs(input_value, tab_state: TabState = None, draw_state=None, unique=0,
                            excluded=None, included=None, show_excluded=False, show_system=False,
                            folder_type=None):
    """Draws a dict as a tab bar: each inner collection gets its own tab (key = tab
    name, contents via draw_any); all non-collection items are grouped into one
    final "General" tab.

    folder_type: a type or tuple of types that get their own tab, overriding
    the default "any collection" rule — e.g. folder_type=(dict, GeneralParse)
    puts dicts and GeneralParses in tabs while tuples/lists land in General.

    Item filtering matches draw_collection: `excluded` names are hidden,
    `included` names always show (overriding every hide rule), the type's
    __excluded_attrs__ hide unless Toggles.show_excluded, and _underscored_
    keys hide unless show_system. show_excluded=True disables all hiding."""
    if excluded is None:
        excluded = set()
    if included is None:
        included = set()
    excl_attrs = getattr(type(input_value), "__excluded_attrs__", None)

    def _key_visible(key):
        key_str = str(key).split("##")[0]
        if key_str in included:
            return True
        if show_excluded:
            return True
        if (excl_attrs is not None and not Toggles.show_excluded
                and key_str in excl_attrs):
            return False
        if key_str in excluded:
            return False
        if not show_system and (key_str.startswith("_") or key_str.endswith("_")):
            return False
        return True

    if folder_type is not None:
        tab_types = folder_type
    else:
        tab_types = (dict, defaultdict, MutableMapping, types.MappingProxyType, list, tuple, set, deque)
    visible = [(k, v) for k, v in input_value.items() if _key_visible(k)]
    tab_keys = [k for k, v in visible if isinstance(v, tab_types)]
    general = {k: v for k, v in visible if not isinstance(v, tab_types)}

    tabs = [str(k) for k in tab_keys]
    key_by_name = {str(k): k for k in tab_keys}
    if general:
        tabs.append("General")
    if not tabs:
        return False, input_value

    tab_state.selected_tabs = [t for t in tab_state.selected_tabs if t in tabs]
    if not tab_state.selected_tabs:
        tab_state.selected_tabs = [tabs[0]]

    # Tab tints come from each child's render kwargs (via return_extras below).
    # The bar draws before the children, so tints lag by one frame; they're held
    # on tab_state (serialized) so tabs retain their color across sessions.
    if getattr(tab_state, "tab_tints", None) is None:
        tab_state.tab_tints = {}
    tints = [tab_state.tab_tints.get(t) for t in tabs]
    # View icons ride the same one-frame-lag path as tints: each child's
    # resolved `icon` kwarg (the rendered icon, e.g. from # [icon=...] comments)
    # is held on tab_state and prefixed onto its tab label.
    if getattr(tab_state, "tab_icons", None) is None:
        tab_state.tab_icons = {}
    icons = [tab_state.tab_icons.get(t) for t in tabs]
    
    indent_size = 6
    imgui.dummy(0, 5)
    spacing = 8

    # Framework DragDrop: this draw_state IS the drop collection (its
    # input_value is the dict a Reorder/Insert applies to via
    # Melty.dnd_requests + the undo tail - undo included). The tab buttons
    # register as its whole-rect drag items; _dnd_horizontal makes the slot
    # lines vertical gaps between tabs. dnd_keys maps each tab to its
    # (dict key, dict index) - insert indices in the slots are then already
    # dict-space, with non-collection keys being skipped. The synthetic
    # General tab gets None: not draggable and contributes no slot.
    draw_state._dnd_drop_target = True
    draw_state._dnd_horizontal = True
    dict_keys = list(input_value.keys())
    dnd_keys = []
    for t in tabs:
        k = key_by_name.get(t) if not (t == "General" and t not in key_by_name) else None
        dnd_keys.append((k, dict_keys.index(k)) if k is not None else None)

    tab_changed, new_tabs, bar_ds = draw_tab_bar(input_value=tab_state.selected_tabs,
                                                 tab_height=30, show_bg=False, bg_offset=1,
                                                 name=f"tab_bar{unique}", wrap=True,
                                                 collection=tabs, tints=tints, icons=icons, as_toggles=False,
                                                 dnd_collection_ds=draw_state, dnd_keys=dnd_keys,
                                                 return_extras=True)
    if tab_changed:
        tab_state.selected_tabs = new_tabs

    # The bar's view is cached, but the dragged button must be baked through
    # the bar's body at the gesture edges (pickup: pick up the floating window
    # kwargs + bake the placeholder; drop: restore the inline button). Between
    # the edges DragDrop._keep_alive re-registers the floating window on its
    # layer, so no per-frame invalidation is needed.
    dnd_active_here = _drag_drop.DragDrop.active and _drag_drop.DragDrop.source_ds is draw_state
    if dnd_active_here != getattr(draw_state, "_tab_dnd_was_active", False):
        draw_state._tab_dnd_was_active = dnd_active_here
        if bar_ds is not None:
            # bar_ds.invalidate()
            request_render()

    imgui.dummy(0, 2)
    changed = False
    # Rendered content views in visual order, as (dict_index, draw_state) -
    # for the between-content drop slots published below.
    content_stack = []
    # Selected tabs stack vertically in tab-bar order (no columns).
    for tab in tabs:
        if tab not in tab_state.selected_tabs:
            continue
        content_dragged = False
        if tab == "General" and tab not in key_by_name:
            general_changed, new_general, child_ds = draw_any(general, name=f"Tab: General {unique}",
                                                              disable_scroll=False, indent_size=indent_size,
                                                              use_cache=True, return_extras=True)
            if general_changed:
                for k, v in new_general.items():
                    input_value[k] = v
            changed |= general_changed
        else:
            key = key_by_name[tab]
            # With several tabs open, each content view is ALSO a drag item of
            # this dict (key= and _collection_draw_state to make its header a
            # pickup handle from DragDrop.register_item) - dragging a stacked
            # collection reorders the tabs just like dragging a tab button.
            # The item_ds identity check disambiguates the two views sharing
            # (collection, key): only the one actually picked up floats.
            multi = len(tab_state.selected_tabs) > 1
            content_extra = {}
            content_dragged = False
            if multi:
                content_extra["key"] = key
                prev_ds = (getattr(draw_state, "_tab_child_ds", None) or {}).get(tab)
                if (prev_ds is not None and _drag_drop.DragDrop.item_ds is prev_ds
                        and _drag_drop.DragDrop.is_dragged_child(draw_state, key)):
                    content_dragged = True
                    content_extra.update(_drag_drop.DragDrop.dragged_item_kwargs())

            content_extra["use_cache"] = True
            tab_content_changed, value, child_ds = draw_any(input_value[key], name=f"{tab}",
                                                            disable_scroll=False, indent_size=indent_size,
                                                             return_extras=True,
                                                            **content_extra)
            if child_ds is not None:
                # Membership follows multi-select: cleared when only one tab
                # is open so the lone content view stops being a pickup handle.
                child_ds._collection_draw_state = draw_state if multi else None
                if not content_dragged:
                    content_stack.append((dict_keys.index(key), child_ds))
            if content_dragged:
                # The dragged content deferred to a floating window - hold its
                # vertical slot open at its pickup rect (placeholder draws its
                # own item spacing).
                _drag_drop.DragDrop.draw_placeholder(False, spacing,
                                                     style_manager=Core.melty.style_manager,
                                                     draw_bg=draw_bg)
            if tab_content_changed:
                input_value[key] = value
            changed |= tab_content_changed

        if not content_dragged:
            # (the placeholder path already added its own item spacing)
            imgui.dummy(0, spacing)

        if child_ds is not None:
            # Track each tab's content draw_state so deselected tabs can be
            # marked hidden below (they stop rendering but keep stale
            # geometry, which leaked DragDrop drop lines).
            if getattr(draw_state, "_tab_child_ds", None) is None:
                draw_state._tab_child_ds = {}
            draw_state._tab_child_ds[tab] = child_ds
            child_ds._hidden_offscreen = False

        child_tint = child_ds._kwargs.get("tint", None) if child_ds is not None else None
        if child_tint is not None and tab_state.tab_tints.get(tab) != child_tint:
            tab_state.tab_tints[tab] = child_tint
            draw_state.invalidate()

        child_icon = child_ds._kwargs.get("icon", None) if child_ds is not None else None
        if child_icon is not None and tab_state.tab_icons.get(tab) != child_icon:
            tab_state.tab_icons[tab] = child_icon
            draw_state.invalidate()

    # Deselected tabs' content: not collapsed, not closed, but no longer
    # rendered - so nothing downstream knows it's invisible. Stamp
    # _hidden_offscreen (the same flag end_frame uses for spawner-scrolled
    # nested tiles) so DragDrop's slot sweep skips their whole subtrees.
    hidden_reg = getattr(draw_state, "_tab_child_ds", None)
    if hidden_reg:
        for t, t_ds in hidden_reg.items():
            if t not in tab_state.selected_tabs or t not in tabs:
                t_ds._hidden_offscreen = True

    # Publish drop slots BETWEEN the stacked content views (the framework only
    # derives slots from _children, which here are the tab buttons): one
    # horizontal line above each content (insert before its dict key) and one
    # below the last (append after it). Rebuilt from live geometry every
    # render, so they track scroll/reflow; a dragged content is excluded (its
    # home slot is the cancel target). Insert indices are dict-space,
    # same as the bar slots.
    extra_slots = []
    for idx, c_ds in content_stack:
        top, left = c_ds.abs_top, c_ds.abs_left
        if top is None or left is None or not c_ds.width:
            continue
        extra_slots.append((idx, left, left + c_ds.width, top - 2, False))
    if extra_slots:
        last_idx, last_ds = content_stack[-1]
        if last_ds.abs_top is not None and last_ds.abs_left is not None and last_ds.width:
            extra_slots.append((last_idx + 1, last_ds.abs_left,
                                last_ds.abs_left + last_ds.width,
                                last_ds.abs_top + (last_ds.height or 0) + 3, False))
    draw_state._dnd_extra_slots = extra_slots

    return changed, input_value


def search_activate_target(node):
    """The draw_state Ctrl+Enter should 'click' for the current search match
    `node` (melty.search_current_node). When the match is one of a collection's
    keys, that's the child at the current key; for a leaf content match it's the
    node itself."""
    if node is None:
        return None
    key = getattr(node, '_search_current_key', None)
    if key is not None:
        child = node._children.get(key)
        if child is not None:
            return child
    # Views that draw their matches as raw draw-list items (no child draw_states
    # - e.g. the Fast Dock) publish the current match's row rect instead; hand
    # back a geometry shim so the caller's center-of-rect click lands on the
    # row rather than the view's center.
    rect = getattr(node, '_search_current_rect', None)
    if rect is not None:
        return types.SimpleNamespace(abs_left=rect[0], abs_top=rect[1],
                                     width=rect[2], height=rect[3],
                                     _tile_id=node._tile_id)
    return node


@render_func(use_cache=True, is_default_for=SymbolUsage)
def draw_symbol_usage(input_value):
    imgui.text(str(input_value))


@render_func(is_default_for=(dict, MutableMapping, defaultdict, tuple, list, GeneralParse,
                             CallParse, ClassParse, EnumParse, FunctionParse, _BubblingDict, _DeepPath),
             use_cache=True,
             header_same_line=False, show_bg=True, show_instance_vars=False, align_header=False,
             manual_content_height=True, shadow=True, selectable=False, bg_offset=-0.8,
             wrap=False, with_header=draw_header, indent_size=3, searchable=True,
             child_kwargs=None)
def draw_collection(input_value, draw_state, depth, style_manager, meta, icon=None,
                    mode=None, keys=None, get_attr=None, set_attr=None, show_excluded=False,
                    child_kwargs=None, show_bg=False, show_search=False, align_header=False, wrap=False,
                    on_collapse=False, search_text="", return_item=False, close_triggers_delete=False,
                    on_expand=False, show_add_delete=False, show_add_types=None, item_spacing_y=3, show_system=False,
                    included=None, horizontal=False, show_indices=False, excluded=None, annotation=None,
                    drop_tail_height=None, **kwargs):
    """
    Universal collection renderer
    show_add_types={"Display Name": TypeA, ...} draws a second + button in the
    header that instantiates the chosen type (rendered by draw_header; the
    value just rides the kwargs through). Several entries get a chevron
    dropdown to pick from; a single entry binds the + directly with no
    chevron. A bare list of types is accepted and keyed by __name__.
    """

    if excluded is None:
        excluded = set()

    if included is None:
        included = set()

    if child_kwargs is None:
        child_kwargs = {}

    changed = False

    if hasattr(input_value, 'children') and isinstance(input_value.children, (list, dict, defaultdict,
                                                                              types.MappingProxyType, deque)):
        input_value = input_value.children

    if show_bg and draw_state.total_z_offset < 0:
        imgui.dummy(1, 3)
    else:
        imgui.dummy(1, 1)

    # --- configure per collection type ---
    collection = input_value

    # When the value *is* a class (e.g. an @window-registered class drawn
    # directly), its per-attribute `{field}_meta` overrides live on the class
    # itself, not on its metaclass. Use the class as parent_type so get_child_meta
    # can find them; otherwise fall back to the instance's class.
    parent_type = input_value if isinstance(input_value, type) else input_value.__class__
    if isinstance(input_value, (str, int, float, bool, Enum, NoneType)):
        imgui.text("No view for type: " + str(type(input_value)))
        return False, input_value
    if keys is None:
        if isinstance(input_value,
                      (dict, list, tuple, set, defaultdict, MutableMapping, types.MappingProxyType, _DeepPath, deque)):
            apply_change = True
            parent_type = input_value.__class__
            if isinstance(input_value, types.MappingProxyType):
                keys = input_value.keys()
            elif isinstance(input_value, (dict, defaultdict, MutableMapping, types.MappingProxyType)):
                keys = input_value.keys()
                collection = input_value
            else:
                keys = range(len(input_value))
                collection = list(input_value)

        elif hasattr(input_value, "__dict__") and depth < Core.melty.max_depth:
            if hasattr(type(input_value), "__field_defaults__") and hasattr(input_value, 'to_dict'):
                type(input_value).__field_defaults__.update(input_value.__dict__)
                # __field_defaults__ accumulates keys from every instance, so a
                # field deleted from THIS instance lingers there. Skip keys the
                # instance no longer resolves (neither set nor the class default)
                # so deleting a field removes its row instead of leaving a ghost.
                keys = [k for k in type(input_value).__field_defaults__
                        if k in input_value.__dict__ or hasattr(input_value, k)]
            else:
                if input_value is None or input_value.__dict__ is None:
                    return False, input_value
                keys = input_value.__dict__.keys()
            collection = input_value.__dict__
            use_tint = False
            apply_change = True
        else:
            imgui.text("No view for type: " + str(type(input_value)))
            return False, input_value

        keys = list(keys)[:]

    # --- per-key type annotations ---
    # Class-level annotations (walking the MRO) name a type per attribute when
    # rendering an object's __dict__; a typed container annotation
    # (Dict[str, Lora] / List[Lora]) covers every key otherwise. Each child
    # inherits its annotation so its own add button can instantiate the right
    # item type (replaces the old meta.field_type path).
    parent_annotations = {}
    for _klass in reversed(getattr(parent_type, "__mro__", ())):
        _anns = _klass.__dict__.get("__annotations__")
        if _anns:
            parent_annotations.update(_anns)
    item_annotation = annotation_item_type(annotation)

    # --- search (key matching) ---
    # Same dual resolution as the text editor: a forwarded SearchTerm carries
    # the shared cross-view session, or the owner's own session when this
    # collection hosts the find UI. We claim one slot per matching key, in
    # visual order interleaved with the children (claimed below), so the
    # combined next/prev sequence reads top-to-bottom. The current key is
    # latched so incidental repaints don't shift the highlight.
    _search_term = search_text or (draw_state.search_text if draw_state.search_active else "")
    if isinstance(_search_term, SearchTerm):
        search_session = _search_term
    elif draw_state.search_active and draw_state._search_session is not None:
        search_session = draw_state._search_session
    else:
        search_session = None
    search_q = str(_search_term).lower() if (search_session is not None and _search_term) else ""
    search_current_y = None  # screen-Y of the current key's row (for scroll)
    search_current_h = None
    # On a full-search frame (term change / nav) render every row — even ones
    # the off-screen optimization would skip — so the row holding the current
    # match is reached and can scroll into view.
    _search_full_render = search_session is not None and search_session.scroll_to

    # Stash a matcher so the search owner's tree walk (DrawState.descendants /
    # melty.search_walk) can count this collection's key matches without
    # rendering. It counts only this collection's own keys (the whole key list,
    # not just the rows the loop below draws); child collections / text editors
    # are separate tree nodes with their own matchers, so the walk sums them
    # without double-counting. Keys are re-derived lazily on call (only on
    # counting frames), so an unsearched render pays nothing for it.
    def _search_matcher(term, session, _iv=input_value, _keys=keys,
                        _excl=excluded, _se=show_excluded):
        q = str(term).lower()
        if not q:
            return
        mk = _collection_match_keys(_iv, _keys, _excl, _se)
        session.claim(sum(1 for _, k in mk if _fuzzy_key_match(q, k)))

    draw_state._search_matcher = _search_matcher

    # The owner's pre-body walk picked the global-current match and stashed its
    # local index on us (_search_active_local) when one of OUR keys holds it —
    # the same walk that produced the count, so selection and count agree.
    # Resolve that ordinal (over our matching keys, in key order) to the key
    # index the loop should highlight + scroll to. None when the current match
    # lives in a child instead (that child carries its own mark).
    _current_key_idx = None
    if search_q and draw_state._search_active_local is not None:
        _matching = [i for (i, k) in
                     _collection_match_keys(input_value, keys, excluded, show_excluded)
                     if _fuzzy_key_match(search_q, k)]
        if 0 <= draw_state._search_active_local < len(_matching):
            _current_key_idx = _matching[draw_state._search_active_local]

    # --- unified loop ---
    drew_any = False

    start_cursor = imgui.get_cursor_pos()[1]
    rect = Core.melty.get_clip_rect()

    premature_break = False

    Core.melty.collection_index_stack.append(0)
    this_collection = len(Core.melty.collection_index_stack) - 1

    max_items = 5000
    start_index = 0
    end_index = min(len(keys) - 1, max_items)


    scroll_offset = draw_state.scroll_offset
    true_left = draw_state.left - scroll_offset[0]
    true_top = draw_state.top - scroll_offset[1]

    # Remove excluded from keys
    item_to_return = None
    # Keys of children whose close (X) was clicked this frame — collected during the
    # loop and removed from the collection AFTER it (never mutate keys mid-iteration).
    to_delete = set()
    # Whether any row was row-skipped this pass - a skipped pass reconstructs
    # the layout from cached relative_pos/height caches, so its accuracy is only
    # as good as those caches; a skip-free pass measured everything for real.
    rows_skipped = False

    for idx in range(start_index, end_index + 1):
        key = keys[idx]
        relative_pos = imgui.get_cursor_screen_pos()
        relative_pos = (relative_pos[0] - true_left,
                        relative_pos[1] - true_top + item_spacing_y)

        child_draw_state = draw_state._children.get(idx, None)

        # A child currently being drag-and-dropped renders as a floating
        # window (kwargs injected below) - never row-skip it, its stale
        # relative_pos no longer says where it is.
        is_dragged = _drag_drop.DragDrop.is_dragged_child(draw_state, key)

        # ----- off-screen detection -----
        # Is this row scrolled outside the viewport? When not searching, skip it
        # entirely up front (the perf early-out). On a search-counting frame keep
        # `clipped` to decide below: reuse a cached match count (no render) or
        # render to (re)count.
        clipped = False
        if (not horizontal and not is_dragged and child_draw_state is not None
                and child_draw_state.relative_pos is not None
                and not Core.melty.frame_count <= 2
                and (not draw_state.invalid_content_height or imgui.is_mouse_down(0)
                     or imgui.is_mouse_down(1) or imgui.is_mouse_down(2))):
            _spy = true_top + child_draw_state.relative_pos[1] - child_draw_state.header_height
            _bottom = _spy + child_draw_state.height + child_draw_state.header_height
            clipped = (_bottom + child_draw_state.height < rect[1] or _spy > rect[3])

        if clipped and not _search_full_render:
            rows_skipped = True
            imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0],
                                         (true_top + child_draw_state.relative_pos[1] +
                                          child_draw_state.height)))
            continue

        Core.melty.collection_index_stack[this_collection] = idx
        item = None
        if get_attr is None:
            if isinstance(collection, dict) and key not in collection:
                # A declared attr deleted from the instance __dict__ still
                # resolves through the class default - use that instead of
                # degrading to a "Key not found" ghost row.
                if hasattr(input_value, "__dict__") and hasattr(input_value, str(key)):
                    item = getattr(input_value, str(key), None)
                else:
                    imgui.text("Key not found: " + str(key))
                    continue
            elif hasattr(input_value, "__dict__") and hasattr(input_value, str(key)):
                item = getattr(input_value, str(key), None)
            else:
                item = collection[key]
        else:
            try:
                item = get_attr(input_value, key)
            except Exception as e:
                imgui.text(f"Error getting key {key}")

        # visual separator (object extras)
        if key is None and item is None:
            seperator(Core.melty.spacing[1])
            continue
        # apply global skip to all types
        if isinstance(key, (float, Enum, NoneType)):
            key_str = f"{input_value.__class__.__name__}"
        elif isinstance(key, int):
            key_str = f"{key}"
        else:
            key_str = str(key)


        if not show_excluded and hasattr(type(input_value), "__excluded_attrs__"):
            if not Toggles.show_excluded:
                if key_str in type(input_value).__excluded_attrs__ and key_str not in included:
                    continue
        display_name = None

        if key_str in excluded and key_str not in included:
            continue

        if not show_excluded and (not show_system and (key_str.startswith("_") or key_str.endswith("_"))):
            if key_str not in included:
                continue

        # ----- SEARCH (key match) -----
        # Whether this key is the global-current match was decided by the
        # owner's pre-body walk (resolved to _current_key_idx above); we just
        # flag it and record its row Y so the post-loop block scrolls to it.
        key_is_match = bool(search_q) and _fuzzy_key_match(search_q, key_str.lower())
        key_is_current = key_is_match and idx == _current_key_idx
        if key_is_current:
            search_current_y = imgui.get_cursor_screen_pos()[1]

        prev_tint = None
        try:

            y_offset = Core.melty.collection_spacing
            if show_indices:
                display_name = f"{str(idx)}"

            item_kwargs = {
                'type_collection': kwargs.get("real_type", type(input_value)),
                'real_type': type(item),
                'return_extras': True,
                'key': key,
                'on_collapse': on_collapse,
                'on_expand': on_expand,
                'collection': input_value,
                'name': key_str,
                'display_name': display_name,
                'parent_show_add_delete': show_add_delete,
                'show_add_delete': show_add_delete,
                'with_header_end': draw_header_end if show_add_delete else None,
                'annotation': parent_annotations.get(key, item_annotation),
                'y_offset': y_offset,
                'mode': mode,
                'wrap':wrap,
                'search_match': key_is_match,
                'search_current': key_is_current,
            }

            if "content_width" in draw_state._kwargs:
                item_kwargs['content_width'] = draw_state._kwargs['content_width'] - Core.melty.spacing[0] * 2

            # Folders: a dict child of a typed-add collection (show_add_types)
            # inherits the same type choices, so nested folders keep the
            # [+ <type> v] affordance all the way down.
            if show_add_types and isinstance(item, dict):
                item_kwargs['show_add_types'] = show_add_types

            # Per-field `# [tint=...]` comment overrides
            # (__overrides__['__<field>__'] from the parent) are fed by the
            # render_func wrapper from the collection= + key= passed above -
            # see the __overrides__ block in core_render.

            item_kwargs = item_kwargs | child_kwargs
            if is_dragged:
                # Detach the dragged child to a floating closable window,
                # pinned to its pre-pickup size (the wrapper glues the
                # window_pos to the cursor each dispatch).
                item_kwargs.update(_drag_drop.DragDrop.dragged_item_kwargs())
            if isinstance(input_value, (list, tuple, set)) or horizontal:
                item_kwargs['align_header'] = False

            if horizontal and child_draw_state is not None:
                rect = Core.melty.get_clip_rect()
                right_edge = rect[2]
                space_left = right_edge - (imgui.get_cursor_screen_pos()[0] + child_draw_state.width)

                if len(child_draw_state._children) > 0 and child_draw_state.height > 50:
                    imgui.dummy(0, 0)

                elif space_left < 0:
                    imgui.new_line()
                    imgui.dummy(0, item_spacing_y)

            item_return = draw_any(item, **item_kwargs)

            if len(item_return) == 3:
                item_changed, out_val, returned_ds = item_return
            else:
                item_changed, out_val, returned_ds = item_return[0], item_return[1], None

            if returned_ds is not None:
                draw_state._children[idx] = returned_ds
                returned_ds._collection_draw_state = draw_state
                returned_ds.relative_pos = relative_pos
                # Child window closed via its X (closable + closed) -> queue its key
                # for removal from the collection (applied after the loop).
                if returned_ds.closable and returned_ds.closed:
                    if close_triggers_delete:
                        to_delete.add(key)
                if key_is_current:
                    search_current_h = returned_ds.header_height
                if is_dragged:
                    # The child deferred to a floating window and drew nothing
                    # inline — hold its slot open with a placeholder so the
                    # collection layout doesn't shift. Neither the horizontal
                    # branch nor any other math off returned_ds may run here:
                    # returned_ds.abs_* is the floating window glued to the
                    # mouse, so a mid-drag re-render would measure the flow
                    # back of the cursor. The placeholder anchors itself to the
                    # pickup slot (DragDrop.home_rect).
                    _drag_drop.DragDrop.draw_placeholder(horizontal, item_spacing_y,
                                                         style_manager=style_manager,
                                                         draw_bg=draw_bg)
                elif horizontal:
                    imgui.same_line(spacing=0)
                    imgui.set_cursor_screen_pos((returned_ds.abs_left + returned_ds.width, returned_ds.abs_top))
                else:
                    imgui.dummy(0, item_spacing_y)

            if isinstance(out_val, CollectionAction):
                # perform the move; this should mutate the plain dicts you attached
                result = Core.melty.to_apply(out_val)
                item_changed, out_val = False, None

            if set_attr is not None and item_changed:
                try:
                    set_attr(input_value, key, out_val)
                except Exception as e:
                    print(f"Error setting key {key} to value {out_val}: {e}")
            else:
                if "return_item" not in item_kwargs:
                    if item_changed and apply_change and key is not None:
                        if isinstance(input_value, (dict, defaultdict, MutableMapping, types.MappingProxyType)):
                            input_value[key] = out_val
                        elif isinstance(input_value, list):
                            input_value[key] = out_val
                        elif isinstance(input_value, deque):
                            input_value[key] = out_val
                        elif isinstance(input_value, tuple):
                            temp = list(input_value)
                            temp[key] = out_val
                            input_value = parent_type(temp)
                        else:
                            setattr(input_value, key_str, out_val)

            changed |= item_changed
            if item_changed and return_item:
                item_to_return = out_val

        except Exception as e:
            print(f"Error rendering field '{key_str}' of {type(input_value).__name__}: {e}")
            print_stack_trace(exception=e)

        finally:
            if prev_tint is not None:
                style_manager.set_imgui_tint(*prev_tint)

    Core.melty.collection_index_stack.pop()

    # Apply removals for children closed via their X this frame (collected above, so the
    # collection is never mutated mid-iteration). Only dict-like / list collections are
    # safely key-deletable here; tuples/sets/object-__dict__ are left untouched. Sets
    # `changed` so the edit propagates to the owner (e.g. a RenderHost io_function).
    if to_delete:
        print(f"Deleting keys {to_delete} {input_value.__class__.__name__}")
        if isinstance(input_value, (dict, defaultdict, MutableMapping, _BubblingDict)):
            for _k in to_delete:
                print(f"_k in to_delete Deleting key {_k}")
                if _k in input_value:
                    print(f"del input_value[_k found in, deleting")
                    del input_value[_k]
                    changed = True


        elif isinstance(input_value, list):
            for _i in sorted((k for k in to_delete if isinstance(k, int)), reverse=True):
                if 0 <= _i < len(input_value):
                    del input_value[_i]
                    changed = True


    # When navigation just happened, scroll the current key into view.
    # draw_collection disables its own scroll, so _scroll_into_view walks up to
    # the real scroll container. (A current match inside a child is scrolled by
    # that child itself.) _current_key_idx came from the owner's walk.
    if search_session is not None and search_session.scroll_to:
        draw_state._search_current_key = _current_key_idx
        if search_current_y is not None:
            h = search_current_h or imgui.get_text_line_height()
            _scroll_into_view(draw_state, search_current_y, search_current_y + h)

    # Prune stale child slots - indices outside the current key range, left
    # over from deletions/cross-collection edits. Their draw_states keep old
    # geometry that ghost-walks (skip-advance, drop slots) would trip over.
    if draw_state._children:
        for _stale_idx in [i for i in draw_state._children
                           if isinstance(i, int) and i > end_index]:
            del draw_state._children[_stale_idx]

    # Static drop tail for drag-and-drop collections: a fixed strip of empty
    # space below the last row. Its height counts towards the collection's
    # measured height, which pushes the PARENT's "append to folder" slot down
    # - without it a folder ends right at its last child, so that slot and the
    # nested collection's own "append at end" slot (last child bottom + 3) land
    # nearly on top of each other. Also gives an empty collection a droppable
    # body. Always present (NOT drag-conditional, per Lukas) so layout never
    # shifts when a drag begins. Gated to dict/list here - exactly what
    # the DnD system treats as a drop target (see drag_drop._is_target_collection).
    # Height comes live from Toggles.Collection.drop_tail_height (so the Toggles
    # UI edits it globally); a per-call drop_tail_height= kwarg overrides it.
    _drop_tail = (drop_tail_height if drop_tail_height is not None
                  else Toggles.Collection.drop_tail_height)
    if (_drop_tail and not horizontal
            and isinstance(draw_state._raw_input_value, (dict, list))):
        tail_h = int(_drop_tail)
        # Single-line collections (the wrapper same-line's the body BESIDE the
        # header when not multi_line - e.g. a small dict) need the tail to
        # also span the header height, or a bare vertical dummy sits to the
        # right of the header and never grows the box past header height. Add
        # the header height so the tail reaches BELOW the header, giving an
        # empty collection a real droppable body. Multi-line collections
        # already stack the body under the header, so a bare tail is fine.
        if not draw_state.multi_line:
            tail_h += int(draw_state.header_height or 0)
        imgui.dummy(1, tail_h)

    end_pos = imgui.get_cursor_pos()[1]
    content_height = (end_pos - start_cursor)
    imgui.dummy(1, 0)

    # Commit the measurement when it's trustworthy: a skip-free pass measured
    # every row for real (commit even mid-drag - that's what lets a collection
    # update DURING a drag instead of storming after it); a pass with
    # skips reconstructed the layout from caches, so only commit it in the
    # old steady-state conditions (no buttons held).
    measured_fully = not rows_skipped and not premature_break
    if measured_fully or (not imgui.is_mouse_down(0) and not imgui.is_mouse_down(1)
                          and not imgui.is_mouse_down(2) and not premature_break):
        draw_state.content_height = snap_int(content_height)
        draw_state.invalid_content_height = False

    draw_state.premature_break = premature_break
    if return_item:
        if changed:
            return changed, item_to_return
        else:
            return False, input_value

    return changed, input_value  


def main_header(input_value, name, **kwargs):
    imgui.text("Main Header")


@render_func(is_default_for=(property))
def draw_property(input_value: property, draw_state, **kwargs):
    imgui.text_colored(f"Property: {input_value.fget.__name__}", 1.0, 0.5, 0.0, 1.0)
    # value = input_value.fget(input_value)
    # draw_any(value, name="Value", show_bg=True, draw_state=draw_state)



@render_func(is_lens_for=(type), skip_draw=True)
def type_lens(input_value, view_func, child_kwargs, **kwargs):
    changed, value = view_func(**child_kwargs)
    if changed:
        for k, v in value.items():
            if hasattr(input_value, k):
                if k.startswith("_"):
                    continue
                try:
                    setattr(input_value, k, v)
                except Exception as e:
                    pass

    return changed, value


@render_func(show_bg=True, align_header=False, use_cache=True, shadow=False,
             with_header=draw_header)
def draw_type(input_value: type, **kwargs):
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


@render_func(show_bg=True, use_cache=True, selectable=False, header_single_line=False, align_header=False,
             with_header=None, bg_offset=-1, auto_resize=False, temp=True)
def draw_global_search(input_value, vis=None, draw_state=None, max_visible=15, left_mouse_down=False, **kwargs):
    """Renders the GlobalSearch window: the search box plus the matching hits
    from the registered search indexes (GLOBAL_SEARCH_INDEXES). Results are
    recomputed only when the query changes."""
    # The persistent usage store uses AppModel so they serialize with app
    # state. Resolve the studio root from Core.melty.vis (set at
    # Melty.init), same as every other view that needs the root; the vis
    # param stays as an explicit override. _ensure_search_store (called every
    # frame from draw_main) guarantees the attr exists on the live root.
    store = _ensure_search_store()
    # Expose our own window draw_state + honour a focus request from draw_main's
    # Ctrl+Shift+F shortcut (one-shot: grab the box's text focus this frame).
    input_value.window_ds = draw_state
    _focus = input_value._focus_requested
    input_value._focus_requested = False
    # return_extras gives the box's draw_state so we can tell when it holds text
    # focus (and thus when our arrow/enter result-navigation should be live).
    # single_line: Enter is OURS (activate the highlighted hit) - without it the
    # editor also inserts a newline, which used to be invisible when every
    # activation dismissed and cleared the box, and shows up now that toggle
    # rows keep the search visible.
    box = draw_text(input_value.query, name="Search", show_name=False, searchable=False, header_same_line=False, show_bg=False,
                    font=Font.JETBRAINS_MONO_50, request_focus=_focus, is_tree=False, align_header=False, is_search_box=True,
                    single_line=True, return_extras=True)
    changed, new_query = box[0], box[1]
    box_ds = box[2] if len(box) > 2 else None
    if changed:
        input_value.query = new_query

    q = (input_value.query or "").strip().lower()
    if q != input_value._last_query:
        input_value._last_query = q
        # Empty box: show the most-selected hits over time instead of nothing.
        input_value.results = (global_search_results(q, store) if len(q) >= 2
                               else _popular_hits(store))
        input_value.selected = 0  # reset highlight to the top match on a new query
    # Full-text hits arrive async from the trigram index (no-op while q is
    # unchanged); they land on input_value.text_results and repaint us.
    _kick_text_search(q)

    # Group ranked hits by category; only the ACTIVE category's results render
    # (one at a time), picked by the selector row under the box.
    by_kind = {}
    for hit in input_value.results:
        by_kind.setdefault(hit.kind, []).append(hit)
    if len(q) >= 3 and input_value.text_results:
        by_kind["Text"] = list(input_value.text_results)
    cats = _search_cats(by_kind)
    # The All tab's combined rows + parallel group labels, built once per
    # frame: used as its rows in _items_for AND as its side headers.
    all_rows, all_groups = _all_tab_items(by_kind)
    # The active category is STICKY - it never auto-switches, so the chips
    # remember where you put it.
    active = input_value.active_kind
    if active not in cats:
        active = cats[0]
        input_value.active_kind = active

    def _items_for(kind):
        """(rows, is_fallback) for a category: its own hits, or — when it
        matched nothing — EVERY category's hits, grouped under their category
        (best-ranked category first, rank order within each). An empty tab
        showing the results the query did find beats an empty pane, and it's
        display-only: the selection never moves, so the chips still say where
        you are and the next keystroke filters the same way.

        The returned order IS the on-screen order, which is what lets the
        highlight index (input_value.selected) address rows directly."""
        if kind == ALL_CATEGORY:
            return all_rows, False
        own = by_kind.get(kind) or []
        if own or not input_value.results:
            return own, False
        grouped = []
        for k in dict.fromkeys(h.kind for h in input_value.results):
            grouped.extend(by_kind[k])
        return grouped, True

    items, fallback = _items_for(active)
    n_vis = min(len(items), max_visible)
    input_value.selected = (input_value.selected % n_vis) if n_vis else 0

    # While the box holds text focus: Tab / Shift+Tab pick the category (the
    # editor's tab-indent is search-box-gated so the keys are free; arrows
    # would move the caret), Up/Down move the highlight, Enter activates it.
    # GLFW_REPEAT is sparse/absent on Wayland, so held keys are supplemented
    # with imgui's synthesized auto-repeat (the text editor's _REPEATABLE_KEYS
    # trick), skipping keys GLFW already reported this frame.
    if box_ds is not None and Core.melty.text_focused_ds is box_ds:
        io = imgui.get_io()
        keys = list(Core.melty.frame_key_events)
        seen_keys = {k for k, _m in keys}
        rep_mods = ((glfw.MOD_SHIFT if io.key_shift else 0)
                    | (glfw.MOD_CONTROL if io.key_ctrl else 0))
        any_held = False
        for rk in (glfw.KEY_UP, glfw.KEY_DOWN, glfw.KEY_TAB,
                   glfw.KEY_LEFT, glfw.KEY_RIGHT):
            if imgui.is_key_down(rk):
                any_held = True
            if rk not in seen_keys and imgui.is_key_pressed(rk, repeat=True):
                keys.append((rk, rep_mods))
        if any_held:
            request_render()  # keep frames coming so the repeat cadence samples

        # Tab / Shift+Tab and Ctrl+Right / Ctrl+Left both cycle the selectors.
        step = sum(-1 if (m & glfw.MOD_SHIFT) else 1
                   for k, m in keys if k == glfw.KEY_TAB)
        step += sum(1 for k, m in keys
                    if k == glfw.KEY_RIGHT and (m & glfw.MOD_CONTROL))
        step -= sum(1 for k, m in keys
                    if k == glfw.KEY_LEFT and (m & glfw.MOD_CONTROL))
        if step:
            ci = cats.index(active) if active in cats else 0
            active = cats[(ci + step) % len(cats)]
            input_value.active_kind = active
            items, fallback = _items_for(active)
            n_vis = min(len(items), max_visible)
            input_value.selected = 0
            request_render()
        vstep = sum(1 for k, _m in keys if k == glfw.KEY_DOWN) \
            - sum(1 for k, _m in keys if k == glfw.KEY_UP)
        if vstep and n_vis:
            input_value.selected = (input_value.selected + vstep) % n_vis
            request_render()
        _enter_mods = [m for k, m in keys
                       if k in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER)]
        if n_vis and _enter_mods:
            _hit = items[input_value.selected]
            _goto = getattr(_hit, "goto", None)
            if (_enter_mods[0] & glfw.MOD_SHIFT) and _goto is not None:
                # Shift+Enter: jump to the hit's DEFINITION (its action's /
                # toggle's code in toggles.py) instead of activating it -
                # always a jump, so always dismiss.
                _goto()
                _dismiss_global_search()
            else:
                _activate_hit(_hit, store)
                # keep_open hits (toggles) EDIT rather than jump: stay up, with
                # the highlight where it is, so editing flips the same row back
                # and forth.
                if not _hit.keep_open:
                    _dismiss_global_search()

    # ---- raw draw-list rendering (direct-draw style) ----
    # Category chips + result rows are plain rects/text with manual
    # hit-testing in one render_func body: no per-row widget draw_states.
    # While hovered the window re-renders every frame (the _bounding_hovered
    # branch), so hover highlights and clicks resolve here with no row state.
    ROW_H, ROW_GAP = 24.0, 2.0
    # Width of the icon gutter at each row's left - the category (or file)
    # icon sits here, OUTSIDE the row's background rect.
    ICON_COL = 22.0
    CHIP_H, CHIP_GAP, CHIP_PAD = 22.0, 6.0, 8.0
    CHIP_ROW_GAP = 6.0
    MORE_H = 18.0
    # Only the ACTIVE chip gets a background - the rest are plain coloured
    # text, like the tab bars elsewhere. Hover brightens the text instead of
    # showing a bg, so an idle chip never gets one.
    chip_bg_active, chip_text_active = 0.16, 1.357
    chip_text_idle, chip_text_hover = 0.62, 1.0
    row_bg_value, row_bg_hot = 0.045, 0.10
    row_text_value, row_text_hot = 0.9, 1.5
    # The file name at a row's far right (hit.file) - just barely above the
    # row background (bg is 0.045/0.10), so it reads only when looked for.
    # Files rows skip it (label IS the file).
    file_text_value, file_text_hot = 0.18, 0.28
    text_saturation = 0.8
    # Live value, drawn at a row's right edge: a bool gets an on/off switch,
    # everything else a real drag/text widget of this width.
    SW_W, SW_H = 28.0, 14.0
    sw_bg_on, sw_bg_off = 0.42, 0.09
    sw_knob_on, sw_knob_off = 1.5, 0.42
    VAL_W = 108.0
    # Selection: a flat outline, NOT the search glow's gradient - white with a
    # slight blue tint so it reads the same over every category's row tint.
    sel_color = imgui.get_color_u32_rgba(0.88, 0.93, 1.0, 1.0)
    sel_thickness = 1.5

    w = (draw_state.content_width - 10) if draw_state and draw_state.content_width else 200
    sm = Melty.style_manager
    dl = imgui.get_window_draw_list()
    x0, y0 = imgui.get_cursor_screen_pos()
    mx, my = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    ev = left_mouse_down
    click = (ev.x, ev.y) if (ev and hasattr(ev, "x")) else None
    line_h = imgui.get_text_line_height()
    # Fallback mode stacks: the "No <category> Found" hint, a breather, then
    # each borrowed category's own label above its rows.
    HINT_H = line_h + 6.0
    HINT_GAP = 10.0
    # Small font for the category labels. Pushing a font retargets the shared
    # draw list too, so dl.add_text picks it up.
    small_font = (Core.melty.font_mgr.get(Font.JETBRAINS_MONO_13)
                  if Core.melty.font_mgr is not None else None)
    small_line_h = line_h
    if small_font is not None:
        imgui.push_font(small_font)
        small_line_h = imgui.get_text_line_height()
        imgui.pop_font()
    GROUP_H = small_line_h + 7.0

    def _mix(tint, value, factor=0.8, sat=1.0):
        c = tint if (isinstance(tint, tuple) and len(tint) >= 3) else (0.5, 0.5, 0.5)
        col = sm.make_color_rgb(c[0], c[1], c[2], value=value, factor=factor,
                                saturation_scale=sat)
        return imgui.get_color_u32_rgba(col[0], col[1], col[2], 1.0)

    # ---- category chip layout, calculated first so the content dummy can
    # report the right height: chips flow left-to-right and WRAP into further
    # rows when they'd overflow the content width (too many categories for one
    # row now that the window is user-sized). The wrap limit is the window's
    # width - an input, never the measured chip width. ----
    CHIP_VGAP = 4.0
    chip_layout = []  # (kind, label, cx, cy, chip_w)
    cx, cy = x0, y0
    for k in cats:
        cnt = len(all_rows) if k == ALL_CATEGORY else len(by_kind.get(k, ()))
        lbl = f"{k} {cnt}"
        chip_w = imgui.calc_text_size(lbl)[0] + 2 * CHIP_PAD
        if cx > x0 and cx + chip_w > x0 + w:
            cx = x0
            cy += CHIP_H + CHIP_VGAP
        chip_layout.append((k, lbl, cx, cy, chip_w))
        cx += chip_w + CHIP_GAP
    chip_block_h = (cy - y0) + CHIP_H + CHIP_ROW_GAP

    # One dummy reports the full content height so everything draws over it.
    # (Live widgets are real imgui items placed by screen position, so the
    # cursor is parked back here once the rows are done.)
    n_over = len(items) - n_vis
    # Group label lines: the All tab always groups ("Top", then one label per
    # category with 2nd/3rd hits); the fallback mode groups by borrowed kind.
    if active == ALL_CATEGORY:
        row_groups = all_groups
    elif fallback:
        row_groups = [h.kind for h in items]
    else:
        row_groups = None
    n_groups = len(dict.fromkeys(row_groups[:n_vis])) if row_groups else 0
    imgui.dummy(w, chip_block_h + n_vis * (ROW_H + ROW_GAP)
                + (HINT_H + HINT_GAP if fallback else 0) + n_groups * GROUP_H
                + (MORE_H if n_over > 0 else 0))
    after_rows = imgui.get_cursor_screen_pos()

    # ---- category chips: count per category. The ACTIVE one is a filled
    # chip; the rest are bare coloured text (brighter under the cursor).
    # Clicking a chip picks it (empty ones too), same as Tab. ----
    for k, lbl, cx, cy, chip_w in chip_layout:
        is_active = k == active
        hov = hover_ok and cx <= mx <= cx + chip_w and cy <= my <= cy + CHIP_H
        tint = _category_tint(k)
        tx_v = chip_text_active if is_active else (chip_text_hover if hov else chip_text_idle)
        if is_active:
            dl.add_rect_filled(cx, cy, cx + chip_w, cy + CHIP_H,
                               _mix(tint, chip_bg_active), rounding=4.0)
        dl.add_text(cx + CHIP_PAD, cy + (CHIP_H - line_h) / 2.0,
                    _mix(tint, tx_v, sat=text_saturation), lbl)
        if (click is not None and cx <= click[0] <= cx + chip_w
                and cy <= click[1] <= cy + CHIP_H):
            input_value.active_kind = k
            input_value.selected = 0
            request_render()

    # ---- result rows: the active category (or, if it's empty, every
    # category), capped at max_results for render cost. Rows are coloured by
    # the hit's tint (a window row uses the window's real tint; symbols the
    # tint their on-dict parse renders in) and the highlighted row is
    # outlined. ----
    ry = y0 + chip_block_h
    if fallback:
        # The empty result on its own line, in the category's own colour, then
        # a breather before the borrowed categories start.
        dl.add_text(x0 + 8, ry + 2.0,
                    _mix(_category_tint(active), 1.15, sat=text_saturation),
                    f"No {active} Found")
        ry += HINT_H + HINT_GAP
    _counts = store.counts if store is not None else {}
    group_shown = None
    for idx, hit in enumerate(items[:n_vis]):
        # Grouped rows (All tab, fallback view) draw their group once, on its
        # own line above its hits - small and dark in the group's colour
        # ("Top" falls through _category_tint to white).
        if row_groups and row_groups[idx] != group_shown:
            group_shown = row_groups[idx]
            if small_font is not None:
                imgui.push_font(small_font)
            dl.add_text(x0 + 8, ry + 2.0,
                        _mix(_category_tint(group_shown), 0.55, sat=text_saturation),
                        group_shown)
            if small_font is not None:
                imgui.pop_font()
            ry += GROUP_H
        sel = (idx == input_value.selected)
        hov = hover_ok and x0 <= mx <= x0 + w and ry <= my <= ry + ROW_H
        hot = sel or hov
        # A class hit's tint is a lazy resolver (source scan) - call it here,
        # so only displayed rows pay for it (memoized inside).
        tint = hit.tint() if callable(hit.tint) else hit.tint
        # Icon gutter: the hit's own icon (e.g. an action's @function icon) or
        # its category's, drawn OUTSIDE the row's background rect in a fixed
        # column, so every row's rect and label align. (getattr: memoized hits
        # from before a hotswap may predate the SearchHit class.)
        rx = x0 + ICON_COL
        _icon = getattr(hit, "icon", None) or _category_icon(hit.kind)
        if _icon:
            dl.add_text(x0 + 2, ry + (ROW_H - line_h) / 2.0,
                        _mix(tint, row_text_hot if hot else row_text_value,
                             sat=text_saturation), _icon)
        dl.add_rect_filled(rx, ry, x0 + w, ry + ROW_H,
                           _mix(tint, row_bg_hot if hot else row_bg_value), rounding=4.0)
        text_x = rx + 8
        # Text hits carry a pre-coloured render plan (`parts` - the editor's
        # syntax palette, built off-thread with the hit); segments with color
        # None take the row tint, so location prefixes still hover-brighten.
        # A segment's third slot is a definition-tint WASH (the editor's
        # block-wash color, see _def_wash_32): consecutive washed segments
        # paint one continuous rectangle under the code, previewing exactly how
        # the line renders in the editor after jump-to.
        _row_col = _mix(tint, row_text_hot if hot else row_text_value,
                        sat=text_saturation)
        # The hit's file name, drawn at the row's FAR RIGHT (after the value
        # widget / pick count claim their space). File rows skip it - there
        # the label is the file.
        _row_file = None if hit.kind == "Files" else getattr(hit, "file", None)
        # code_row hits render their code through the REAL editor: draw_text
        # with the file's live cst-dict parse and a jump_to line-offset shim,
        # so washes/token colors are pixel-identical to the jump target. The
        # raw label prefix/suffix frames it; a higher-priority click sub
        # below reclaims activation from the editor's own caret sub (the tab
        # close-button pattern). parts serves as the fallback plan.
        _cr = getattr(hit, "code_row", None)
        _cr_drawn = False
        if _cr is not None:
            _cp, _cl, _cpre, _ccode, _csuf = _cr
            _ty = ry + (ROW_H - line_h) / 2.0
            if _cpre:
                dl.add_text(text_x, _ty, _row_col, _cpre)
                text_x += imgui.calc_text_size(_cpre)[0]
            _sw = imgui.calc_text_size(_csuf)[0] if _csuf else 0.0
            _cw = max(60.0, x0 + w - 8 - _sw - (12.0 if _csuf else 0.0) - text_x)
            _cdict, _chost = _row_code_hosts(_cp)
            imgui.set_cursor_screen_pos((text_x, ry))
            try:
                # use_cache=False on purpose: GlobalSearch lives on the
                # always_on_top layer (188), past the layer-64 masking cliff —
                # up there the blit mask's maxamp depths tie and a per-row
                # tile z-fights the window's own composite (the def-tint
                # washes lost the flip, and hover forced live renders).
                # Uncached rows draw live into whatever frame the window
                # renders, so the WINDOW tile captures the full composite and
                # no row-tile depth ever competes at the clamp. ~15 one-line
                # bodies, only on frames the window repaints out.
                _res = draw_text(_ccode, name=f"gs_code_row_{idx}", unique=idx,
                                 show_header=False, show_bg=True, shadow=True,
                                 single_line=True, width=_cw, height=ROW_H,
                                 use_cache=True, code_dict=_cdict,
                                 jump_to=_RowSpan(_cl - 1), is_tree=False,
                                 show_jump_bar=False,
                                 selectable=False, return_extras=True)
                if _chost is not None:
                    # Repaint when the background parse lands (washes pop in).
                    # The WINDOW ds, not the row's: rows are uncached, so even
                    # a window-tile invalidation makes them re-render.
                    _chost.notify_on_change(draw_state)
                _cr_drawn = True
            except Exception:
                traceback.print_exc()
            if _cr_drawn and _csuf:
                dl.add_text(x0 + w - 8 - _sw, _ty,
                            imgui.get_color_u32_rgba(0.52, 0.55, 0.6, 1.0), _csuf)
            if _cr_drawn and draw_state.on_action(
                    "left_mouse_down", view_id=f"gs_row_act_{idx}",
                    rect=(x0, ry, x0 + w, ry + ROW_H),
                    priority_delta=4) is not None:
                input_value.selected = idx
                _activate_hit(hit, store)
                if not hit.keep_open:
                    _dismiss_global_search()
        _parts = getattr(hit, "parts", None)
        if _cr_drawn:
            pass
        elif _parts:
            _ty = ry + (ROW_H - line_h) / 2.0
            for _part in _parts:
                _seg, _col = _part[0], _part[1]
                _wash = _part[2] if len(_part) > 2 else None
                if text_x > x0 + w - 12:
                    break
                _seg_w = imgui.calc_text_size(_seg)[0]
                if _wash is not None:
                    dl.add_rect_filled(text_x, ry + 2.0,
                                       min(text_x + _seg_w, x0 + w - 8),
                                       ry + ROW_H - 2.0, _wash)
                dl.add_text(text_x, _ty, _row_col if _col is None else _col, _seg)
                text_x += _seg_w
        else:
            # The label keeps the full identity/match text; only the DISPLAY
            # drops a trailing " - stem" that would duplicate the file name
            # drawn at the row's far right (symbol labels carry one).
            _main = hit.label
            if _row_file:
                _fstem = os.path.splitext(_row_file)[0]
                if _main.endswith(f" - {_fstem}"):
                    _main = _main[: -len(f" - {_fstem}")]
            dl.add_text(text_x, ry + (ROW_H - line_h) / 2.0, _row_col, _main)
            text_x += imgui.calc_text_size(_main)[0]
        # Right edge: a hit with live state shows it - a bool as a switch drawn
        # here, anything else as a real widget (drag/text) done by _value_widget.
        r_edge = x0 + w - 8
        val_rect = None
        st = hit.state() if hit.state is not None else None
        if isinstance(st, bool):
            sx1, sx0 = r_edge, r_edge - SW_W
            sy = ry + (ROW_H - SW_H) / 2.0
            dl.add_rect_filled(sx0, sy, sx1, sy + SW_H,
                               _mix(tint, sw_bg_on if st else sw_bg_off),
                               rounding=SW_H / 2.0)
            kr = SW_H / 2.0 - 2.0
            dl.add_circle_filled((sx1 - kr - 2.0) if st else (sx0 + kr + 2.0),
                                 sy + SW_H / 2.0, kr,
                                 _mix(tint, sw_knob_on if st else sw_knob_off), 12)
            r_edge = sx0 - 6
        elif st is not None and hit.set_state is not None:
            r_edge, val_rect = _value_widget(hit, st, r_edge, ry, ROW_H, VAL_W)
        # Lifetime pick count, right-aligned and dim - only show above zero.
        cnt_ = _counts.get(f"{hit.kind}:{hit.label}", 0)
        if cnt_ > 0:
            cs = str(cnt_)
            cw = imgui.calc_text_size(cs)[0]
            dl.add_text(r_edge - cw, ry + (ROW_H - line_h) / 2.0,
                        imgui.get_color_u32_rgba(0.62, 0.62, 0.62, 1.0), cs)
            r_edge -= cw + 8
        # File name, right-aligned at whatever far-right space is left -
        # barely brighter than the row background (see file_text_value).
        if _row_file:
            _fw = imgui.calc_text_size(_row_file)[0]
            if text_x + 12.0 <= r_edge - _fw:
                dl.add_text(r_edge - _fw, ry + (ROW_H - line_h) / 2.0,
                            _mix(tint, file_text_hot if hot else file_text_value,
                                 sat=text_saturation), _row_file)
                r_edge -= _fw + 8
        if sel:
            dl.add_rect(rx, ry, x0 + w, ry + ROW_H, sel_color,
                        rounding=4.0, thickness=sel_thickness)
        # A click INSIDE the value widget belongs to the widget (drag or caret),
        # never to the row - activating there would re-open the editor mid-drag.
        if (click is not None and x0 <= click[0] <= x0 + w
                and ry <= click[1] <= ry + ROW_H
                and not (val_rect and val_rect[0] <= click[0] <= val_rect[2]
                         and val_rect[1] <= click[1] <= val_rect[3])):
            input_value.selected = idx
            _activate_hit(hit, store)
            if not hit.keep_open:
                _dismiss_global_search()
        ry += ROW_H + ROW_GAP
    if n_over > 0:
        dl.add_text(x0 + 8, ry + 2.0,
                    imgui.get_color_u32_rgba(0.55, 0.55, 0.55, 1.0), f"+ {n_over} more")
    # Value widgets moved the cursor; put it back where the dummy left it so
    # the enclosing layout is unaffected.
    imgui.set_cursor_screen_pos(after_rows)
    return False, input_value


@window(view_func=draw_global_search, mode=Modes.WINDOW_RESIZABLE, always_on_top=True)
@defaults(tint=(0.15076258778572083, 0.2957677, 0.4697674512863159))
class GlobalSearch:
    query = ""
    window_ds = None  # this window's own draw_state (for the show shortcut)
    _focus_requested = False
    _last_query = None
    results = []  # cached [SearchHit] for the current query
    text_results = []  # async [SearchHit] from the tracy full-text index
    _text_query = None  # last query handed to _kick_text_search
    _text_gen = 0  # generation counter that debounces/cancels text search
    selected = 0  # index (in on-screen order) of the arrow-key highlight
    active_kind = ALL_CATEGORY  # the category whose rows show (Left/Right cycles)
    editing = None  # label of the row whose value widget owns the keyboard
    _edit_focus = False  # one-shot: grab the keyboard on the next show


@render_func()
def class_to_var_dict(input_value: type, changed, draw_state, **kwargs):
    class_vars = {**{k: getattr(input_value, k) for k in vars(input_value)}}
    class_vars["__original__"] = input_value

    return changed, class_vars


@render_func()
def var_dict_to_class(input_value, changed, **kwargs):
    original_class = input_value.get("__original__", None)
    if original_class is None:
        imgui.text_colored("Error: No original class found in dict", 1.0, 0.0, 0.0, 1.0)
        return False, input_value

    if changed:
        for k, v in input_value.items():
            if k.startswith("_"):
                continue
            try:
                imgui.text(f"Setting attribute {k} to value {v} on class {original_class.__name__}")
                setattr(original_class, k, v)
            except Exception as e:
                imgui.text(f"Error setting attribute {k} on class {original_class.__name__}: {e}")

    return False, input_value


some_float = [0.0]
cst_dict = {}
test_code = None
selected_tabs = ["Alpha"]


@render_func(show_bg=True, with_header=draw_header)
def test_columns():
    draw_str("Column 1", name="col1", column=0)
    draw_int(123, name="col2", column=1)
    draw_float(0.5, name="col3", column=2)
    draw_float(0.5, name="test_5", column=5)
    draw_float(0.5, name="test_5_b", column=5)

    for i in range(10):
        draw_float(0.4, name=f"float_{i}", column=2)


@render_func(use_cache=False, show_bg=False, disable_scroll=True, shadow=False, selectable=False)
def draw_with_modes(input_value, modes, tab_state: TabState = None, search_text="", draw_state=None, unique=0):
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




@render_func
def draw_draw_state(input_value, **kwargs):
    pass


@render_func(use_cache=False, shadow=False, show_bg=False, disable_scroll=False, selectable=False)
def run_chain(input_value, chain=None, draw_state=None, route=None,
              s_key_pressed=False, enter_key_pressed=False, unique=None, debug=False, **kwargs):
    """Debug render function: executes a chain step by step with imgui output.

    Shows function name, changed flag, output type, and a value preview
    at each stage.  Color coded: green=changed, gray=cached, yellow=pending.
    """
    if chain is None:
        imgui.text("No chain provided")
        return False, input_value

    value = input_value
    changed = False

    if debug:
        imgui.text(f"Chain: {len(chain)} nodes")
        imgui.text(f"Input: {type(input_value).__name__}")
        imgui.separator()

    cache_tree = draw_state._chain_stack
    cache_tree.begin()

    mode_cache = chain[0][1].get("mode_cache", False) if isinstance(chain[0], dict) else False
    if mode_cache:
        value = cache_tree.step(changed, value)

    to_route = {}

    for i, func in enumerate(chain):
        if isinstance(func, tuple):
            func, func_kwargs = func[0], func[1]
        else:
            func_kwargs = {}

        if debug:
            if not changed:
                name = getattr(func, '__name__', repr(func))
                imgui.text(f"  [{i}] {name} — (no change)")

        func_kwargs['name'] = f"{func.__name__}{i}{kwargs.get('name', f'')}{unique}"
        func_kwargs['shadow'] = False
        func_kwargs['changed'] = changed
        func_kwargs['show_header'] = False
        func_kwargs['s_key_pressed'] = s_key_pressed
        func_kwargs['enter_key_pressed'] = enter_key_pressed
        func_kwargs['draw'] = True
        func_kwargs['real_type'] = type(input_value)
        for arg_name, arg_val in to_route.values():
            func_kwargs[arg_name] = arg_val

        next_cached = cache_tree.peek()
        if isinstance(value, str):
            imgui.text(f"  [{i}] {func.__name__} — str: '{value[:30]}'")

        imgui.begin_group()
        changed, value = func(input_value=value, reference=next_cached, **func_kwargs)
        imgui.end_group()
        #
        # if not changed:
        #     value = None

        if isinstance(value, Pending):
            changed = False
            value = None

        mode_cache = func_kwargs.get('mode_cache', True)
        if mode_cache:
            value = cache_tree.step(changed, value)

        if route is not None:
            if func in route:
                arg_name = route[func]
                to_route[arg_name] = arg_name, value

    cache_tree.end()

    return changed, value


some_test_tensor = torch.randn(3, 3)

# Nested sample data for the recursive dropdown demo.
dropdown_demo_data = {
    "small": 12,
    "medium": 16,
    "large": 24,
    "color": {
        "rgb": {"red": (1.0, 0.0, 0.0), "green": (0.0, 1.0, 0.0)},
        "named": {"steel": "#4682b4", "teal": "#008080"},
    },
    "alignment": ["left", "center", "right"],
}

drop_down_selection = None


@render_func(use_cache=False, show_bg=True, selectable=False, shadow=False, show_name=False,
             show_tint=True, is_tree=False, bg_offset=0, with_header=draw_header)
def draw_main(input_value, vis, search_text="", draw_state=None, **kwargs):
    global test_obj
    global cst_dict
    global test_code
    from src.lsd.gl_gui.view.mode import Mode

    # Guarantee the persistent search store exists on the live root (a root
    # built before the field existed would never gain it on its own).
    _ensure_search_store()

    # Setting edits made from global search before the Goggles code host had
    # parsed - retried here until they land (no-op, nothing is parked).
    drain_pending_settings()

    # Slow-source writes parked during a mouse drag (anywhere's fast/slow
    # split) run their real set_anywhere the frame the button releases.
    flush_deferred_writes()

    # Ctrl+Shift+F: reveal the GlobalSearch window and focus its box. A
    # non_blocking root handler — so it survives a window blocker stacked in
    # front (instead of needing an extreme priority that would consume events
    # from everything else) and doesn't swallow the key from other views.
    if draw_state.on_action("non_blocking_ctrl_shift_f_down", priority_delta=512):
        gs = Core.melty.find_window("GlobalSearch")
        if gs is not None and not gs.closed:
            # Already open: repeated Ctrl+Shift+F cycles the result category
            # (same as Tab inside the box) instead of re-summoning the window.
            cats = _search_cats()
            ci = cats.index(GlobalSearch.active_kind) if GlobalSearch.active_kind in cats else -1
            GlobalSearch.active_kind = cats[(ci + 1) % len(cats)]
            GlobalSearch.selected = 0
            # State changed OUTSIDE the search view's body: repaint its tile
            # (a cached blit would keep showing the old category).
            if GlobalSearch.window_ds is not None:
                GlobalSearch.window_ds.invalidate()
        else:
            gs = Core.melty.open_window("GlobalSearch")
            if gs is not None:
                # Summon the box to just above the cursor so it shows up where
                # you're looking and is ready to type into.
                mx, my = imgui.get_mouse_pos()
                Core.melty.summon_window(gs, mx, my - 65)
            GlobalSearch._focus_requested = True
        request_render()

    # Ctrl+Enter: recompile ALL pending edits - the per-editor Ctrl+Enter in
    # code_file_io was retired in favor of this. Same root-handler pattern as
    # Ctrl+Shift+F above (non_blocking, so it fires even while the view is
    # closed / cache-blitted or a blocker is in front): open + raise the
    # Pending Saves window and run the same recompile its button does.
    if draw_state.on_action("non_blocking_ctrl_enter_down", priority_delta=512):
        # draw_function_live override / blit-cached half: while a lab's body
        # renders, its own BLOCKING ctrl_enter_down (priority_delta=1024)
        # stops the chain before this handler - but per-frame subscriptions
        # lapse in a cached ancestor, so the otherwise idle lab never
        # re-registers and the key lands here. Same BVH re-route as the
        # Ctrl+F fallback below, and the same dual-dispatch rule (the
        # behavior exists in BOTH places; at most one half fires per press):
        # hovering a lab presses its Run button, no Pending Saves reveal.
        mx, my = imgui.get_mouse_pos()
        _lab_ds = None
        _hits = Core.melty.bvh_query(mx, my)
        _front_win = _hits[0].root_window if _hits else None
        for ds in _hits:
            if _front_win is not None and ds.root_window is not _front_win:
                continue
            _fn = getattr(ds, '_view_func', None)
            if (_fn is not None and getattr(inspect.unwrap(_fn), '__name__',
                                            '') == 'draw_function_live'):
                _lab_ds = ds
                break
        if _lab_ds is not None:
            from src.lsd.gl_gui.view.core_views.live_view_views import (
                request_run)
            request_run(_lab_ds)
            request_render()
        else:
            from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
            # One worker drives the button's exact UI lifecycle (reveal, busy
            # spinner, fading check mark + close) - recompile_all_ui is the
            # shared entry the MCP tool uses too. recompile_all itself finds
            # external changes now, so the separate ExternalChanges.recompile_ui
            # call is gone (through the delegate it would recompile TWICE). The
            # worker toasts the summary for the never-opened-window case.
            def _hotkey_recompile():
                from src.lsd.gl_gui.notifications import notify
                notify(PendingSave.recompile_all_ui())
            threading.Thread(target=_hotkey_recompile, daemon=True,
                             name="ctrl_enter_recompile").start()
            request_render()


    # Ctrl+F root fallback: the per-view Ctrl+F (core_render's searchable
    # block) only registers while the view actually RENDERS - a fully
    # cache-blitted window (an idle code/index pane) never registers, so the
    # key would go nowhere. The root always renders: resolve the hovered
    # searchable view via the BVH (parent-most, matching the per-view inverted
    # logic) and activate its search exactly as the per-view path would.
    # non_blocking, so a front view that DID register still gets the event -
    # both then act on the same parent-most view, which is idempotent.
    if draw_state.on_action("non_blocking_ctrl_f_down", priority_delta=512):
        mx, my = imgui.get_mouse_pos()
        target = None
        owns_ctrl_f = False
        _hits = Core.melty.bvh_query(mx, my)
        # Only the hovered window's searchable competes - its nearest enclosing
        # WINDOW, not root_window: nested windows all share a root, so a
        # root-based filter let a different nested window layered BEHIND the
        # hovered one win the parent-most scan (its searchable has a lower
        # z-pos) and open its find bar instead. If the hovered window has no
        # searchable, fall back outward through its ANCESTOR chain (a small
        # floating window over a searchable pane keeps Ctrl+F working) - never
        # to siblings behind, which never outside the hovered window.
        from src.lsd.gl_gui.view.core_views.core_render import (_owning_window,
                                                                _window_descends_from)
        _chain = []
        if _hits:
            _w = _owning_window(_hits[0])
            # Start the chain at the DEEPEST hovered window, not hits[0]'s: a
            # stale z_pos on a nested-window subtree can sort a parent-window
            # hit to the front, which started the chain at the parent and let
            # it steal Ctrl+F from the nested child under the cursor.
            for _h in _hits:
                _hw = _owning_window(_h)
                if _hw is not _w and _window_descends_from(_hw, _w):
                    _w = _hw
            for _ in range(32):
                _chain.append(_w)
                _p = _w.parent_window
                if _p is None or _p is _w:
                    break
                _w = _p
        for _win in _chain:
            for ds in _hits:
                if _owning_window(ds) is not _win:
                    continue
                # A view whose render func declares the inverted_ctrl_f_down
                # event param (e.g. the context menu's Input tab, which routes
                # Ctrl+F to its own filter box) OWNS the key for its subtree -
                # activating a searchable descendant's search bar here would
                # fight that filter.
                _fn = getattr(ds, '_view_func', None)
                if _fn is not None:
                    _code = getattr(inspect.unwrap(_fn), '__code__', None)
                    if _code is not None and 'inverted_ctrl_f_down' in _code.co_varnames[:_code.co_argcount]:
                        owns_ctrl_f = True
                        break
                kw = getattr(ds, '_kwargs', None) or {}
                if not (kw.get('searchable')
                        or getattr(kw.get('render_func'), '_searchable', False)):
                    continue
                if target is None or getattr(ds, 'z_pos', 0) < getattr(target, 'z_pos', 0):
                    target = ds
            if owns_ctrl_f or target is not None:
                break
        _find_box = None
        if target is None and not owns_ctrl_f:
            # No searchable view under the cursor - the press may sit on a
            # floating Find window (it covers its owner's corner, so this is
            # common when reaching for the box). Resolve the owner from the
            # find UI itself: the hovered search box's ancestor header, or the
            # hovered Find window, whose input_value IS the owner draw_state.
            _box = Core.melty.text_focused_ds
            if _box is not None and getattr(_box, 'is_search_box', False):
                _find_box = _box
                node = _box
                while target is None:
                    if getattr(node, 'search_active', False):
                        target = node          # in-header box: nothing above it
                        break
                    p = node._parent
                    if p is node or p is None:
                        rv = getattr(node, '_raw_input_value', None)
                        if isinstance(rv, DrawState) and getattr(rv, 'search_active', False):
                            target = rv        # floating Find window root
                        break
                    node = p
            if target is None and _chain:
                rv = getattr(_chain[0], '_raw_input_value', None)
                if isinstance(rv, DrawState) and getattr(rv, 'search_active', False):
                    target = rv
        if target is not None and not owns_ctrl_f:
            # Prefill the find box from the current text selection - also when
            # the search is already open, replacing the term (same as the
            # per-view path in core_render's searchable block).
            from src.lsd.gl_gui.view.core_views.core_render import _selection_for_search
            _sel = _selection_for_search(target)
            if _sel is not None and _sel != target.search_text:
                target.search_text = _sel
            # Multiple find bars may stay open at once: opening a view's search
            # no longer closes whichever view's search was already open.
            target.search_active = True
            target._search_was_active = False     # find box re-claims focus
            # Keep the open popover (color picker / dropdown) alive: it isn't in
            # target's ancestor closure, so a bare clear_focus(not_this=target)
            # closes it on every Ctrl+F. Popovers close on outside click /
            # Esc / pick only.
            Core.melty.clear_focus(not_this=(target, Melty.popover_focused_ds))
            Core.melty.focused_ds = target
            Core.melty.cache.invalidate_up(target._tile_id, force=True, max_depth=12)
            # The floating Find window is its own tile-root, so the owner
            # invalidation above doesn't reach it; when the press was resolved
            # through the box, repaint it directly (select-all shows this
            # frame instead of waiting on the owner's focus-repaint).
            if _find_box is not None:
                Core.melty.cache.invalidate_up(_find_box._tile_id, force=True)
            request_render()

    if draw_state.on_action("non_blocking_ctrl_z_down"):
        UndoManager.undo()

    # Redo: Ctrl+Shift+Z (mac/linux convention) or Ctrl+Y (Windows convention).
    if (draw_state.on_action("non_blocking_ctrl_shift_z_down")
            or draw_state.on_action("non_blocking_ctrl_y_down")):
        UndoManager.redo()

    # Navigation stack (tab switches / jump-tos / window open/close): its own
    # timeline on Ctrl+Shift+arrows. Suppressed while text is focused -
    # Ctrl+Shift+Left/Right is extend-selection-by-word in the editor.
    if Core.melty.text_focused_ds is None:
        if draw_state.on_action("non_blocking_ctrl_shift_left_arrow_down"):
            NavUndo.undo()
        if draw_state.on_action("non_blocking_ctrl_shift_right_arrow_down"):
            NavUndo.redo()

    # Esc dismisses the GlobalSearch window - and its ActionRunner popup -
    # while open. Handled here on the root (always hover-eligible) rather than
    # on the windows themselves, so it works no matter where the cursor is.
    # Non_blocking so the front window's blocker doesn't eat it; only
    # subscribes while one of them is open, so it doesn't swallow Esc in a
    # per-view context otherwise.
    _gs = Core.melty.find_window("GlobalSearch")
    _ar = Core.melty.find_window("ActionRunner")
    _gs_open = _gs is not None and not _gs.closed
    _ar_open = _ar is not None and not _ar.closed
    if ((_gs_open or _ar_open)
            and draw_state.on_action("non_blocking_escape_key_down_inverted", priority_delta=512)):
        if _ar_open:
            # The runner is the topmost of the two (activating an Actions hit
            # dismissed the search), so Esc peels it first; if the search is
            # somehow open too, the next Esc handles it.
            from src.lsd.gl_gui.view.playground.actions_playground import _close_action_runner
            _close_action_runner()
        elif GlobalSearch.editing is not None:
            # Editing a term: Esc leaves the widget (imgui reverts a temp text
            # input on its own), and the NEXT Esc closes the search.
            _end_editing()
        else:
            _gs.closed = True
            Core.melty.text_focused_ds = None
            Core.melty.focused_ds = None

            request_render()

    # draw_any(Core.melty.registered_windows, name="Dock", with_header=draw_header,
    #          mode=(Mode.WINDOWS_SORTED, Mode.WINDOW))

    # Fast Dock: same functionality as the Dock, but the rows are raw tile-list
    # rendering inside one render_func (see fast_dock.py). The sync call runs
    # every frame after this always-rendering root so external open/close/tint
    # changes update the cached tile. Hidden in presentation mode (windows
    # stay visible through the global search); the sync's signature check
    # catches up on whatever changed while hidden.
    if not Toggles.presentation_mode:
        from src.lsd.gl_gui.view.core_views.fast_dock import draw_fast_dock, fast_dock_sync
        fast_dock_sync()
        draw_fast_dock(Core.melty.registered_windows, name="Fast Dock", with_header=draw_header,
                       mode=Mode.WINDOW, bg_offset=-3)

    # (Multi-split overlays moved to Melty.end_frame - drawn from this
    # root they ran BEFORE the dragged window's position update and trailed
    # window drags by one frame.)

    for window_cls, stored_kwargs in Core.melty.annotated_window_classes.values():

        # Copy: the stored dict is the @window decorator kwargs and persists
        # across frames. Popping view_func out of it would consume the override
        # after the first frame, so later frames fall back to draw_with_modes.
        if hasattr(window_cls, "__name__"):
            name = f"{window_cls.__name__}##@window"
        else:
            name = kwargs.get("name", f"Unnamed {window_cls.__class__.__name__}")

        kwargs = dict(stored_kwargs)
        kwargs.setdefault('show_bg', True)
        kwargs.setdefault('name', name)

        is_render_func = hasattr(window_cls, "__render_func__")
        if is_render_func:
            kwargs.setdefault('mode', Mode.MODE_WINDOW)
            kwargs.setdefault("disable_scroll", True)
            window_cls(**kwargs)
        else:
            kwargs['disable_scroll'] = True
            kwargs.setdefault('mode', (Mode.NEW_CODE, Mode.MODE_WINDOW))
            window_func = kwargs.pop("view_func", code_file_io)
            window_func(window_cls, **kwargs)


    from src.lsd.gl_gui.model.app_model import TensorView
    draw_any(TensorView, name="Tensorview", mode=(Mode.WINDOW))
    #
    # draw_any(filesystem_proxy, name="Filesystem", disable_scroll=False, mode=Mode.WINDOW)
    # draw_any([screenshots], name="Screenshots", mode=Mode.WINDOW,
    #             child_kwargs={"child_kwargs":{"auto_resize": True}, "shadow":False,
    #                           "show_name":False, "show_header":False, "show_bg":False, "horizontal":True})
    #
    # global drop_down_selection
    # changed, selection = draw_dropdown(drop_down_selection, collection=dropdown_demo_data,
    #                                    name="Dropdown Demo", mode=Mode.WINDOW, tint=(0.180984, 0.2, 0.2))
    #
    # if changed:
    #     drop_down_selection = selection
    #     print("Drop down change", repr(selection))
    #
    draw_collection(vis.root.lora_collection, name="Loras", mode=Mode.WINDOW)
    draw_any(vis.root.lora_collection, name="Loras Alt View", mode=Mode.WINDOW)
    # draw_any(vis.root.lora_collection.loras, name="Loras View Three", child_kwargs={
    #     'is_tree': True, 'expanded': False, 'show_add_delete': False}, mode=Mode.WINDOW)

    # normalized_sub_mask, _, _ = Melty.filter.normalize(Melty.cache._mask_tex)
    # draw_texture(normalized_sub_mask, show_bg=True, max_contrast=30, jet=True,
    #             max_brightness=30, name="mask_tex", live=True, mode=Mode.WINDOW)
    draw_any(Core.melty.cache.snapshot_tex, show_bg=True, name="Viewport", live=True, mode=Mode.WINDOW)

    # The normalize is a FULL-SCREEN GPU min-max reduction (~6-8ms CPU + real
    # GPU fill per frame). Only run it while the debug window is actually open
    # - it was burning that every frame feeding a CLOSED window. The window ds
    # is looked up once and cached; a just-reopened window shows the reduce pass
    # from its last frame (one frame of blank).
    #
    # KEEP THE DATA'S TYPE STABLE while gated off. draw_any routes by type, and
    # the cache/draw_state key hashes on the func's name. passing it None
    # would route to draw_none, i.e. a SECOND window was named "full_mask_tex"
    # with its own `closed` flag. Closing either one then just handed the name to
    # the other, and on app start (where a closed window is invisible, see below)
    # the texture window popped open every launch. Passing the un-normalized
    # texture id keeps the route on draw_texture: one window, one closed flag.
    global _full_mask_win_ds
    try:
        _full_mask_win_ds
    except NameError:
        _full_mask_win_ds = None
    if _full_mask_win_ds is None or getattr(_full_mask_win_ds, 'name', None) != 'full_mask_tex':
        # find_window, not cache.key_to_draw_state: a closed window returns from
        # its renderer before it ever registers a tile, so the cache table won't
        # see it and the gate would never latch (it would normalize every frame
        # of a session that starts with the window closed). The ManagedWindow
        # registration survives closing.
        _full_mask_win_ds = (Core.melty.find_window("full_mask_tex")
                             or next((d for d in Core.melty.cache.key_to_draw_state.values()
                                      if getattr(d, 'name', None) == 'full_mask_tex'), None))
    if _full_mask_win_ds is None or not getattr(_full_mask_win_ds, 'closed', False):
        normalized_sub_mask, _, _ = Core.melty.filter.normalize(Core.melty.cache._full_mask_tex)
    else:
        normalized_sub_mask = numpy.uint32(Core.melty.cache._full_mask_tex or 0)


    draw_any(normalized_sub_mask, show_bg=True, max_contrast=30, jet=True,
             max_brightness=30, name="full_mask_tex", live=True, mode=Mode.WINDOW)

    mouse_pos = imgui.get_mouse_pos()
    ds_under_mouse = Core.melty.bvh_query(mouse_pos[0], mouse_pos[1])
    ds_names = [ds.name for ds in ds_under_mouse]
    draw_any(ds_names, name="Draw State under mouse", show_bg=True, wrap=True, use_cache=True, mode=Mode.WINDOW,
             live=True)

    # Self-registering RenderHost objects (view/core_conversion/render_host.py): each
    # is a stateful wrapper that draws into its own window. Snapshot the values - a
    # host may register/remove during render (re-entrant mutation). Skipped entirely
    # while any mouse button is held (click or drag) or a view is being scrolled
    # (Melty.on_scroll - which lingers a few frames past the last wheel event,
    # bridging the gaps in a trackpad's event stream) - host draws are deferrable
    # work that would otherwise eat into drag/scroll frames; they catch up after.
    any_mouse_held = (imgui.is_mouse_down(0) or imgui.is_mouse_down(1)
                      or imgui.is_mouse_down(2))
    if not any_mouse_held and not Melty.on_scroll:
        for h_idx, host in enumerate(list(Core.melty.render_hosts.values())):
            # Event-driven pump: an entirely hidden cache host skips its draw
            # polling (draw_needed - edit flags / upstream identity change /
            # tile invalidation / slow heartbeat). An open code tab has
            # ~20 code-cache pairs and each polled draw was ~0.1ms of wrapper
            # overhead for nothing - this is what dropped drags to ~70fps.
            # Visible hosts always draw.
            if not host.draw_needed():
                continue
            host.draw()

    display(len(Core.melty.render_hosts), tag="Render Hosts Count")


@render_func
def test_widget(input_value, name, unique, **kwargs):
    imgui.text("Test Widget")
    draw_text("Editable Text", name="editable_text", show_bg=True)


source = "x = foo(val=1)\nprint(x)\nsome_list=[0, 1, 2, 3]\n"
module = cst.parse_module(source)
proxy = cst_wrap(module)
name_edits = {}
code_export_str = "Test"



# Main draw function, called by the GUI framework

@live
class TestObj:
    def __init__(self):
        self.test_val = 0.0
        self.test_list = [1, 2, 3, 4, 5]


test_obj = TestObj()


def draw_melty_windows(vis):
    flags = (imgui.WINDOW_NO_BACKGROUND | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE |
             imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR | imgui.WINDOW_NO_NAV_FOCUS |
             imgui.WINDOW_NO_BRING_TO_FRONT_ON_FOCUS | imgui.WINDOW_NO_NAV_INPUTS | imgui.WINDOW_NO_NAV |
             imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_SAVED_SETTINGS)

    # style.frame_padding = (4, 2)


    imgui.set_next_window_position(0, 0)
    # Fill the entire screen
    fb_w, fb_h = map(int, imgui.get_io().display_size)

    imgui.set_next_window_size(fb_w, fb_h)
    title = "main##window_melty"
    opened, _ = begin(title, closable=False, flags=flags)

    Core.melty.imgui_main_window_hovered = imgui.is_window_hovered()

    Core.melty.begin_frame()

    # imgui.invisible_button("window_blocker", width=fb_w, height=fb_h)
    imgui.set_cursor_screen_pos((0, 0))
    imgui.set_item_allow_overlap()

    draw_list = imgui.get_window_draw_list()
    draw_list.channels_split(Core.melty.max_depth)
    Core.melty.channels_split = True
    Core.melty.window_stack.append((title, True))

    draw_main(name="Main Window", vis=vis, width=fb_w, height=fb_h)

    from src.lsd.gl_gui.applet.test_applet import render_app
    render_app()

    Core.melty.end_frame()

    # End frame ###############
    Core.melty.window_stack.pop()
    draw_list.channels_merge()
    Core.melty.channels_split = False

    end()


@render_func(is_default_for=PendingTexture, use_cache=True, wrap=True, z_offset=1, selectable=False,
             show_bg=True, auto_resize=True, with_header=draw_header)
def draw_pending_texture(input_value: PendingTexture, draw_state):
    if input_value.texture_id is None:
        imgui.text(f"Uploading... {id(input_value)}")
        return False, input_value

    max_size = 300
    if input_value.tex_width > input_value.tex_height:
        width = max_size
        height = int(max_size * input_value.tex_height / input_value.tex_width)
    else:
        height = max_size
        width = int(max_size * input_value.tex_width / input_value.tex_height)

    return_val = draw_texture(input_value.texture_id, initial={"width":width, "height":height},
                              name=f"{draw_state.id}_inner", auto_resize=False,
                              show_header=False, use_cache=True, wrap=False, tint=(0.11, 0.29, 0.52))

    return return_val


@render_func(is_default_for=numpy.uint32, show_bg=True, use_cache=False, show_add_delete=False, z_offset=2,
             fill_height=True, selectable=True,
             indent_size=0, min_width=35, min_height=35, wrap=False, disable_scroll=True,
             zoom_speed=0.3, with_header=draw_header, manual_content_height=True)
def draw_texture(input_value: numpy.uint32, hovered, scroll_y_changed, middle_mouse_drag, right_mouse_drag,
                 zoom_state: ZoomState, zoom_speed, header_height=0, min_zoom=0.1,
                 max_zoom=50.0, style_manager=None, max_brightness=5.0, max_contrast=5.0,
                 draw_state=None, jet=False, **kwargs):
    original_id = input_value
    texture_id = input_value
    imgui.dummy(draw_state.width, draw_state.height - 20)

    # Ensure we have valid state if this is the first run
    if not hasattr(zoom_state, 'zoom'):
        zoom_state.zoom = 1.0
        zoom_state.center_u = 0.5
        zoom_state.center_v = 0.5

    # Check if opengl texture ID is valid
    if not gl.glIsTexture(texture_id):
        imgui.text(f"Error: {texture_id} is not a valid texture")
        return False, input_value

    # 1. Query Texture Properties
    original_texture = gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D)
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture_id)

    width = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_WIDTH)
    height = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_HEIGHT)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    if width > 16384 or height > 16384:
        imgui.text(f"Error: Texture size {width}x{height} exceeds maximum supported size.")
        return False, input_value

    if width == 0 or height == 0:
        return False, input_value

    # 2. Canvas Setup (Fill available space)
    view_width = max(1, draw_state.width)
    view_height = max(1, draw_state.height)

    # 3. Calculate Aspect Ratio Corrections
    tex_aspect = width / height
    view_aspect = view_width / view_height

    # Calculate the visible UV width/height based on zoom and aspect ratio.
    if view_aspect > tex_aspect:
        # View is wider: Fit to Height
        uv_height_size = 1.0 / zoom_state.zoom
        uv_width_size = uv_height_size * (view_aspect / tex_aspect)
    else:
        # View is taller: Fit to Width
        uv_width_size = 1.0 / zoom_state.zoom
        uv_height_size = uv_width_size * (tex_aspect / view_aspect)

    mixed_color = (1, 1, 1, 1)
    highlight_color = (1, 1, 1, 1)

    if style_manager is not None:
        mixed_color = style_manager.make_color_rgb(*mixed_color[:3],
                                                   value=0.3,
                                                   factor=0.9,
                                                   saturation_scale=1.0,
                                                   alpha=1.0)
        highlight_color = style_manager.make_color_rgb(*mixed_color[:3],
                                                       value=1.0,
                                                       factor=0.9,
                                                       saturation_scale=1.0,
                                                       alpha=1.0)
    io = imgui.get_io()
    overlay: _DrawList = imgui.get_overlay_draw_list()

    if right_mouse_drag:
        b_str = f"{zoom_state.brightness:.3f}"
        overlay.add_text(right_mouse_drag.x, right_mouse_drag.y - 30,
                         col=imgui.get_color_u32_rgba(*highlight_color[:3], 1),
                         text=f"brightness:{zoom_state.brightness:.3}\ncontrast:{zoom_state.contrast:.3}")

        if io.key_shift:
            if io.key_ctrl:
                zoom_state.hue += right_mouse_drag.dx * 0.001
                zoom_state.saturation -= right_mouse_drag.dy * 0.001
            else:
                zoom_state.brightness += right_mouse_drag.dx * 0.001
                zoom_state.contrast -= right_mouse_drag.dy * 0.001
        else:
            if io.key_ctrl:
                zoom_state.hue += right_mouse_drag.dx * 0.005
                zoom_state.saturation -= right_mouse_drag.dy * 0.005
            else:
                zoom_state.brightness += right_mouse_drag.dx * 0.005
                zoom_state.contrast -= right_mouse_drag.dy * 0.005

        # zoom_state.brightness = max(0.0, min(max_brightness, zoom_state.brightness))
        # zoom_state.contrast = max(0.0, min(max_contrast, zoom_state.contrast))

    if jet:
        texture_id = Core.melty.filter.brightness_contrast(
            input_value,
            brightness=zoom_state.brightness,
            contrast=zoom_state.contrast
        )

        # texture_id = Core.melty.filter.swirl(
        #     input_value,
        #     radius=zoom_state.brightness,
        #     angle=zoom_state.contrast
        #
        # )
        texture_id = Core.melty.filter.jet(texture_id, offset=zoom_state.hue)
    else:
        texture_id = Core.melty.filter.brightness_contrast(
            input_value,
            brightness=zoom_state.brightness,
            contrast=zoom_state.contrast
        )

        # texture_id = Core.melty.filter.swirl(
        #     input_value,
        #     radius=zoom_state.brightness,
        #     angle=zoom_state.contrast
        #
        # )
        texture_id = Core.melty.filter.hue_saturation(
            texture_id,
            saturation=(zoom_state.saturation),
            hue_shift=(zoom_state.hue),
        )

    # texture_id = Core.melty.filter.swirl(
    #     input_value,
    #     radius=1.0,
    #     angle=(zoom_state.brightness * 5),
    # )
    # texture_id = Core.melty.filter.swirl(
    #     texture_id,
    #     angle=zoom_state.brightness,
    #     radius=zoom_state.contrast
    # )

    p_min = (draw_state.abs_left + 2, draw_state.abs_top + 2)
    p_max = (draw_state.abs_left + draw_state.width, draw_state.abs_top + draw_state.height - 2)
    p_min_x, p_min_y = p_min[0], p_min[1]

    scroll_delta = 0
    if scroll_y_changed is not None:
        scroll_delta = scroll_y_changed.value

    # --- Logic: Zoom and Pan ---

    zoom_delta = 0.0

    # 4a. Handle Zoom Triggers (Scroll & Keyboard)

    # Keyboard Shortcuts (1, 2, 3, 4)
    forced_zoom = -1.0
    key_1 = 49
    numpad_key_1 = 321
    if hovered:
        if imgui.is_key_pressed(key_1) or imgui.is_key_pressed(numpad_key_1):  # Key '1'
            forced_zoom = 1.0
            # Reset Pan to Center
            zoom_state.center_u = 0.5
            zoom_state.center_v = 0.5
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0
        elif imgui.is_key_pressed(50):  # Key '2'
            forced_zoom = 0.5
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0
        elif imgui.is_key_pressed(51):  # Key '3'
            forced_zoom = 0.25
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0
        elif imgui.is_key_pressed(52):  # Key '4'
            forced_zoom = 0.125
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0

    if forced_zoom > 0:
        zoom_state.zoom = forced_zoom
        # Recalculate uv sizes immediately for consistent bounding this frame
        if view_aspect > tex_aspect:
            uv_height_size = 1.0 / zoom_state.zoom
            uv_width_size = uv_height_size * (view_aspect / tex_aspect)
        else:
            uv_width_size = 1.0 / zoom_state.zoom
            uv_height_size = uv_width_size * (tex_aspect / view_aspect)

    # Scroll Logic
    if scroll_delta != 0:
        if io.key_shift:
            zoom_delta = scroll_delta * zoom_speed * 0.3
        else:
            zoom_delta = scroll_delta * zoom_speed
    elif middle_mouse_drag and middle_mouse_drag.modifiers == glfw.MOD_CONTROL:
        zoom_delta = io.mouse_delta.y * -0.008

    # 4b. Handle Pan (Middle Click Drag)
    if middle_mouse_drag and middle_mouse_drag.modifiers != glfw.MOD_CONTROL:
        u_scale = uv_width_size / view_width
        v_scale = uv_height_size / view_height
        if middle_mouse_drag.modifiers == glfw.MOD_SHIFT:
            zoom_state.center_u -= middle_mouse_drag.dx * u_scale * 0.5
            zoom_state.center_v += middle_mouse_drag.dy * v_scale * 0.5
        else:
            zoom_state.center_u -= middle_mouse_drag.dx * u_scale
            zoom_state.center_v += middle_mouse_drag.dy * v_scale

    # 4c. Apply Zoom Logic (Zoom to Cursor)
    if zoom_delta != 0.0:
        zoom_factor = 1.0 + zoom_delta
        new_zoom = max(min_zoom, min(zoom_state.zoom * zoom_factor, max_zoom))

        if new_zoom != zoom_state.zoom:
            mouse_pos = imgui.get_mouse_pos()

            if io.key_ctrl:
                mouse_u_ratio, mouse_v_ratio = (0.5, 0.5)
            else:
                mouse_u_ratio = (mouse_pos[0] - p_min_x) / view_width
                mouse_v_ratio = (mouse_pos[1] - p_min_y) / view_height

            curr_uv_w = uv_width_size
            curr_uv_h = uv_height_size

            # Recalculate NEW UV dimensions
            if view_aspect > tex_aspect:
                new_uv_h = 1.0 / new_zoom
                new_uv_w = new_uv_h * (view_aspect / tex_aspect)
            else:
                new_uv_w = 1.0 / new_zoom
                new_uv_h = new_uv_w * (tex_aspect / view_aspect)

            diff_w = curr_uv_w - new_uv_w
            diff_h = curr_uv_h - new_uv_h

            zoom_state.center_u += diff_w * (mouse_u_ratio - 0.5)
            zoom_state.center_v += diff_h * (0.5 - mouse_v_ratio)

            zoom_state.zoom = new_zoom

            # Update these for Step 5
            uv_width_size = new_uv_w
            uv_height_size = new_uv_h

    # 5. Calculate Final UVs and Clamp to Bounds
    half_uv_w = uv_width_size * 0.5
    half_uv_h = uv_height_size * 0.5

    # --- Bounding Logic Start ---
    margin_px = 20.0

    pixel_u = uv_width_size / view_width
    pixel_v = uv_height_size / view_height
    margin_u = margin_px * pixel_u
    margin_v = margin_px * pixel_v

    min_u = -half_uv_w + margin_u
    max_u = 1.0 + half_uv_w - margin_u

    if min_u > max_u:
        zoom_state.center_u = 0.5
    else:
        zoom_state.center_u = max(min_u, min(zoom_state.center_u, max_u))

    min_v = -half_uv_h + margin_v
    max_v = 1.0 + half_uv_h - margin_v

    if min_v > max_v:
        zoom_state.center_v = 0.5
    else:
        zoom_state.center_v = max(min_v, min(zoom_state.center_v, max_v))
    # --- Bounding Logic End ---

    uv_x_min = zoom_state.center_u - half_uv_w
    uv_x_max = zoom_state.center_u + half_uv_w
    uv_y_min = zoom_state.center_v - half_uv_h
    uv_y_max = zoom_state.center_v + half_uv_h

    uv_a = (uv_x_min, uv_y_max)
    uv_b = (uv_x_max, uv_y_min)

    # 6. Clip and Draw
    scale_u_px = view_width / uv_width_size
    scale_v_px = view_height / uv_height_size

    # Project Texture Edges
    raw_img_left = p_min_x + (0.0 - uv_x_min) * scale_u_px
    raw_img_right = p_min_x + (1.0 - uv_x_min) * scale_u_px
    raw_img_top = p_min_y + (uv_y_max - 1.0) * scale_v_px
    raw_img_bottom = p_min_y + (uv_y_max - 0.0) * scale_v_px

    # Intersect with Viewport
    clip_left = max(p_min_x, raw_img_left)
    clip_right = min(p_max[0], raw_img_right)
    clip_top = max(p_min_y, raw_img_top)
    clip_bottom = min(p_max[1], raw_img_bottom)
    draw_list: _DrawList = imgui.get_window_draw_list()

    if imgui.is_mouse_hovering_rect(clip_left, clip_top, clip_right, clip_bottom):
        draw_state.hover_reported = True
    else:
        draw_state.hover_reported = False

    Core.melty.push_clip((clip_left, clip_top, clip_right - 3, clip_bottom))
    draw_list.add_image_rounded(texture_id,
                                a=p_min,
                                b=p_max,
                                uv_a=uv_a,
                                uv_b=uv_b,
                                rounding=5.0)
    draw_list.add_rect(raw_img_left, raw_img_top, raw_img_right + 1, raw_img_bottom + 1,
                       imgui.get_color_u32_rgba(*mixed_color[:3], 1.0),
                       0.0, 0, 1.0)
    Core.melty.pop_clip()

    line_height = imgui.get_text_line_height()
    draw_list.add_text(max(p_min_x + 5, raw_img_left), clip_top - line_height - 5,
                       imgui.get_color_u32_rgba(*mixed_color[:3], 1.0),
                       text=f"{original_id} - {texture_id} - {width}x{height} - Zoom: {zoom_state.zoom:.2f}x")

    gl.glBindTexture(gl.GL_TEXTURE_2D, original_texture)

    return False, draw_state


@render_func(is_default_for=ManagedWindow, is_tree=False, show_name=False, use_cache=True,
             shadow=False, show_bg=False, selectable=False, show_add_delete=False,
             show_tint=False, wrap=False, with_header=draw_header, temp=True)
def draw_managed_window(input_value, name, draw_state, mouse_down=False, selectable=False, **kwargs):
    try:
        window_draw_state = input_value.draw_state
    except Exception as e:
        imgui.text(f"Error accessing draw_state: {e}")
        return False, input_value

    window_input_value = input_value.input_value
    name = window_draw_state.name

    start_cursor = imgui.get_cursor_screen_pos()
    imgui.dummy(4, 20)
    imgui.same_line()

    if not window_draw_state.persistent and not window_draw_state.seen and window_draw_state.closed:
        Core.melty.delete_window(window_draw_state)

    window_tint = None

    if hasattr(window_input_value, 'tint') and window_input_value.tint is not None:
        changed, new_tint = draw_tuple(window_input_value.tint, name="")
        if changed:
            window_input_value.tint = new_tint
            window_draw_state.tint = window_input_value.tint
        draw_state.tint = window_draw_state.tint
        window_tint = window_input_value.tint

    elif window_draw_state.tint is not None:
        changed, new_tint = draw_tuple(window_draw_state.tint, name="")
        if changed:
            window_draw_state.tint = new_tint
        draw_state.tint = window_draw_state.tint
        window_tint = window_draw_state.tint

    imgui.same_line()

    if mouse_down:
        window_draw_state.closed = not window_draw_state.closed

    button_height = 31
    target_spacing = 81
    target_tint_value = 0.103

    if name == "Window Manager":
        button(f"{name}", color=(0, 0, 0, 0),
               saturation=1.3, width=130, height=button_height)[0]
        return

    # Pass the search-match flags (set by draw_collection for this key) through
    # to the name button so it can draw the find highlight — the visible row is
    # this button, not a header.
    _search_match = kwargs.get("search_match", False)
    _search_current = kwargs.get("search_current", False)
    if window_draw_state.closed:
        if button(f"{name}", color=window_tint, z_offset=-4, tint_value=0.035, factor=0.92, text_value=0.305,
                  saturation=0.872, width=draw_state.content_width - target_spacing, height=button_height,
                  search_match=_search_match, search_current=_search_current)[0]:
            window_draw_state.closed = False
            this_window_right = draw_state.abs_left + draw_state.width
            # summon_window does the anchor math AND clips the result to the
            # screen, so a row low in the list can't put the window with its
            # bottom below the bottom of the screen.
            Core.melty.summon_window(window_draw_state, this_window_right + 10, draw_state.abs_top)
            Core.melty.cache.invalidate_up_by_obj(input_value)
    else:
        if button(f"{name}", saturation=1.315, z_offset=4, color=window_tint, factor=0.659, value=-0.205, text_value=1.357,
                  width=draw_state.content_width - target_spacing, height=button_height,
                  search_match=_search_match, search_current=_search_current)[0]:
            window_draw_state.closed = True

    imgui.same_line()

    if window_tint is None or not isinstance(window_tint, tuple) or len(window_tint) < 3:
        window_tint = (2.558, 0.5, 0.5)

    imgui.set_cursor_screen_pos((draw_state.abs_left + draw_state.content_width-20, draw_state.abs_top))
    target_icon = ""  # Target icon (FontAwesome Unicode)
    if  button(f"{target_icon}##{name}", height=button_height, color=window_tint, z_offset=2, tint_value=target_tint_value,
           factor=0.799,
           saturation=0.764, shadow=False)[0]:
        this_window_right = draw_state.abs_left + draw_state.width
        from_zero_x = window_draw_state.abs_left - window_draw_state.window_pos[0]
        from_zero_y = window_draw_state.abs_top - window_draw_state.window_pos[1]
        window_draw_state.window_pos = (this_window_right + 10 - from_zero_x, draw_state.abs_top - from_zero_y)
        Core.melty.move_window_to_front(window_draw_state)
        Core.melty.cache.invalidate_up_by_obj(input_value)

    imgui.set_cursor_screen_pos(start_cursor)

    live_tint = (0.409, 0.1, 0.1)

    if window_draw_state.live:
        fa_live_icon = ""
        imgui.text_colored(fa_live_icon, *(live_tint))
        imgui.same_line()


def draw(vis):
    draw_melty_windows(vis)


def export_code(test_param_2: int = 5):
    # print(f"hello {test_param_2}")
    global code_export_str
    code_export_str = proxy.node.code


@render_func(use_cache=False)
def draw_drag_drop_target(input_value, draw_state, on_drag, do_flow, depth,
                          collection, key, melty, y_offset, enable_flow, min_width,
                          unique, tag, style_manager, offset=0, indent_size=10):
    if Core.melty.active_layer == Core.melty.drag_layer:
        return False, 0.0

    cursor_y_screen = imgui.get_cursor_screen_pos()[1]

    if collection == input_value or not Core.melty.is_window_enabled():
        return False, 0.0

    if melty.initial_drag_offset is None:
        return False, 0.0

    if key is None:
        pass
    # ----------------- top spacing -----------
    falloff = 25.0  # Higher is gentler
    if enable_flow:
        drop_gap = 6.0
    else:
        drop_gap = 0.0

    drag_delta_curve = 1.0 - max(0.0, min(1.0, 1.0 - abs(melty.drag_delta[1] / 15.0)))

    mouse_pos = imgui.get_mouse_pos()
    cursor_top = imgui.get_cursor_screen_pos()[1]
    cursor_left = imgui.get_cursor_screen_pos()[0]
    static_offset = drop_gap
    distance_to_mouse = abs(mouse_pos[1] - cursor_y_screen -
                            melty.initial_drag_offset[1] - drop_gap + static_offset)
    bell_curve = max(0.0, min(1.0, 1.0 - (distance_to_mouse / falloff)))

    window_size = imgui.get_window_size()
    window_pos = imgui.get_window_position()
    window_rect = (window_pos[0], window_pos[1],
                   window_pos[0] + window_size[0],
                   window_pos[1] + window_size[1])
    mouse_over_window = imgui.is_mouse_hovering_rect(*window_rect)

    if melty.drag_in_progress:
        if melty.dragged_item is None:
            melty.drag_in_progress = False

        elif id(melty.dragged_item._input_value) == id(collection):
            return False, 0.0

    if melty.drag_in_progress and do_flow and not on_drag and mouse_over_window:
        flow_spacing = drop_gap * bell_curve * drag_delta_curve
    else:
        flow_spacing = 0.0
        drag_delta_curve = 1.0

    if tag == "top":
        Core.melty.flow_spacing += (flow_spacing)
        # imgui.set_cursor_pos_y(imgui.get_cursor_pos()[1] + (flow_spacing))

    draw_list = imgui.get_window_draw_list()
    # if Core.melty.channels_split:
    #     draw_list.channels_set_current(min(Core.melty.max_depth - 1, depth + 2))

    # line_width = imgui.get_style().frame_padding.y * 2.0
    # color = style_manager.make_color_rgb(*(1.0, 1.0, 1.0), factor=1.0,
    #                                      value=1.0, alpha=1.0, saturation_scale=0.3)

    # cursor_bottom = imgui.get_cursor_screen_pos()[1]
    # ------------------ end spacing -----------
    cursor_bottom = cursor_top + max(2.0, flow_spacing)

    if tag == "bottom":
        # span = cursor_bottom - cursor_top
        cursor_bottom += 0
        cursor_top += 0

    if melty.drag_in_progress and not on_drag and do_flow and mouse_over_window:
        if draw_state.height is not None:
            active_drop = (melty.drag_drop_target == draw_state.unique
                           and tag == melty.drag_drop_target_tag)

            if Core.melty.channels_split:
                draw_list.channels_set_current(min(Core.melty.get_channel() + 1, Core.melty.max_depth - 1))

                if active_drop:
                    draw_list.channels_set_current(min(Core.melty.get_channel() + 2, Core.melty.max_depth - 1))
                    cursor_bottom += ((1.0 - drag_delta_curve) * drop_gap)

            if distance_to_mouse < melty.nearest_drop_distance:
                melty.nearest_drop_distance = distance_to_mouse
                melty.nearest_drop_target = draw_state.unique
                melty.nearest_drop_target_tag = tag

                melty.drag_drop_action.target_unique = draw_state.unique
                melty.drag_drop_action.target_tag = tag
                melty.drag_drop_action.target_key = key
                melty.drag_drop_action.target_collection = collection
                melty.drag_drop_action.target_draw_state = draw_state

                if melty.drag_drop_action.target_key is None:
                    pass

            height_as_factor = 800.0
            drag_distance = sqrt(melty.drag_delta[0] ** 2 + melty.drag_delta[1] ** 2)
            initial_fade_offset = max(min(1.0, melty.total_drag_distance / 10.0), 0.0)
            if melty.total_drag_frames < 1:
                initial_fade_offset = 0.0
            opacity = max(0.0, min(1.0, 1.0 - (distance_to_mouse / (height_as_factor * 0.3))))
            opacity *= initial_fade_offset
            # opacity = 1.0 if active_drop else opacity

            bg_tint = Core.melty.get_bg_color(-1)
            bg_style = GlobalStyle.get_global_constant("bg_style", folder="bg_styles")

            color = style_manager.make_custom_styled(*bg_tint, input=bg_style,
                                                     value=1.3,
                                                     alpha=opacity, saturation=0.8)

            # color = style_manager.make_color_rgb(*bg_tint, factor=0.0,
            #                                      value=1.0, alpha=opacity, saturation_scale=1.0)
            inactive_color = style_manager.make_custom_styled(*bg_tint, input=bg_style,
                                                              value=0.7,
                                                              alpha=opacity, saturation=0.8)
            # if draw_state.width == None:
            #     draw_state.width = min_width
            # if draw_state.left == None:
            #     draw_state.left = 1

            padding = imgui.get_style().frame_padding.x

            color = color if active_drop else inactive_color

            top = cursor_top - 1
            bottom = max(draw_state.abs_top, cursor_bottom - 1)
            left = draw_state.abs_left + offset
            right = draw_state.abs_left + draw_state.width - indent_size
            width = draw_state.width
            height = draw_state.height

            draw_list.add_rect_filled(left, top, right, bottom,
                                      col=imgui.get_color_u32_rgba(*color), rounding=4.0)

            if opacity > 0:
                Core.melty.cache.mask_mark_rect(draw_state, Core.melty.max_depth - 1, draw_state.shadow_depth, left, top, width,
                                           height,
                                           key=f"{left}x{top}_flow")
            #
            # draw_list.add_line(draw_state.left, draw_state.abs_top - 2 - offset,
            #                    draw_state.left + draw_state.width,
            #                    draw_state.abs_top - 2 - offset,
            #                    col=imgui.get_color_u32_rgba(*color), thickness=3)

    return False, flow_spacing


@hotkey(glfw.KEY_O)
def toggle_offscreen():
    if Core.melty.cache.enabled:
        Core.melty.cache.set_enabled(False)
    else:
        Core.melty.cache.set_enabled(True)


import imgui


def draw_vertical_scrollbar(content_height: float,
                            view_height: float,
                            view_width: float,
                            scroll_offset: float,
                            scrollbar_width: float,
                            left: float = 0.0,
                            top: float = 0.0,
                            *,
                            pad: float = 0.0,
                            rounding: float = 3.0,
                            min_grab_size: float | None = None):
    # Style & colors
    style = imgui.get_style()
    if min_grab_size is None:
        min_grab_size = float(style.grab_min_size)

    col_track = imgui.get_color_u32_rgba(0, 0, 0, 0.1)
    col_grab = imgui.get_color_u32_rgba(1, 1, 1, 0.3)
    col_border = imgui.get_color_u32(imgui.COLOR_BORDER)

    # Early clamps & deriveds
    view_height = max(0.0, float(view_height))
    view_width = max(0.0, float(view_width))
    content_height = max(0.0, float(content_height))
    scrollbar_width = max(0.0, float(scrollbar_width))

    max_scroll = max(0.0, content_height - view_height)
    scroll_offset = float(max(0.0, min(scroll_offset, max_scroll)))

    # Anchor the container at the current cursor position in screen space
    origin_x, origin_y = (left, top)

    bar_margin = 4.0
    bar_margin_x = 1.0

    # Track geometry (stick it to the right edge of the container)
    track_w = min(scrollbar_width, view_width)
    track_h = view_height
    track_x1 = origin_x + (view_width - track_w) - bar_margin_x
    track_y1 = origin_y + bar_margin
    track_x2 = track_x1 + track_w - bar_margin_x
    track_y2 = track_y1 + track_h - bar_margin * 2

    # Compute grab size & position
    if content_height <= 0.0 or track_h <= 0.0:
        grab_h = 0.0
        t = 0.0
    else:
        # Proportional size with a minimum; cap to track height.
        ratio = view_height / content_height if content_height > 0.0 else 1.0
        grab_h = max(min_grab_size, ratio * track_h)
        grab_h = min(grab_h, track_h)

        # Normalized scroll position -> grab top
        travel = max(0.0, track_h - grab_h)
        t = 0.0 if max_scroll == 0.0 else (scroll_offset / max_scroll)
        t = max(0.0, min(1.0, t))  # clamp just in case

    grab_y1 = track_y1 + (max(0.0, track_h - grab_h) * t)
    grab_y2 = grab_y1 + grab_h

    # Inner padding for nicer visuals
    inner_x1 = track_x1 + pad
    inner_x2 = track_x2 - pad
    inner_y1 = track_y1 + pad
    inner_y2 = track_y2 - pad
    grab_x1 = inner_x1
    grab_x2 = inner_x2
    grab_y1 = max(inner_y1, min(grab_y1, inner_y2 - (grab_y2 - grab_y1)))
    grab_y2 = grab_y1 + max(0.0, min(grab_h, inner_y2 - inner_y1))

    # Draw
    dl = imgui.get_window_draw_list()
    # Track
    track_w = track_x2 - track_x1
    track_h = track_y2 - track_y1
    dl.add_rect_filled(track_x1, track_y1, track_x2, track_y2, col_track, rounding)
    # Core.melty.cache.mask_mark_rect(Core.melty.depth, track_x1, track_y1, track_w, track_h,
    #                            key=str(Core.melty.unique_stack[-1]) + "scrollbar")

    dl.add_rect(track_x1, track_y1, track_x2, track_y2, col_border, rounding)
    # Grab
    if grab_y2 > grab_y1 and grab_x2 > grab_x1:
        dl.add_rect_filled(grab_x1, grab_y1, grab_x2, grab_y2, col_grab, rounding)
        dl.add_rect(grab_x1, grab_y1, grab_x2, grab_y2, col_border, rounding)

    return {
        "offset": scroll_offset,
        "track_min": (track_x1, track_y1),
        "track_max": (track_x2, track_y2),
        "grab_min": (grab_x1, grab_y1),
        "grab_max": (grab_x2, grab_y2),
        "visible": content_height > view_height
    }


bg_style_default = {
    "value": 0.01,
    "saturation": 1.2,
    "alpha": 1.0,
    'max_value': 1.0
}


def get_bg_color(depth, rounding, style_manager, auto_resize):
    depth_factor = GlobalStyle.get_global_constant("depth_factor", default=1.0, folder="bg_styles") * 0.95
    depth_offset = GlobalStyle.get_global_constant("depth_offset", default=0.0, folder="bg_styles") - 1.3
    dynamic_value = max(0, (float(depth + depth_offset) * depth_factor))

    hovered_offset = 0.0

    def mix_colors(c1, c2, fac):
        return (c1[0] * (1 - fac) + c2[0] * fac,
                c1[1] * (1 - fac) + c2[1] * fac,
                c1[2] * (1 - fac) + c2[2] * fac)

    global bg_style_default
    bg_style = GlobalStyle.get_global_constant("bg_style", default=bg_style_default, folder="bg_styles")
    outline_factor = GlobalStyle.get_global_constant("outline_factor", default=1.0, folder="bg_styles") * 1.4

    if not auto_resize:
        outline_factor *= 1.3

    if auto_resize:
        bleed_factor = 0.2
    else:
        bleed_factor = 0.0
    bg_bleed = Core.melty.get_bg_color(-1)
    bg_bleed = style_manager.make_custom_styled(*bg_bleed, input=bg_style,
                                                value=0.6,
                                                alpha=1.0, saturation=1.8)
    bg_color = (style_manager.
                make_color_style_value(input=bg_style, value=max(0, dynamic_value) + hovered_offset))
    bg_color = mix_colors(bg_color, bg_bleed, bleed_factor)
    return bg_color


def seperator(height):
    imgui.dummy(0, snap_int(height / 2))
    imgui.separator()
    imgui.dummy(0, snap_int(height / 2))


def test_func():
    # Some comment
    # Comment here
    some_val = 1.706
    some_dict = {"some_key": -0.02,
                 "key": False,
                 "key_2": 2.421
                 }

def _clamp_bg_value(color, max_bg_value):
    """Cap a background color's VALUE (max channel, as in HSV) at `max_bg_value`,
    keeping hue exact and BOOSTING saturation by the same factor the value was
    cut by (s / k, clamped to 1.0). A plain uniform channel scale holds HSV
    saturation constant but still reads as washed out once it's dark, so the
    boost buys the colorfulness back — the color only ever gets darker and
    *more* saturated, never grayer.

    This is the LAST thing applied to a bg color — it bounds the color actually
    painted, not the depth ramp that fed it, so whatever the depth/tint/bleed
    chain produced, `max_bg_value=0` is black and `0.2` is at most 20% value.
    Deliberately not text_editor's _brightness_clamp: that one is a perceptual
    (luma) guard and no-ops at max_b == 0, which would break the black case.

    Done in raw channel arithmetic rather than a colorsys round trip: hue is
    just the position of the mid channel in the [min, max] span, so rebuilding
    against the new chroma preserves it without ever naming an angle."""
    if max_bg_value is None or color is None or len(color) < 3:
        return color
    rest = tuple(color[3:])
    red, green, blue = color[0], color[1], color[2]
    value = max(red, green, blue)
    if value <= max_bg_value:
        return color
    if max_bg_value <= 0 or value <= 0:
        return (0.0, 0.0, 0.0) + rest

    low = min(red, green, blue)
    if low >= value:  # achromatic - no hue to preserve, just darken
        return (max_bg_value, max_bg_value, max_bg_value) + rest

    # k is the cut applied to the value; undo it with saturation.
    k = max_bg_value / value
    saturation = min(1.0, ((value - low) / value) / k)
    chroma = max_bg_value * saturation
    new_low = max_bg_value - chroma
    span = value - low
    return (new_low + (red - low) / span * chroma,
            new_low + (green - low) / span * chroma,
            new_low + (blue - low) / span * chroma) + rest


def compute_bg_color(bg_offset=0, tint=None, nested_bg=False, max_bg_depth=None, max_bg_value=None):
    depth_wrap = 34
    depth_scale = 1.629
    intensity_factor = 0.021
    intensity_offset = -0.336
    outline_depth_mul = 0.786
    # More text
    bleed_style = {'value': -0.111, 'alpha': 1.12, 'saturation': 7.045}

    bg_style = {
        'value': -0.004, 'saturation': 1.101,
        'alpha': 0.504, 'max_value': 1.8,
    }

    def mix_colors(color_a, color_b, factor):
        return (
            color_a[0] * (1 - factor) + color_b[0] * factor,
            color_a[1] * (1 - factor) + color_b[1] * factor,
            color_a[2] * (1 - factor) + color_b[2] * factor,
        )

    # -- Depth calculation -------------------
    max_depth = 15
    bg_depth = Core.melty.bg_depth if Core.melty.bg_depth is not None else 0
    bg_offset = bg_offset if bg_offset is not None else 0
    wrapped_depth = min(max_depth, (bg_depth % depth_wrap) + bg_offset)
    # Caller-supplied ceiling on the effective depth: past this step the color
    # keeps the palette value for max_bg_depth instead of getting lighter.
    if max_bg_depth is not None:
        wrapped_depth = min(wrapped_depth, max_bg_depth)
    scaled_depth = wrapped_depth * depth_scale
    depth_intensity = (scaled_depth + intensity_offset) * intensity_factor
    max_depth_intensity = 0.652
    depth_intensity = min(depth_intensity, max_depth_intensity)


    # ── Outline color ──────────────────────────────────────────
    depth_mul = outline_depth_mul
    if not nested_bg:
        depth_mul *= 1.00

    # ── Background bleed color ─────────────────────────────────
    bleed_factor = 0.501 if nested_bg else 0.446

    bleed_base = Core.melty.get_bg_color(-1)
    bleed_color = Melty.style_manager.make_custom_styled(
        *bleed_base, input=bg_style, **bleed_style,
    )

    # ── Fill rendering ─────────────────────────────────────────
    bg_color = Melty.style_manager.make_color_style_value(input=bg_style, value=max(0.0, depth_intensity))
    bg_color = mix_colors(bg_color, bleed_color, bleed_factor)

    return _clamp_bg_value(bg_color, max_bg_value)

@window
def draw_bg(left=25, top=0, width=0, height=57, depth=0, rounding=6.0, bg_offset=0,
            outline=True, bg_color=None, opacity=0.0,
            style_manager=None, tint=None, outline_tint=None, selected=False,
            hovered=False, pressed=False, nested_bg=False, saturation=1.0, max_bg_depth=None,
            max_bg_value=None, **kwargs):
    # -- Constants ---------------------------------
    min_value = -0.272
    depth_wrap = 300
    depth_scale = 2.713
   
    # [tint=(2,1,1)]
    corner_radius = rounding
    border_inset = 2.802
    border_inset_half = 1.5
    stroke_width = 4.0
    # How depth maps to color intensity
    intensity_factor = 0.021
    intensity_offset = 3.137

    some_var = [32, 18, 19]
    # Outline color tuning
    outline_base = 1.765
    outline_depth_mul = 0.786
    outline_sat = {'default': 1.1, 'nested': 1.473}

    # More text
    bleed_mix = {'nested': 0.472, 'default': 0.526}
    bleed_style = {'value': -0.035, 'alpha': 1.112, 'saturation': 6.592}
    outline_bleed_mix = 0.272
    # Hover offsets per interaction state
    hover_offset_by_state = {
        'default': -1.807,
        'selected': -1.401,
        'pressed_hi': -1.813,  # pressed + opacity > 0.5
        'pressed_lo': -0.441,
    }
    bg_style = {
        'value': -0.004, 'saturation': 1.101,
        'alpha': 0.504, 'max_value': 1.8,
    }

    # ── Helpers ────────────────────────────────────────────────
    def current_indent_px():
        return Core.melty.current_indent

    def mix_colors(color_a, color_b, factor):
        return (
            color_a[0] * (1 - factor) + color_b[0] * factor,
            color_a[1] * (1 - factor) + color_b[1] * factor,
            color_a[2] * (1 - factor) + color_b[2] * factor,
        )

    # -- Depth calculation -------------------
    max_depth = 30
    if Core.melty.bg_depth + bg_offset < 2:
        wrapped_depth = min(max_depth, (Core.melty.bg_depth) + bg_offset)
    else:
        wrapped_depth = min(max_depth, (Core.melty.bg_depth % depth_wrap) + bg_offset)

    # Caller-supplied ceiling on the effective depth: after this step the view
    # keeps the color entry for max_bg_depth instead of getting lighter.
    if max_bg_depth is not None:
        wrapped_depth = min(wrapped_depth, max_bg_depth)

    scaled_depth = wrapped_depth * depth_scale
    depth_intensity = (scaled_depth + intensity_offset) * intensity_factor
    max_depth_intensity = 0.652
    depth_intensity = min(depth_intensity, max_depth_intensity)

    # ── Geometry ───────────────────────────────────────────────
    right = left + width
    bottom = top + height

    fill_rect = (
        snap_int(left) + border_inset, snap_int(top) + border_inset,
        snap_int(right) - border_inset, snap_int(bottom) - border_inset,
    )
    outline_rect = (
        snap_int(left) + border_inset_half, snap_int(top) + border_inset_half,
        snap_int(right) - border_inset_half, snap_int(bottom) - border_inset_half,
    )

    # ── Interaction hover offset ───────────────────────────────
    hover_offset = hover_offset_by_state['default']
    if selected:
        hover_offset = hover_offset_by_state['selected']
    elif pressed:
        if opacity > 0.5:
            hover_offset = hover_offset_by_state['pressed_hi']
        else:
            hover_offset = hover_offset_by_state['pressed_lo']

    # ── Outline style ──────────────────────────────────────────
    sat = outline_sat['default']
    depth_mul = outline_depth_mul
    if not nested_bg:
        depth_mul *= 1.00
        sat = outline_sat['nested']

    # ── Background bleed color ─────────────────────────────────
    bleed_factor = bleed_mix['nested'] if nested_bg else bleed_mix['default']

    bleed_base = Core.melty.get_bg_color(-2)
    bleed_color = style_manager.make_custom_styled(
        *bleed_base, input=bg_style, **bleed_style,
    )
    bleed_base = mix(*Core.melty.get_bg_color(-1)[:3], *bleed_color[:3], 0.32)
    bleed_color = style_manager.make_custom_styled(
        *bleed_base, input=bg_style, **bleed_style,
    )

    # ── Outline rendering ──────────────────────────────────────
    outline_value = max(min_value, depth_intensity * depth_mul + outline_base + hover_offset)
    outline_color = style_manager.make_color_style_value(
        input=bg_style, saturation=sat, value=outline_value,
    )
    outline_color = mix_colors(outline_color, bleed_color, outline_bleed_mix)

    if outline:
        packed_outline = imgui.get_color_u32_rgba(*outline_color[:3], 1.0)
        if outline_tint is not None:
            packed_outline = imgui.get_color_u32_rgba(*outline_tint[:3], 1.0)
        imgui.get_window_draw_list().add_rect(
            *outline_rect, col=packed_outline, rounding=corner_radius, thickness=stroke_width,
        )

    # ── Fill rendering ─────────────────────────────────────────
    if bg_color is None:
        bg_color = style_manager.make_color_style_value(input=bg_style, saturation=bg_style['saturation'] * saturation,
                                                        value=max(min_value, depth_intensity))
        bg_color = mix_colors(bg_color, bleed_color, bleed_factor)

    # Applies to whatever ends up as the fill - depth-ramp color OR a passed
    # bg_color/tint, so the cap holds regardless of the input's hue/brightness.
    bg_color = _clamp_bg_value(bg_color, max_bg_value)
    packed_fill = imgui.get_color_u32_rgba(bg_color[0], bg_color[1], bg_color[2], 1.0)
    if tint is not None:
        tinted = _clamp_bg_value(tint, max_bg_value)
        packed_fill = imgui.get_color_u32_rgba(*tinted[:3], opacity)

    if opacity > 0.0:
        imgui.get_window_draw_list().add_rect_filled(*fill_rect, col=packed_fill, rounding=corner_radius)

    return False, bg_color



@render_func(use_cache=True, selectable=False, disable_scroll=True, indent_size=0, show_bg=False, min_width=5,
             min_height=10, wrap=True, show_add_delete=False, rounding=None, icon=None)
def button(input_value="", width=5, height=14, draw_state=None, alpha=1.00, left_mouse_held=False, shadow=True, left_mouse_down=False,
           color=(0.533, 0.068, 0.5), icon=None, highlight_hovered=True, hovered=False, style_manager=None, show_button_bg=True,
           factor=1.0, tint_value=0.16, text_value=1.023, saturation=1.2, text_saturation=0.8, text_align="center",
           search_match=False, search_current=False, tint=None, rounding=None, corner_radius=6.0, text_pad=15,
           max_bg_brightness=0.25):

    if color is not None:
        if not isinstance(color, tuple) or len(color) < 3:
            color = (2.558, 0.5, 0.5)
        if shadow:
            if left_mouse_held:
                draw_state.z_offset = 0
            else:
                draw_state.z_offset = 3.0
        else:
            draw_state.z_offset = 0.0

        if hovered and highlight_hovered:
            mixed_color = style_manager.make_color_rgb(color[0], color[1], color[2], value=tint_value + 0.05,
                                                       factor=factor, saturation_scale=saturation, alpha=1.0)
        else:
            mixed_color = style_manager.make_color_rgb(color[0], color[1], color[2], value=tint_value,
                                                       factor=factor, saturation_scale=saturation, alpha=1.0)
        # Brightness guard: cap the bg's perceived brightness (scale-preserving,
        # same clamp as the window's tint washes) so the bright text keeps
        # contrast even when a vivid/near-white color is passed in.
        mixed_color = _brightness_clamp(mixed_color[0], mixed_color[1], mixed_color[2],
                                        0.0, max_bg_brightness)
        text_color = style_manager.make_color_rgb(color[0], color[1], color[2], value=text_value + (1.5 if hovered else 0.0),
                                                  factor=factor, saturation_scale=text_saturation, alpha=1.0)
    else:
        text_color = (1.0, 1.0, 1.0)
        mixed_color = (0, 0, 0)

    button_txt = str(input_value).split("##")[0]
    if icon is not None:
        button_txt = f"{icon} {button_txt}"

    min_size = imgui.calc_text_size(button_txt)
    # text_pad is an authored padding value, so it tracks the UI scale;
    # width/height are NOT scaled here - callers pass physical pixels through
    # them (the drag pickup size through height=), which would scale twice.
    width = max(width, min_size[0] + Melty.px(text_pad))
    height = max(height, min_size[1])
    draw_list: _DrawList = imgui.get_window_draw_list()
    bx0, by0 = imgui.get_cursor_screen_pos()

    imgui.dummy(width, height)
    draw_state.width = width
    draw_state.height = height

    # draw_state.width = btn_size[0]
    # draw_state.height = btn_size[1]


    bx1, by1 = bx0 + width, by0 + height
    # `corner_radius` is an auto-state param: Mode / class defaults, internal
    # draw_state.corner_radius writes all flow into it, and the mirror keeps
    # framework painters (selection highlight, blit mask) in sync. `rounding`
    # is an individual per-call override on top (e.g. the search results,
    # which read as a flat list of items, pass rounding to square the corners).
    rnd = corner_radius if rounding is None else rounding

    if alpha > 0.0 and show_button_bg:
        draw_list.add_rect_filled(bx0, by0, bx1, by1,
                                  imgui.get_color_u32_rgba(*mixed_color[:3], alpha), rounding=rnd)

    # Tint fill: button mutes `color` into a dark bg, so to show a window's tint
    # we paint the raw colour over it — at low alpha so it stays a subtle wash.
    elif tint is not None and show_button_bg:
        draw_list.add_rect_filled(bx0, by0, bx1, by1,
                                  imgui.get_color_u32_rgba(tint[0], tint[1], tint[2], 0.33),
                                  rounding=rnd)

    # Search match highlight (drawn under the text): the current row radiates a
    # circular gradient glow with its rect cut out so its content stays legible;
    # other matches get a thin outline. Tunable via Toggles.SearchSettings.
    # Lifted to a higher depth channel (restored after) so the halo's glow
    # past this row's rect isn't composited over by sibling rows' backgrounds.
    if search_match:
        if Melty.channels_split:
            draw_list.channels_set_current(
                min(Melty.get_channel() + 2, Melty.max_depth - 1))
        draw_search_highlight(draw_list, bx0, by0, bx1, by1, current=search_current, rounding=rnd)
        if Melty.channels_split:
            draw_list.channels_set_current(Melty.get_channel())

    if text_align == "left":
        draw_list.add_text(draw_state.abs_left + 5,
                           draw_state.abs_top + (height - min_size[1]) / 2.0 - 1,
                           imgui.get_color_u32_rgba(*text_color[:3], 1.0), button_txt)
    elif text_align == "right":
        draw_list.add_text(draw_state.abs_left + width - min_size[0] - 5,
                           draw_state.abs_top + (height - min_size[1]) / 2.0 - 1,
                           imgui.get_color_u32_rgba(*text_color[:3], 1.0), button_txt)
    else:
        draw_list.add_text(draw_state.abs_left + (width - min_size[0]) / 2.0 + 2,
                           draw_state.abs_top + (height - min_size[1]) / 2.0 - 1,
                           imgui.get_color_u32_rgba(*text_color[:3], 1.0), button_txt)


    if left_mouse_down:
        request_render()
        return True, input_value

    return False, None


def render_profiler_time(input_value=None, brief=False, style_manager=None):
    """
    Renders the time taken for a specific operation in the profiler.
    """
    in_ms = input_value * 1000.0
    if brief:
        if in_ms >= 0.99:
            formatted_value = f"{(in_ms):.1f}ms"
        else:
            formatted_value = f"{(in_ms):.2f}ms"
        if formatted_value.startswith("0."):
            formatted_value = formatted_value[1:]
    else:
        formatted_value = f"{in_ms:.2f} ms"
    golden_yellow = (2.0, 0.5, 0)
    dynamic_saturation_factor = GlobalStyle.profiler["object_attr"][
        "dynamic_saturation_factor"]
    dynamic_saturation_offset = GlobalStyle.profiler["object_attr"][
        "dynamic_saturation_offset"]
    saturation = GlobalStyle.profiler["object_attr"]["saturation"]
    value = GlobalStyle.profiler["object_attr"]["value"]
    dynamic_sat = (float(in_ms + dynamic_saturation_offset) * dynamic_saturation_factor)
    text_tint = style_manager.make_color_rgb(*golden_yellow, factor=1.0 - dynamic_sat,
                                             value=min(1.0, max(0, value + dynamic_sat * 0.5)),
                                             alpha=1.0,
                                             saturation_scale=max(0, saturation - dynamic_sat))[:3]
    imgui.text_colored(f"{formatted_value}", *text_tint)
    return False, input_value


@render_func(header_same_line=True, use_cache=True, is_default_for=(types.NoneType),
             shadow=False, is_tree=False, with_header=draw_header, temp=True)
def draw_none(input_value: NoneType):
    imgui.align_text_to_frame_padding()
    imgui.text_colored("None", *(0.164, 0.389, 0.197), 0.4)
    return False, input_value


@render_func(is_default_for=(bool), use_cache=True,
             is_tree=False, min_width=83, shadow=False, 
             with_header=draw_header, temp=True)
def draw_bool(input_value: bool, draw_state, left_mouse_clicked=None, max_width=359,
              max_height=100, min_height=20, header_same_line=True,
              selectable=False, left_mouse_drag=None, left_mouse_held=False, align_header=True,
              left_mouse_down=False):
    left_margin = 3
    box_h = 21
    text_inset = 11
    cursor_start = imgui.get_cursor_pos_x()
    
    if input_value:
        bg_color = imgui.get_color_u32_rgba(*Tint.checkbox_bg_selected(), 1.0)
        text_color = (*Tint.checkbox_text_true(), 1.0)
        icon = f""
    else:
        bg_color = imgui.get_color_u32_rgba(*Tint.checkbox_bg(), 1.0)
        text_color = (*Tint.checkbox_text(), 0.45)
        icon = f""
    
    label = f"{icon} {input_value}"
    icon_w = imgui.calc_text_size(icon)[0]
    label_w = imgui.calc_text_size(label)[0]

    # content_width carries a min_width floor (core_render), so when a long
    # unpadded header eats the row below min_width it overstates the space
    # actually left - the float-right logic would push the box past the row's
    # right edge. Measure the true leftover from the header to the row edge
    # (10 = the row's right content margin) and take the smaller for the cell.
    leftover = draw_state.abs_left + draw_state.width - 10 - cursor_start
    cell_width = min(draw_state.content_width, leftover)

    # The box hugs the content rather than expanding to fill the cell. When even
    # the label won't fit the available space we collapse to a square that shows
    # just the icon. `width` is the box's right edge measured from the cell left,
    # matching the original cursor-relative geometry below.
    avail = min(draw_state.width - 18, cell_width)
    full_width = left_margin + text_inset * 2 + label_w
    compact = full_width > avail
    if compact:
        width = 30
    else:
        width = full_width

    imgui.dummy(width, 21)
    draw_list = imgui.get_window_draw_list()

    outline_color = imgui.get_color_u32_rgba(*Tint.checkbox_outline(), 1.0)

    # Align the box to the right edge of the value cell: the leftover space
    # between the content width and the box's own width becomes the left offset.
    # When the box is wider than the cell this goes negative, pinning the right
    # edge and letting the box grow left over the header - so when there's
    # absolutely no room it starts overlapping the header rather than overflowing.
    right_offset = cell_width - width
    box_left = imgui.get_cursor_pos_x() + left_margin + right_offset
    box_right = imgui.get_cursor_pos_x() + width + right_offset
    box_top = draw_state.abs_top
    box_bottom = draw_state.abs_top + draw_state.content_height

    draw_list.add_rect_filled(box_left, box_top, box_right, box_bottom,
                              rounding=4, col=bg_color)
    draw_list.add_rect(box_left, box_top, box_right, box_bottom,
                       rounding=4, col=outline_color, thickness=1.5)

    # The hit target is the box itself, not the whole value cell - the rest of
    # the row (the header) stays usable for its own drag-and-drop. _bounding_hovered
    # keeps occlusion/z-order correct (no clicking through an overlapping window);
    # the rect test narrows it to the drawn box.
    box_hovered = draw_state._bounding_hovered and imgui.is_mouse_hovering_rect(
        box_left, box_top, box_right, box_bottom)

    if box_hovered:
        hover_color = imgui.get_color_u32_rgba(*Tint.checkbox_bg_hovered(), 0.2)
        draw_list.add_rect_filled(box_left, box_top, box_right, box_bottom,
                                  rounding=4, col=hover_color)

    if compact:
        # Center just the icon inside the square (which spans [left_margin, width]).
        box_w = width - left_margin
        imgui.same_line(left_margin + (box_w - icon_w) / 2.0 + right_offset + 1 )
        imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + 2)
        imgui.text_colored(icon, *text_color)
    else:
        imgui.same_line(text_inset + left_margin + right_offset)
        imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + 2)
        imgui.text_colored(label, *text_color)

    if box_hovered and imgui.is_mouse_clicked(0):
        request_render()
        return True, not input_value
    else:
        return False, input_value




@render_func(is_default_for=(str), shadow=False, wrap_text=False, show_bg=False, is_tree=False, wrap=False,
             show_header=False,
             show_add_delete=False, show_name=False, use_cache=True,
             disable_scroll=True, min_width=30, with_header=draw_header, temp=True)
def text(input_value: str, wrap, wrap_text=False, text_color=(1, 1, 1), draw_state=None, font=None):
    _font_pushed = False
    if font is not None and Core.melty.font_mgr is not None:
        _font_handle = Core.melty.font_mgr.get(font)
        if _font_handle is not None:
            imgui.push_font(_font_handle)
            _font_pushed = True

    text_size = imgui.calc_text_size(str(input_value), wrap_width=draw_state.content_width)
    if wrap_text and text_size[1] > imgui.get_text_line_height() * 4 and not wrap:
        imgui.push_text_wrap_pos(draw_state.abs_left + draw_state.width)
        imgui.push_style_color(imgui.COLOR_TEXT, text_color[0], text_color[1], text_color[2], 1.0)
        imgui.text_wrapped(str(input_value))
        imgui.pop_style_color()
        imgui.pop_text_wrap_pos()

    else:
        if text_color is not None:
            imgui.text_colored(str(input_value), text_color[0], text_color[1], text_color[2], 1.0)
        else:
            imgui.text(str(input_value))



    if font is not None:
        imgui.pop_font()

    return False, input_value


@render_func(is_default_for=(str), shadow=False, show_bg=False, wrap=False, selectable=False,
             is_tree=False, show_add_delete=False, use_cache=True, min_height=20,
             disable_scroll=True, with_header=draw_header, temp=True)
def draw_str(input_value: str, draw_state, editable=True, wrap=False, min_width=110, immediate_return=False, alpha=1.0):
    if not editable:
        imgui.push_style_var(imgui.STYLE_ALPHA, alpha)

        text_size = imgui.calc_text_size(str(input_value), wrap_width=draw_state.content_width)
        imgui.push_text_wrap_pos(draw_state.abs_left + draw_state.width)
        imgui.text_wrapped(str(input_value))
        imgui.pop_text_wrap_pos()

        imgui.pop_style_var(1)
        return False, input_value

    some_int = 29
    line_count = input_value.count('\n') + 1
    line_height = imgui.get_text_line_height()
    text_height = imgui.calc_text_size(str(input_value))[1] + line_height * 2
    if line_count == 1:
        padding = imgui.get_style().frame_padding.y
        height = imgui.get_text_line_height() + padding

    else:
        text_bottom = draw_state.abs_top + text_height
        clamped_bottom = text_bottom
        height = clamped_bottom - draw_state.abs_top

    show_controls = True

    if not show_controls:
        imgui.push_style_var(imgui.STYLE_ALPHA, 0)

    if False:
        if not wrap:
            item_width = draw_state.content_width - 1
        else:
            item_width = min_width

        if immediate_return:
            imgui.set_next_item_width(item_width)
            changed, value = imgui.input_text("##str", str(input_value))
        else:
            imgui.set_next_item_width(item_width)
            changed, value = imgui.input_text("##str", str(input_value),
                                              flags=imgui.INPUT_TEXT_ENTER_RETURNS_TRUE)
    else:
        # disable scrolling
        changed, value = draw_text(str(input_value), name=draw_state.name +"##innder", show_bg=True,
                                    editable=True, with_header=draw_header, width=draw_state.content_width - 10,
                                   show_name=False, is_tree=False, temp=True)

    if not show_controls:
        imgui.pop_style_var(1)

    if changed:
        return True, value
    return changed, value


def sort_dict_alphabetically(input_value, **kwargs):
    changed = False
    attr_name = "name"
    first_item = next(iter(input_value.items()), None)[1]
    if hasattr(first_item, attr_name):
        sorted_dict = dict(sorted(input_value.items(), key=lambda item: str(getattr(item[1], attr_name)).lower()))
        return changed, sorted_dict
    else:
        imgui.text("Cannot sort: items do not have 'name' attribute")
        return False, input_value


@render_func()
def unsort_dict_alphabetically(input_value, ref=None, changed=False):
    if ref is None:
        imgui.text("Original order not available")
        return False, input_value
    else:
        # Ref is the original dict
        ref.update(input_value)
        return changed, ref


def param_source_matrix(input_value, keys=None, func=None, include_unmatched=False, **kwargs):
    """Pivot a render function's possible INPUTS into a parameter × source
    table — the inputs-tab aggregator. `input_value` is the collected sources,
    a {source_name: {param: value}} mapping (caller kwargs, @defaults on the
    model class, mode kwargs, @render_func decorator kwargs, signature
    defaults, …); `keys` is the function's parameter-name list (rows) — pass it
    directly, or pass `func` and they're derived via inspect (unwrapped, minus
    the catch-all params). Returns {param: {source_name: value}}:

        rows     one per parameter, in parameter order — an EMPTY row means no
                 source sets it (still shown: the point is mapping the full
                 input surface in one spot)
        columns  one per source that sets the param, in source order

    Cells alias the source values (no copies). With include_unmatched, keys a
    source sets that are NOT parameters append as extra rows at the end —
    typos and **kwargs ride-throughs stay visible instead of vanishing.
    Shaped like sort_dict_alphabetically: a plain (changed, value) chain node;
    apply_param_source_matrix below is the unsort-style reverse.
    Source COLOR-CODING is not this function's job: codecs carry a tint
    (new_codecs.render_kwargs) that core_render merges in as the lowest
    kwargs layer, so codec-backed values color themselves wherever drawn."""
    changed = False
    if isinstance(input_value, dict):
        items = list(input_value.items())
    else:
        items = [(f"source_{i}", s) for i, s in enumerate(input_value or ())]
    items = [(str(n), s) for n, s in items if isinstance(s, dict)]

    if keys is None and func is not None:
        try:
            keys = [p for p in inspect.signature(inspect.unwrap(func)).parameters
                    if p not in ("args", "kwargs", "o_kwargs", "next_kwargs")]
        except (TypeError, ValueError):
            keys = []
        # A render func's input surface is its signature PLUS the kwargs the
        # @render_func machinery itself consumes (width/height/tint/shadow and
        # the flag zoo) - shared by every render func, loaded once per process
        # by re-scanning the decorator source (render_func_kwarg_names).
        if getattr(func, "__render_func__", False):
            _seen = set(keys)
            keys += [k for k in render_func_kwarg_names() if k not in _seen]
    keys = list(keys or [])

    matrix = {}
    for k in keys:
        row = {}
        for sname, sdict in items:
            if k in sdict:
                row[sname] = sdict[k]
        matrix[k] = row
    if include_unmatched:
        for sname, sdict in items:
            for k in sdict:
                # Parse metadata is NOT an input: skip non-plain-str keys
                # (Comment objects keying their own line in a class-body
                # parse), dunders/underscored bookkeeping, and the parse's
                # section keys - only real attribute names become rows.
                if (type(k) is not str or k.startswith('_')
                        or k in _PARSE_SECTION_KEYS):
                    continue
                if k not in matrix or (k not in keys and sname not in matrix[k]):
                    matrix.setdefault(k, {})[sname] = sdict[k]
    return changed, matrix


# Attributes ALWAYS in the inputs-tab param list (pinned right after the view
# function's own signature params), even if no source sets them organically -
# they come from the @render_func machinery, not the view function's signature.
MATRIX_DEFAULT_PRIORITY = ("width", "height", "min_height", "min_width", "tint")

# Structural sections of a GeneralParse dict - never attribute names, so
# param_source_matrix's include_unmatched must not turn them into rows when a
# a class/function parse is registered as a source (the class-var source).
_PARSE_SECTION_KEYS = frozenset({"decorators", "parameters", "locals"})

# Framework-injected parameters: present in most view-function signatures but
# never user-tunable, so they don't belong in the signature section.
_MATRIX_FRAMEWORK_PARAMS = {"input_value", "draw_state", "args", "kwargs",
                            "o_kwargs", "next_kwargs", "meta", "viewstate",
                            "self", "unique", "changed"}


def signature_param_names(func):
    """The view function's OWN tunable parameters (unwrapped signature minus
    the framework-injected names) plus MATRIX_DEFAULT_PRIORITY — the params
    pinned to the front of the inputs-tab list. For draw_float that's
    min_value/max_value/speed/…; width/height/min_height/min_width/tint ride
    along from the default list."""
    try:
        params = inspect.signature(inspect.unwrap(func)).parameters
    except (TypeError, ValueError):
        return []
    names = [p for p in params if p not in _MATRIX_FRAMEWORK_PARAMS]
    names += [k for k in MATRIX_DEFAULT_PRIORITY if k not in names]
    return names


@render_func()
def apply_param_source_matrix(input_value, ref=None, changed=False):
    """Reverse of param_source_matrix — the unsort_dict_alphabetically analog.
    `ref` is the ORIGINAL {source_name: dict} sources mapping; every edited
    cell writes back into the source dict it came from (a tint edited under
    the 'caller' column lands in the caller-kwargs dict), so each source's own
    save path can persist it. Sources absent from ref are left untouched."""
    if ref is None:
        imgui.text("Original sources not available")
        return False, input_value
    for param, row in input_value.items():
        if not isinstance(row, dict):
            continue
        for sname, val in row.items():
            src = ref.get(sname) if isinstance(ref, dict) else None
            # Skip already-equal cells: bubbling-wrapped source dicts mark
            # their host dirty on each write, so only real edits write back.
            if isinstance(src, dict) and (param not in src or src[param] is not val):
                src[param] = val
    return changed, ref


@render_func(use_cache=True, show_bg=False, shadow=False, with_header=None,
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
    changed = False
    tints = source_tints or {}
    locations = source_locations or {}
    plus_icon = "\uf067"   # FA plus -- explicit escape, see jump_to.py
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
    # the open popover (nothing ever calls it with closed=True again, so it
    # sticks open). Instead the trigger label is synced below by stamping
    # selected_label/selected_path on the dropdown's misc state, which only
    # covers a filter auto-switch changing the selection out from under it.
    dd_res = draw_dropdown(
        selected, collection={p: p for p in params},
        name=f"param_pick##{unique}",
        width=min(280, max(120, draw_state.content_width - 24)),
        show_header=False, return_extras=True)
    picked_changed, picked = dd_res[0], dd_res[1]
    if picked_changed and picked in params:
        draw_state._selected_param = picked
        selected = picked
        draw_state.invalidate()
    dd_ds = dd_res[2] if len(dd_res) > 2 else None
    dd_state = (getattr(dd_ds, 'misc', None) or {}).get('drop_down_state') if dd_ds is not None else None
    if dd_state is not None and getattr(dd_state, 'selected_label', None) != selected:
        dd_state.selected_label = selected
        dd_state.selected_path = (selected,)
    imgui.same_line()
    origin = "signature" if selected in prio_set else "core_render.py"
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
            from src.lsd.gl_gui.utils.jump_to_code import open_in_intellij
            threading.Thread(target=open_in_intellij, args=(str(loc[0]),),
                             kwargs={"line_number": loc[1]},
                             daemon=True).start()
        imgui.same_line()

        if is_set:
            # × removes the param from this source's dict. A plain dict
            # mutation: the bubbling invalidate notifies the owning object,
            # which goes dirty and persists via its own chain_out/save.
            if isinstance(src, dict):
                if button(times_icon, width=30, height=22, shadow=True, use_cache=True,
                          text_value=1.0, name=f"{sname}_delete##{selected}_{unique}",
                          show_button_bg=True, **tint_kwargs)[0]:
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
            ch, nv = draw_any(row[sname], name=f"{sname}##{selected}_{unique}",
                              key=selected,
                              tint=tint,
                              show_name=False, wrap=True,
                              bg_offset=2, z_offset=0, disable_scroll=True, width=167)
            if ch:
                row[sname] = nv
                changed = True
        elif can_write:
            # + stamps the param on this source - same plain-dict write the
            # cell editors use, so the same host-dirty/save machinery runs.
            if button(plus_icon, width=31, height=22, shadow=True, use_cache=True,
                      text_value=1.1, name=f"add_{sname}##{selected}_{unique}",
                      show_button_bg=True, **tint_kwargs)[0]:
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


@render_func(is_default_for=UsageRef, use_cache=True, shadow=True, z_offset=2, show_bg=True, with_header=draw_header,
             is_tree=True, tint=(0.11, 0.1, 0.16))
def draw_usage(input_value: UsageRef):
    imgui.text(
        f"{input_value.path} {input_value.line}:{input_value.column} {input_value.scope} {input_value.module_name}")

    return False, input_value


@render_func(is_default_for=(Comment), shadow=False, header_same_line=True, initial={"expanded":False}, icon="",
             is_tree=True, show_name=False, indent_size=8, selectable=False, use_cache=False, tint=(0.137, 0.683, 0.299, 0.708),
             show_bg=False, with_header=draw_header, temp=False, expanded_mode=ExpandMode.MANUAL)
def draw_comment(input_value: Comment, draw_state, style_manager, cursor_hover=False, font=Font.JETBRAINS_MONO_13):
    changed, value = False, input_value

    imgui.dummy(0, 0)
    depth = max(0.3, Core.melty.bg_depth)
    depth_scale = 0.047
    
    # [tint=(0.883, 0.712, 0.206, 0.34)]
    name_style = {
        'value': -0.420, 'saturation': 1.06,
        'alpha': 0.047, 'max_value': 0.704,
        'depth_factor': 0.34
    }
    depth_intensity = float(depth) * depth_scale
    name_style['value'] = depth_intensity * name_style['depth_factor'] + name_style['value']
    
    # [tint=(0.767, 0.379, 0.379)]
    alpha = 1.02
    
    
    sat_depth_factor = 0.0
    sat_depth_offset = 0.188
    sat_shift = float(depth + sat_depth_offset) * sat_depth_factor
    name_style['saturation'] = name_style['saturation'] + sat_shift

    name_color = style_manager.make_color_style_value(input=name_style)
    
    # A grouped multi-line comment is a '\n'-joined run of '# ' lines; strip the
    # '#'/'# ' prefix from EACH line so it displays as clean prose, not just the
    # first (str[2:] would leave a stray '#' on every continuation line).
    def _strip_hash(ln):
        return ln[2:] if ln.startswith("# ") else (ln[1:] if ln.startswith("#") else ln)
        
    display = "\n".join(_strip_hash(ln) for ln in str(input_value).split("\n"))

    # The framework header (is_tree=True) owns the expand/collapse button.
    # ExpandMode.MANUAL keeps this body visible while collapsed, with the
    # first line standing in for the whole comment.
    if "\n" in display and not draw_state.expanded:
        # Collapsed: one line truncated to the available width - never let it
        # spill onto a second row.
        flat = " ".join(display.split("\n"))
        avail = draw_state.abs_left + draw_state.width - imgui.get_cursor_screen_pos().x
        if imgui.calc_text_size(flat).x > avail:
            lo, hi = 0, len(flat)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if imgui.calc_text_size(flat[:mid]).x <= avail:
                    lo = mid
                else:
                    hi = mid - 1
            flat = flat[:lo].rstrip()
        imgui.push_style_color(imgui.COLOR_TEXT, *name_color[:3], alpha)
        imgui.text(flat)
        imgui.pop_style_color()
    else:
        imgui.push_text_wrap_pos(draw_state.abs_left + draw_state.width)
        imgui.push_style_color(imgui.COLOR_TEXT, *name_color[:3], alpha)
        imgui.text_wrapped(display)
        imgui.pop_style_color()
        imgui.pop_text_wrap_pos()

    if changed:
        return True, value
    return False, input_value


@render_func(use_cache=False, show_bg=True, shadow=False, selectable=False, with_header=None)
def draw_color_picker(input_value, wrap=True, draw_state=None, info=None, **kwargs):
    """Large immediate-mode HSV colour picker: a saturation/value square plus a
    hue bar. `input_value` is a 3- or 4-float RGB(A) tuple in 0..1; returns
    (changed, new_tuple). HSV is derived from the value each frame and the edit
    written straight back (imgui's own is_item_active tracks the drag), so it
    can be dropped anywhere; draw_tuple wraps it in Mode.POPOVER. The one piece
    of state is `draw_state._cp_precise`: the last full-precision RGBA we
    emitted plus its HSV. RGB→HSV loses hue at black/gray (h collapses to 0)
    and a driven value can echo back quantized (save/parse round trip, %.3f
    drag rounding) — so when the incoming value is just an echo of our own
    edit, we resume from the cache instead of re-deriving."""
    imgui.dummy(0,3)
    vals = list(input_value)
    has_alpha = len(vals) >= 4
    r, g, b = float(vals[0]), float(vals[1]), float(vals[2])
    a = float(vals[3]) if has_alpha else 1.0
    # Exact incoming channels - untouched channels are emitted as these, so
    # an edit to one channel doesn't override the others' cached/rounded
    # working copies.
    in_r, in_g, in_b, in_a = r, g, b, a

    # Tolerance for "this is our own value coming back"; covers %.3f drag
    # rounding (±0.0005) and 1/255 hex quantization (±0.002).
    ECHO_TOL = 0.002
    _prec = getattr(draw_state, '_cp_precise', None)
    is_echo = False
    if _prec is not None:
        p_rgba, p_hsv = _prec  # p_rgba is always stored as a 4-tuple
        is_echo = (abs(p_rgba[0] - r) <= ECHO_TOL and
                   abs(p_rgba[1] - g) <= ECHO_TOL and
                   abs(p_rgba[2] - b) <= ECHO_TOL and
                   abs(p_rgba[3] - a) <= ECHO_TOL)

    if is_echo:
        # Echo of our own edit - resume the full-precision working value so
        # our SV/hue markers don't jump on quantization noise, and the cached
        # hue survives even when the colour is currently black/gray.
        r, g, b = p_rgba[0], p_rgba[1], p_rgba[2]
        if has_alpha:
            a = p_rgba[3]
        h, s, v = p_hsv
    else:
        h, s, v = imgui.color_convert_rgb_to_hsv(r, g, b)
        if _prec is not None:
            # External change to black/gray: hue (and at black, saturation)
            # are undefined in the new value - keep the cached ones for
            # continuity rather than snapping the markers to red/top-left.
            if s <= 0.0 or v <= 0.0:
                h = _prec[1][0]
            if v <= 0.0:
                s = _prec[1][1]

    SQ, BAR_W, GAP = 180, 18, 8
    dl = imgui.get_window_draw_list()
    white = imgui.get_color_u32_rgba(1, 1, 1, 1)
    black = imgui.get_color_u32_rgba(0, 0, 0, 1)
    trans = imgui.get_color_u32_rgba(0, 0, 0, 0)
    changed = False
    hsv_changed = False  # only convert HSV->RGB when the square/hue is actually changed

    # Everything renders in NATURAL imgui flow (invisible_button advances the
    # cursor, same_line for the hue bar, plain widgets below). That keeps imgui's
    # content-size tracking honest so the auto-resized popover grows to match the
    # square + channel rows + hex.

    # --- SV square: white->hue across, transparent->black down ---
    sx0, sy0 = imgui.get_cursor_screen_pos()
    hr, hg, hb = imgui.color_convert_hsv_to_rgb(h, 1.0, 1.0)
    hue = imgui.get_color_u32_rgba(hr, hg, hb, 1)
    dl.add_rect_filled_multicolor(sx0, sy0, sx0 + SQ, sy0 + SQ, white, hue, hue, white)
    dl.add_rect_filled_multicolor(sx0, sy0, sx0 + SQ, sy0 + SQ, trans, trans, black, black)
    imgui.invisible_button("##sv", SQ, SQ)
    if imgui.is_item_active():
        mx, my = imgui.get_mouse_pos()
        s = min(max((mx - sx0) / SQ, 0.0), 1.0)
        v = 1.0 - min(max((my - sy0) / SQ, 0.0), 1.0)
        hsv_changed = True

    # --- Hue bar to its right, 6 gradient segments ---
    imgui.same_line(spacing=GAP)
    hx0, hy0 = imgui.get_cursor_screen_pos()
    for i in range(6):
        t0, t1 = i / 6.0, (i + 1) / 6.0
        r0, g0, b0 = imgui.color_convert_hsv_to_rgb(t0, 1, 1)
        r1, g1, b1 = imgui.color_convert_hsv_to_rgb(t1, 1, 1)
        c0 = imgui.get_color_u32_rgba(r0, g0, b0, 1)
        c1 = imgui.get_color_u32_rgba(r1, g1, b1, 1)
        dl.add_rect_filled_multicolor(hx0, hy0 + SQ * t0, hx0 + BAR_W, hy0 + SQ * t1, c0, c0, c1, c1)
    imgui.invisible_button("##hue", BAR_W, SQ)
    if imgui.is_item_active():
        h = min(max((imgui.get_mouse_pos()[1] - hy0) / SQ, 0.0), 1.0)
        hsv_changed = True

    # --- Markers (after input, at the current position) ---
    cx, cy = sx0 + s * SQ, sy0 + (1.0 - v) * SQ
    dl.add_circle(cx, cy, 6, black, thickness=1.0)
    dl.add_circle(cx, cy, 5, white, thickness=1.5)
    hmy = hy0 + h * SQ
    dl.add_rect(hx0 - 1, hmy - 2, hx0 + BAR_W + 1, hmy + 2, white, thickness=1.5)

    # Fold the SV/hue edit back to RGB only when the square or hue bar was just
    # dragged. Otherwise keep the original input RGB untouched - round-tripping
    # RGB->HSV->RGB every frame accumulates conversion error and feeds it back as
    # next frame's input, which is what made the RGB sliders jitter.
    if hsv_changed:
        r, g, b = imgui.color_convert_hsv_to_rgb(h, s, v)
        changed = True

    # --- RGBA drag floats (natural flow, below the square row). Dragging a
    # channel edits RGB directly, overriding the HSV-derived value this frame. ---
    imgui.dummy(0, 4)
    imgui.push_item_width(SQ + GAP + BAR_W)
    out, edited = [], []
    for lbl, cur in ([("R", r), ("G", g), ("B", b)] + ([("A", a)] if has_alpha else [])):
        imgui.set_next_item_width(draw_state.content_width - 30)
        ch, nv = imgui.drag_float(f"{lbl}##cp_{lbl}", cur, 0.004, 0.0, 1.0, "%.3f")
        if ch:
            changed = True
        edited.append(ch)
        # Keep `cur` verbatim unless this row was actually dragged - the
        # returned value can differ from format rounding even if untouched.
        out.append(min(max(nv, 0.0), 1.0) if ch else cur)
        imgui.dummy(0,1)
    imgui.pop_item_width()
    r, g, b = out[0], out[1], out[2]
    if has_alpha:
        a = out[3]

    # --- Hex code label (#rrggbb, +a with alpha) ---
    ri, gi, bi = (int(round(c * 255)) for c in (r, g, b))
    hex_str = (f"#{ri:02x}{gi:02x}{bi:02x}{int(round(a * 255)):02x}"
               if has_alpha else f"#{ri:02x}{gi:02x}{bi:02x}")

    if button("", tint=(1,0,0, 0.5), height=21, shadow=True, use_cache=True, name=f"delete_color##")[0]:
        request_render()
        return True, None
    imgui.same_line()
    imgui.text_colored(hex_str, *Tint.subtle_text())
    if info:
        # General-purpose caption (the tuple widget's info callback) - bottom
        # line, under the hex readout.
        imgui.dummy(0, 2)
        imgui.text_colored(str(info), 1.0, 1.0, 1.0, 0.45)
    if changed:
        # Untouched channels emit the EXACT incoming value - the working
        # copies may be truncated/rounded and must never overwrite precise
        # channels the user didn't edit. An SV/hue drag rewrites RGB
        # wholesale (that edit scope is all three channels); alpha only
        # changes when its own row was dragged.
        if has_alpha and not edited[3]:
            a = in_a
        if not hsv_changed:
            if not edited[0]:
                r = in_r
            if not edited[1]:
                g = in_g
            if not edited[2]:
                b = in_b
            # RGB drags edit the colour directly - refresh the cached HSV,
            # keeping hue/saturation where the new value leaves them undefined.
            nh, ns, nv = imgui.color_convert_rgb_to_hsv(r, g, b)
            if ns <= 0.0 or nv <= 0.0:
                nh = h
            if nv <= 0.0:
                ns = s
            h, s, v = nh, ns, nv
        draw_state._cp_precise = ((r, g, b, a), (h, s, v))
        request_render()
        return True, ((r, g, b, a) if has_alpha else (r, g, b))
    return False, input_value


@render_func(is_default_for=('tint', 'help_yellow_tint', 'color', 'context_select_tint', "text_color", "gradient_color", "outline_color"), has_popup=True,
             indent_size=2, is_tree=False, align_header=True, header_same_line=True, wrap=True,
             show_name=True, selectable=False, max_width=100, min_width=33, use_cache=False, with_header=draw_header)
def draw_tuple(input_value: tuple | types.NoneType, name, unique, draw_state, outline=False,
               info=None):
    is_open = False
    changed = False

    if input_value is None:
        # Position BEFORE drawing, exactly like the color branch below - a
        # first frame that skips the same_line breaks the header row (every
        # remaining item wraps to a new line) and the early return under the
        # button never restores it. Single-line rows only: line placement
        # belongs in the wrapper (it same_lines after the header unless it
        # chose multi_line), and forcing same_line on a multi_line frame drags
        # the widget back onto the header's line.
        if not draw_state.multi_line:
            imgui.same_line(spacing=4)
        if button("", height=21, shadow=False, z_offset=0, corner_radius=4, tint=(0,0,0, 0.1
        ), tint_value=0.14, use_cache=True, show_bg=True, text_pad=7,  name=f"add_tuple##{unique}",
                  show_button_bg=True)[0]:
            input_value = (0.0, 0.0, 0.0, 1.0)
            request_render()
            return True, input_value

    else:
        # if not draw_state.multi_line:
        #     imgui.same_line(spacing=4)

        is_color = input_value is not None and isinstance(input_value, tuple) and len(input_value) in (3, 4) and all(isinstance(c, (float, int)) for c in input_value)
        if is_color:
            # A swatch trigger that opens our own colour-picker popover (replacing
            # imgui's built-in popup). Same popover pattern as the dropdown: identity
            # in Melty.popover_focused_ds is the open state; click toggles it; the
            # picker window is anchored under the swatch and dismissed on outside
            # click / Esc. The picker itself is stateless and returns the new colour.
            from src.lsd.gl_gui.view.mode import Mode
            is_open = Melty.popover_focused_ds is draw_state
            col = list(input_value)
            alpha = col[3] if len(col) == 4 else 1.0
            # ALPHA_PREVIEW_HALF makes the swatch split: one half shows the colour
            # composited over a checkerboard at its real alpha, the other fully
            # opaque - so a length-4 tuple's transparency is visible in the chip itself
            # (plain color_button forces opaque regardless of the alpha we pass).
            flags = imgui.COLOR_EDIT_NO_TOOLTIP | imgui.COLOR_EDIT_ALPHA_PREVIEW_HALF
            if imgui.color_button(f"##swatch{unique}{name}", col[0], col[1], col[2], alpha,
                                  flags=flags, width=17, height=17):
                Melty.popover_focused_ds = None if is_open else draw_state
                if not is_open:
                    Melty._popover_open_frame = Melty.frame_count  # grace the opening click
                request_render()
            if outline:
                # Tight ring around the CHIP (the item just drawn) - the
                # widget's draw_state box is the measured min/max_width
                # envelope, far wider than the swatch.
                _omn, _omx = imgui.get_item_rect_min(), imgui.get_item_rect_max()
                imgui.get_window_draw_list().add_rect(
                    _omn.x - 1.5, _omn.y - 1.5, _omx.x + 1.5, _omx.y + 1.5,
                    imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.55), rounding=4.0)
            is_open = Melty.popover_focused_ds is draw_state  # reflect the toggle this frame

            # The picker window is closable -> fixed size (auto-resize is off for
            # closable windows), and its content is raw imgui (not child render_funcs)
            # so the framework can't measure it. Size the window to fit the SV square
            # (180) + the N channel drag-floats and the hex line, so nothing clips.
    # info: a general-purpose caption for the popover (e.g. the anywhere
    # swatch's "last known source"). A CALLABLE resolves only while the
    # popover is open for a lazy path, so closed swatches never pay for it.
    _info = None
    if is_open and info is not None:
        _info = info() if callable(info) else info
    picker_h = 180 + 14 + 4 * 26 + 26 + (22 if _info else 0)
    # parent_window=draw_state anchors the popover under the swatch and makes
    # the tuple the picker's ancestor, so clear_focus (which protects the
    # clicked swatch window closure) leaves the popover open when you click
    # the tuple, and dismisses it when you click anywhere else.
    # Flip up when opening down would run past the display bottom: the
    # popover renders at the invoking cursor + window_pos (core_render's
    # nested-window anchor), and the cursor here sits just under the swatch -
    # so the up offset is the picker's own height plus the swatch row.
    _pop_y = 10
    _anchor_y = imgui.get_cursor_screen_pos()[1]
    _disp_h = imgui.get_io().display_size[1]
    if _anchor_y + _pop_y + picker_h > _disp_h - 10:
        _pop_y = -(picker_h + 38)
    color_changed, new_color = draw_color_picker(input_value, name=f"color_picker{unique}",
                                            closed=not is_open, window_pos=(0, _pop_y), info=_info,
                                            parent_window=draw_state, width=216, height=picker_h, mode=Modes.POPOVER)
    if is_open:
        if color_changed:
            if new_color is not None:
                input_value = tuple(new_color)
            else:
                input_value = None
                request_render()
            changed = True
        # Dismiss on a click outside the swatch/popover, or on Esc.
        # if imgui.is_mouse_clicked(0):
        #     mx, my = imgui.get_mouse_pos()
        #     if not any(_is_in_subtree(d, draw_state) for d in Core.melty.bvh_query(mx, my)):
        #         Melty.popover_focused_ds = None
        #         request_render()
        if any(k == glfw.KEY_ESCAPE for k, _ in Core.melty.frame_key_events):
            Melty.popover_focused_ds = None
            request_render()
        # Keep re-rendering while a slider/square is being dragged so the live
        # imgui interaction (is_item_active) updates the frame.
        if Melty.imgui_any_item_active or imgui.is_mouse_down(0):
            Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10, force=True)
            request_render()
                
    # elif input_value is not None and len(input_value) > 0 and isinstance(input_value[0], (float, int)):
    #     str_value = ", ".join([str(v) for v in input_value])
    #     ch, input_str = imgui.input_text("##tuple", str_value)
    #     if ch:
    #         try:
    #             new_tuple = eval(f"({input_str},)")
    #             if isinstance(new_tuple, tuple):
    #                 input_value = new_tuple
    #                 changed = True
    #         except Exception:
    #             pass
    # else:
    #     changed, input_value = draw_collection(input_value=input_value)

    return changed, input_value


class TestClass(DictConversion):
    def __init__(self):
        super().__init__()
        self.value = 2
        self.str_val = "Test"


@render_func
def draw_float_ctx(input_value):
    imgui.text('Float content menu')
    imgui.dummy(30, 30)
    draw_float(0.0, name="test")
    imgui.text(f"WxH {input_value.width} {input_value.height}")
    imgui.text(f"Content width {input_value.content_width} {input_value.height}")

    imgui.text(f"Abs Left/Top {input_value.abs_left} {input_value.abs_top}")
    imgui.text(f"Header WxH {input_value.header_width} {input_value.header_height}")

    draw_list: _DrawList = imgui.get_overlay_draw_list()
    draw_list.add_rect(upper_left_x=input_value.abs_left, upper_left_y=input_value.abs_top,
                       lower_right_x=input_value.abs_left + input_value.width,
                       lower_right_y=input_value.abs_top + input_value.height,
                       col=imgui.get_color_u32_rgba(1, 0, 0, 0.5), thickness=1.0)


@render_func(is_default_for=(float), shadow=False, use_cache=False, wrap=False,
             is_tree=False, with_header=draw_header, align_header=True, temp=True)
def draw_float(input_value: float,
               draw_state,
               wrap=False,
               min_width=80,
               max_height=100, min_height=20,
               min_value=-98.703,
               max_value=99.264,
               speed=0.0042):

    if not wrap:
        imgui.set_next_item_width(draw_state.content_width)
    else:
        imgui.set_next_item_width(min_width)
    changed, value = imgui.drag_float("", input_value,
                                      format='%.3f',
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)

    if changed:
        return True, value

    return False, input_value


@render_func(shadow=False, use_cache=False, wrap=False, is_tree=False,
             with_header=draw_header, align_header=True, temp=True)
def draw_button(input_value="", draw_state=None, label="", tint=(1.0, 1.0, 1.0, 1.0), min_width=80,
                min_height=14, wrap=False, height=23):
    """A VALUE-ROW button — draw_float's shape with a button where the slider
    sits, so it composes with the standard header (name label, tint, layout)
    that `def button` fights. `changed` IS the click; input_value passes
    through untouched (the caller acts on the click, e.g. the info tab's +
    stamping a param into a source).

    Styled from draw_state.tint (raw imgui.button's stock style reads pure
    white in the melty theme). The label wraps: it's drawn OVER a label-less
    button with a text-wrap pos, and the button height grows to fit."""
    w = min_width if wrap else (draw_state.content_width or min_width)
    _t = tint
    r, g, b = (_t[:3] if isinstance(_t, (tuple, list)) and len(_t) >= 3
               else (0.45, 0.47, 0.55))
    pad_x, pad_y = 6, 2
    _lbl = str(label)
    ts = imgui.calc_text_size(_lbl, wrap_width=max(10.0, w - pad_x * 2))
    h = height or max(min_height, ts[1] + pad_y * 2)

    imgui.push_style_color(imgui.COLOR_BUTTON, r * 0.28, g * 0.28, b * 0.28, 0.6)
    imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, r * 0.42, g * 0.42, b * 0.42, 0.8)
    imgui.push_style_color(imgui.COLOR_BUTTON_ACTIVE, r * 0.55, g * 0.55, b * 0.55, 0.95)
    pos = imgui.get_cursor_screen_pos()
    # Shadow on the button rect only - the default shadow (decorator
    # shadow=True) would mark the whole value row, name label included.
    add_shadow((pos[0], pos[1], w, h),
               corner_radius=imgui.get_style().frame_rounding)
    clicked = imgui.button("##btn", width=w, height=h)
    imgui.pop_style_color(3)

    # Wrapped label over the button; restore the flow cursor after.
    flow = imgui.get_cursor_screen_pos()
    imgui.set_cursor_screen_pos((pos[0] + pad_x, pos[1] + pad_y))
    imgui.push_text_wrap_pos(imgui.get_cursor_pos_x() + w - pad_x * 2)
    imgui.text(_lbl)
    imgui.pop_text_wrap_pos()
    imgui.set_cursor_screen_pos(flow)
    return clicked, input_value


@render_func(is_default_for=(Parameter), wraps=render_func, with_header=draw_header)
def draw_parameter(input_value):
    parameter_default = input_value.default
    if parameter_default is inspect.Parameter.empty:
        imgui.same_line()
        imgui.text("<No Default>")
    else:
        return draw_any(parameter_default, show_name=False, show_add_delete=False)


@render_func(is_default_for=(types.MappingProxyType), shadow=False, show_bg=False, show_add_delete=False,
             with_header=draw_header)
def draw_mapping_proxy(input_value):
    # To list first, then back to mapping proxy
    try:
        dict_values = dict(input_value)
        changed, new_dict = draw_collection(dict_values, show_bg=False, indent_size=0, show_header=False,
                                            show_add_delete=False)
        if changed:
            return True, types.MappingProxyType(new_dict)

    except Exception as e:
        imgui.text(f"Error converting MappingProxyType to dict: {e}")
        return False, input_value

    return changed, input_value


@render_func(wraps=render_func, show_add_delete=False, with_header=draw_header)
def eval_function(input_value, draw_state):
    signature = inspect.signature(input_value)
    params = signature.parameters
    changed, new_val = draw_any(params, name="Parameters", show_add_delete=False)
    if changed:
        set_fn_defaults(input_value, new_val)

    push_style_var(imgui.STYLE_ITEM_SPACING, (2, 4))
    push_style_var(imgui.STYLE_FRAME_PADDING, (8, 6))
    push_style_var(imgui.STYLE_FRAME_ROUNDING, 6)

    function_args = inspect.signature(input_value).parameters
    kwargs = {}
    for name, param in function_args.items():
        if param.default is not inspect.Parameter.empty:
            kwargs[name] = param.default
        else:
            kwargs[name] = None
    try:
        result = input_value(**kwargs)
        draw_any(result, name="Result", show_header=True, show_add_delete=False)
        if draw_state._result != result:
            Core.melty.cache.invalidate_all()
        draw_state._result = result

    except Exception as e:
        print(f"Error calling function '{input_value.__name__}': {e}")
        print_colored_traceback(*sys.exc_info())

    pop_style_var(3)

    return changed, input_value


@render_func(is_default_for="LSDStudio", show_bg=True,
             auto_resize=True, min_width=218,tint=(0.6, 0.2, 0.8),
             with_header=draw_header)
def draw_lsd_studio(input_val):
    imgui.text("An LSD Studio Instance")


@render_func(is_default_for="ImGuiStyleManager", tint=(0.8, 0.7, 0), use_cache=True, with_header=None)
def draw_style_manager(input_val):
    imgui.text("Style Manager")
   
    return False, input_val


@render_func(is_default_for="Melty", use_cache=True, with_header=None)
def draw_vis(input_val):
    imgui.text("Melty")
    return False, input_val


@render_func(is_default_for="AppModel", show_bg=True, tint=(0.6, 0.2, 0.8), with_header=draw_header)
def draw_app_model(input_val):
    imgui.text("An App Model Instance")

def _format_run_error(exc):
    """One compact, UI-ready error: `Type: message`, then the deepest
    traceback frame in PROJECT code (site-packages/stdlib frames are where
    the error SURFACED, not where it's fixable) with its source line."""
    import traceback
    frames = traceback.extract_tb(exc.__traceback__)
    target = None
    for fr in reversed(frames):
        if "site-packages" not in fr.filename and "/lib/python" not in fr.filename:
            target = fr
            break
    if target is None and frames:
        target = frames[-1]
    text = f"{type(exc).__name__}: {exc}"
    if target is not None:
        text += f"\n{Path(target.filename).name}:{target.lineno} in {target.name}"
        if target.line:
            text += f"\n    {target.line}"
    return text


@render_func(is_default_for=(types.FunctionType, types.MethodType), z_offset=0, use_cache=True, 
             show_add_delete=False, selectable=False, show_bg=True,
             parent_show_add_delete=False, is_tree=False, show_name=False, with_header=draw_header)
def draw_function(input_value, name, draw_state, unique, auto_run=None, wrap=False,
                  show_run_button=True, run_in_thread=False, result_fade_frames=None,
                  **kwargs):
    """`auto_run`: opt-in compile-and-run — pass any comparable version token
    (e.g. id(fn.__code__)); the function runs whenever the token CHANGES or a
    parameter is edited, no button click. The token is stored before running
    so a throwing function doesn't retry every frame. `show_run_button=False`
    drops the named run button (the streamlined live-lab look).


    `run_in_thread=True` runs the function on a daemon worker instead of
    blocking the render loop (long model passes). Single-flight: a click or
    auto_run while a run is in flight is skipped — but the auto_run token is
    only latched when a run actually starts, so a hotswap landing mid-run
    re-fires on completion instead of being lost. The worker only writes
    draw_state attrs and uses the cross-thread invalidation path (the
    Background.run completion pattern); all rendering stays on the GL thread."""
    if not callable(input_value):
        imgui.text("Not a callable function")
        return False, input_value
    params_edited = False
    try:
        signature = inspect.signature(input_value)
        params = signature.parameters
        if len(draw_state.params) != len(params):
            param_dict = {}

            for name, param in params.items():
                if name == 'kwargs':
                    continue
                if param.default is not inspect.Parameter.empty:
                    param_dict[name] = param.default
                else:
                    param_type = param.annotation
                    default_value = param.default
                    if default_value is not inspect.Parameter.empty:
                        param_dict[name] = default_value
                    else:
                        if name in Core.melty.global_attrs:
                            param_dict[name] = Core.melty.global_attrs[name]

            draw_state.params = param_dict
        if len(draw_state.params) > 0:
            from src.lsd.gl_gui.view.mode import Mode
            changed, new_val = draw_collection(draw_state.params, name="Parameters", initial={"expanded":True}, use_cache=True,
                                               mode=Mode.FUNCTION_PARAMS,
                                               show_add_delete=False, shadow=True, z_offset=1, parent_show_add_delete=False, 
                                               horizontal=False, wrap=wrap,
                                               child_kwargs={"max_width": 397, "shadow":False,
                                                             "show_bg": False, "use_cache": True, "z_offset": 0.0
                                                             }, tint=(0.34, 0.40, 0.45))
            if changed:
                draw_state.params = new_val
                params_edited = True
    except Exception as e:
        imgui.text(f"Error inspecting function parameters: {e}")
        draw_state.params = {}

        sees_this = 0

    def _run():
        if run_in_thread:
            if draw_state.misc.get("_run_busy"):
                return  # single-flight: one run per runner at a time
            draw_state.misc["_run_busy"] = True
            # Snapshot params so a mid-run edit can't give the worker a
            # half-updated dict; never run a @render_func WRAPPER off-thread
            # (it mutates process-global Melty stacks - see run_in_background),
            # otherwise take the bare function.
            params = dict(draw_state.params)
            fn = getattr(input_value, "__wrapped__", input_value)

            def _worker():
                try:
                    draw_state.result = fn(**params)
                    draw_state.misc["_result_frame"] = Melty.frame_count
                    draw_state.misc.pop("_run_error", None)
                except Exception as e:
                    draw_state.misc["_run_error"] = _format_run_error(e)
                    print(f"Error calling function '{input_value.__name__}': {e}")
                    print_colored_traceback(*sys.exc_info())
                finally:
                    draw_state.misc.pop("_run_busy", None)
                    # invalidate_up_current reads the live render stack - only
                    # valid mid-render on the GL thread. Off-thread completion
                    # marks the runner's subtree by tile id (force: the result
                    # pane is a cached descendant) and wakes the loop; the
                    # validation itself happens on the render thread.
                    from src.lsd.gl_gui.view.invalidation_tracker import Note
                    Melty.cache.invalidate_up(
                        draw_state._tile_id, force=True,
                        note=Note(name="draw_function run complete",
                                  reason=f"func={input_value.__name__}",
                                  tint=(0, 0, 1)))
                    request_render()

            threading.Thread(target=_worker, daemon=True,
                             name=f"draw_function:{input_value.__name__}").start()
            Core.melty.cache.invalidate_up_current(force=True)  # show spinner now
            request_render()
            return
        try:
            draw_state.result = input_value(**draw_state.params)
            draw_state.misc["_result_frame"] = Melty.frame_count
            draw_state.misc.pop("_run_error", None)
            Core.melty.cache.invalidate_up_current(force=True)
        except Exception as e:
            # Surfaced in the UI (red text where the result goes), anchored at
            # the deepest frame in PROJECT code - the line the user can fix.
            draw_state.misc["_run_error"] = _format_run_error(e)
            Core.melty.cache.invalidate_up_current(force=True)
            print(f"Error calling function '{input_value.__name__}': {e}")
            print_colored_traceback(*sys.exc_info())

    busy = run_in_thread and draw_state.misc.get("_run_busy")
    # One-offed run request (draw_function_live's Ctrl+Enter - the
    # hotkey IS the Run button): always popped, so it can't replay on later
    # frames; dropped while busy, matching a click during a threaded run.
    if draw_state.misc.pop("_run_requested", None) and not busy:
        _run()
    if auto_run is not None and not busy and (
            params_edited or draw_state.misc.get("_auto_run_ver") != auto_run):
        draw_state.misc["_auto_run_ver"] = auto_run
        _run()


    imgui.new_line()
    if show_run_button and button(f"Run {input_value.__name__}()##{unique}", icon=kwargs.get("icon", ""), height=35,
                                  bg_offset=0, tint=(0.499, 0.844, 0.488, 0.32), shadow=True, rounding=None)[0]:
        _run()

    # Fading result (result_fade_frames): the check mark + result text hold,
    # then fade out and clear - modeled on code_file_io's recompile_status
    # (frame-based fade, invalidate + request_render pump while fading).
    # None (default) keeps the persistent result pane.
    result_fade = 1.0
    if result_fade_frames and draw_state.result is not None:
        shown_for = float(Melty.frame_count
                          - draw_state.misc.get("_result_frame", Melty.frame_count))
        result_fade = min(1.0, max(0.0, 2.0 - shown_for / float(result_fade_frames)))
        if result_fade > 0.01:
            draw_state.invalidate()
            request_render()
        else:
            draw_state.result = None
            draw_state.misc.pop("_result_frame", None)

    if run_in_thread and draw_state.misc.get("_run_busy"):
        imgui.same_line(spacing=10)
        imgui.text_colored("", 0.55, 0.75, 1.0, 1.0)
        imgui.new_line()
    elif draw_state.result is not None:
        imgui.same_line(spacing=10)
        imgui.text_colored("", 0.55, 0.75, 1.0, result_fade)
        if result_fade_frames and isinstance(draw_state.result, str):
            # Fading summary rides the button row inline, so hiding it never
            # reflows the content below (the row holds its height).
            imgui.same_line(spacing=8)
            imgui.text_colored(draw_state.result, 0.55, 0.75, 1.0, result_fade)
        imgui.new_line()
    else:
        imgui.same_line()
        imgui.text_colored(" ", 0.55, 0.75, 1.0, 1.0)
        imgui.new_line()

    run_error = draw_state.misc.get("_run_error")
    if run_error:
        imgui.push_text_wrap_pos(0.0)
        imgui.text_colored(run_error, 1.0, 0.45, 0.40, 1.0)
        imgui.pop_text_wrap_pos()

    if draw_state.result is not None and not (result_fade_frames
                                              and isinstance(draw_state.result, str)):
        imgui.text_colored(" Result", *(1.0, 1.0, 1.0, 0.5))
        imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() - 15)
        draw_any(draw_state.result, name="Result", header_same_line=True, show_header=False, 
                 show_add_delete=False)

    # pop_style_var(3)

    return False, input_value


@render_func(is_default_for=(int), shadow=False, use_cache=False, wrap=False,
             is_tree=False, with_header=draw_header, align_header=True, temp=True)
def draw_int(input_value: int, draw_state=None, max_height=100, min_height=20,
             min_width=80, wrap=False, min_value=-1000.0, 
             max_value=1000.0, speed=0.1, unique=0):
    if not wrap:
        imgui.set_next_item_width(draw_state.content_width)
    else:
        imgui.set_next_item_width(min_width)

    max_int = 2147483647
    if input_value < max_int:
        changed, value = imgui.drag_int("##int", input_value,
                                        change_speed=speed,
                                        min_value=min_value,
                                        max_value=max_value)
        if changed:
            return True, value

        return changed, value
    return False, input_value


@render_func(show_header=False, show_name=False, show_bg=True, with_header=draw_header)
def draw_debug_label(input_value: str):
    imgui.text(input_value)


@render_func(is_default_for=Enum, is_tree=False, shadow=False, align_header=False,
             selectable=False, show_add_delete=False,
             parent_show_add_delete=False, with_header=draw_header, temp=True)
def draw_enum(input_value: Enum, draw_state=None, unique=0, style_manager=None, enum_tint=(0.3, 0.3, 0.3)):
    # Delegate to draw_tab_bar so enums get its wrapping + styling for free.
    # Enums are single-select: pass the current value as the lone selection and
    # render every member as a tab; names are the prettified member names.
    options = list(input_value.__class__)

    if len(options) > 4:
        changed, selection = draw_dropdown(input_value, collection=options, show_header=False,
                                           name=f"{input_value.__class__.__name__}##{unique}enum")

        if changed:
            return True, selection
    else:

        names = [opt.name.replace("_", " ").capitalize() for opt in options]
        changed, selected = draw_tab_bar([input_value], collection=options, names=names, name=f"{unique}_enum", wrap=True,
                                          z_offset=-1, rounding=5, as_toggles=False, bg_offset=-3)
        if changed and selected:
            return True, selected[0]
    return False, input_value


@render_func(is_tree=False, show_bg=True, shadow=False, use_cache=False, header_same_line=True,
             disable_scroll=True,
             indent_size=0, show_add_delete=False, 
             show_name=False, selectable=False, parent_show_add_delete=False,
             with_header=draw_header)
def draw_tab_bar(input_value: list, tab_height=30, names=None, tint_value=0.235, tint_saturation=0.372, unique=None,
                 collection=None, as_toggles=False, tints=None, icons=None, excluded=None, width=None,
                 dnd_collection_ds=None, dnd_keys=None,
                 draw_state=None):
    """Tab bar with multi-select via shift-click. input_value is the list of selected items, collection is all available tabs.
    Tabs wrap onto a new row when the cumulative width would exceed draw_state.content_width.

    tints: optional list of (r, g, b) tint colors, one per tab in `collection`. Entries that are
    None (or beyond the list) fall back to the neutral grey. (Defaults to None rather than [] to
    avoid the mutable-default-arg pitfall; behaves identically to an empty list.)

    dnd_collection_ds + dnd_keys: opt tabs into the framework DragDrop. Each
    tab button registers as a whole-rect drag item of `dnd_collection_ds` (the
    draw_state whose input_value is the collection being reordered — the
    Reorder/Insert lands on ITS return via Melty.dnd_requests). dnd_keys is a
    list parallel to `collection`: (key, collection_index) per tab, or None
    for tabs that aren't draggable (e.g. a synthetic General tab). The owner
    must stamp _dnd_drop_target/_dnd_horizontal on dnd_collection_ds so slot
    lines render vertically between the tabs."""
    if collection is None:
        return False, input_value
    
    
        
    if excluded is None: 
        excluded = set()
    # Content-left edge, captured before the dummy/same_line/-10 shift below.
    # The wrap limit is measured from here so it lines up with content_width.
    origin_x = imgui.get_cursor_screen_pos()[0]

    imgui.dummy(0,0)
    imgui.same_line()

    # [tint=(0.894, 0.568, 0.204, 1.0), show_tint=True]
    io = imgui.get_io()
    changed = False
    selected = list(input_value)
    if selected is None:
        selected = []
    if names is None and hasattr(input_value, 'keys') and hasattr(input_value, 'values'):
        input_value = list(input_value.values())
        names = list(input_value.keys())

    imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] - 10, imgui.get_cursor_screen_pos()[1]))
    # First-row start x after the -10 shift; wrapped rows realign to this
    # (new_line() alone resets to the window content x, which is ~10px right).
    row_start_x = imgui.get_cursor_screen_pos()[0]

    # Mirror button()'s sizing: width = calc_text_size(label_text).x + text_pad (15).
    # Scaled like button() scales its text_pad, so the wrap measurement below
    # matches the width the buttons actually take.
    # [tint=(0.124, 0.65, 0.087, 1.0), show_tint=True]
    button_padding = Melty.px(15)
    # tab_height arrives as an authored-at-1.0 constant (callers pass 30/40),
    # so it scales here - once, up front, so the button, the click test and
    # the drag placeholder below all agree on one height. The drag path
    # deliberately overrides this with the MEASURED pickup height, which is
    # already in real pixels.
    tab_height = Melty.px(tab_height)
    content_width = draw_state.content_width if draw_state is not None else 0
    x_limit = origin_x + content_width if content_width > 0 else None

    # An explicitly passed width is a real constraint content_width knows
    # nothing about (the context menu passes width=content_width-282) — clamp
    # to it. draw_state.width and abs_clip_rect are NOT constraints here: with
    # wrap=True the wrapper writes the measured content extent back into
    # draw_state.width (and the clip rect derives from it), so clamping to
    # them ratchets the bar narrower on every reflow until each tab fits on
    # its own row.
    if width and draw_state is not None:
        view_right = draw_state._abs_left() + width - 3
        x_limit = view_right if x_limit is None else min(x_limit, view_right)

    def _tab_text(t):
        return t.name if hasattr(t, 'name') else str(t)

    for i, tab in enumerate(collection):
        raw = _tab_text(tab)
        # label_text = raw.replace("_", " ")
        
        if i in excluded:
            continue
        label = f"{raw}"
        if names is not None and i < len(names):
            label = f"{names[i]}"
        # Icons parallels `collection` like tints; prefix before the## so the
        # imgui id (and click identity) stays keyed on the bare tab name.
        if icons is not None and i < len(icons) and icons[i]:
            label = f"{icons[i]} {label}"
        active = tab in selected

        tab_color = (0.5, 0.5, 0.5)
        tinted = tints is not None and i < len(tints) and tints[i] is not None
        if tinted:
            tab_color = tints[i]

        new_value = 0.15 if not tinted else 0.1

        # make_color_rgb mixes `color` toward the theme color by `factor`; factor=1.0 (button's
        # default) discards `color` entirely. Drop factor for tinted tabs so the tint shows, and
        # give inactive tinted tabs a faint fill (the default alpha=0.0 draws no rect at all).
        tab_factor = 0.30 if tinted else 1.2

        tab_width = imgui.calc_text_size(label.split("##")[0]).x + button_padding

        # Wrap before drawing: the previous iteration's same_line() left the
        # cursor at this tab's real start (with item spacing included), so
        # comparing live cursor + width against the content right edge needs
        # no estimated spacing or fudge factor.
        if i > 0 and x_limit is not None and imgui.get_cursor_screen_pos()[0] + tab_width > x_limit:
            imgui.new_line()
            imgui.set_cursor_screen_pos((row_start_x, imgui.get_cursor_screen_pos()[1]))

        # Framework drag-and-drop: this button acts as a whole-rect drag
        # item of dnd_collection_ds (key= is what DragDrop._begin picks up).
        # While IT is the dragged child it renders with the floating-window
        # kwargs (detached, fixed to pickup size, glued to the cursor) and a
        # plain dummy holds its slot open in the flow.
        dnd = None
        dragged = False
        dnd_extra = {}
        if dnd_collection_ds is not None and dnd_keys is not None and i < len(dnd_keys):
            dnd = dnd_keys[i]
        _tx, _ty = imgui.get_cursor_screen_pos()
        btn_height = tab_height
        if dnd is not None:
            dnd_extra = {"key": dnd[0], "dnd_handle": True, "return_extras": True}
            # Same (collection, key) can be carried by two views - this button
            # and the tab's stacked content view (draw_collection_as_tabs
            # multi-select). Only the view actually picked up detaches, so
            # require the dragged item to BE this button's draw state.
            dragged = (_drag_drop.DragDrop.is_dragged_child(dnd_collection_ds, dnd[0])
                       and _drag_drop.DragDrop.item_ds is dnd_collection_ds._children.get(dnd[1]))
            if dragged:
                dnd_extra.update(_drag_drop.DragDrop.dragged_item_kwargs())
                # dragged_item_kwargs pins width/height to the pickup size -
                # height would collide with the explicit height= below, so
                # route it through btn_height (the pinned button size).
                btn_height = dnd_extra.pop("height", btn_height)

        if dnd is not None:
            # dnd: tabs - legacy @render_func buttons: DragDrop needs a
            # per-tab draw_state to register as the drag child (pickup,
            # floating window, slot placeholder). Only the context menu's
            # reorderable tab bar takes this branch.
            if active:
                selected_value = 0.23
                btn_res = button(label, z_offset=2, name=f"tab_{i}_{unique}",
                                 height=btn_height, tint_value=new_value + selected_value - 0.03,
                                 color=tab_color, factor=tab_factor, draw=True, **dnd_extra)
            else:
                saturation = 1.0 if tinted else 0.3
                btn_res = button(label, indent_size=0, height=btn_height, draw=True, z_offset=0.0,
                                 alpha=0.0 if tinted else 0.0, tint_value=new_value if not tinted else 0.1, saturation=saturation,
                                 name=f"tab_{i}_{unique}_deactivated", color=tab_color, factor=tab_factor,
                                 text_value=1.0 if not tinted else 0.9,
                                 shadow=False, **dnd_extra)
            # A draggable tab must not change the selection on mouse-DOWN (a
            # drag pickup would eat a multi-select). Override the button's
            # down-click and select on CLICKED instead: the input handler only
            # passes it for a release within MOVE_MAX_DISTANCE of the press,
            # so a press that becomes a drag never selects.
            clicked = False
            if not dragged and draw_state is not None:
                clicked = draw_state.on_action(
                    "left_mouse_clicked", view_id=f"tab_click_{i}",
                    rect=(_tx, _ty, _tx + tab_width, _ty + tab_height),
                    priority_delta=2) is not None
            btn_ds = btn_res[2] if len(btn_res) == 3 else None
            if btn_ds is not None:
                # Fulfill the drop-collection contract on the OWNER's ds (see
                # DragDrop._is_drop_collection): children keyed by collection
                # index, backlinked for register_item/_begin.
                btn_ds._collection_draw_state = dnd_collection_ds
                if dnd_collection_ds._children is None:
                    dnd_collection_ds._children = {}
                dnd_collection_ds._children[dnd[1]] = btn_ds
            if dragged:
                # The dragged button deferred to a floating window and drew
                # nothing inline - hold its slot open at the current flow
                # position so the bar doesn't reflow mid-drag.
                imgui.dummy(tab_width, tab_height)
                clicked = False
        else:
            # Plain tabs: draw-list rendering (flat_button - the fast-dock
            # model). The per-tab @render_func buttons cost ~0.7ms each in
            # wrapper machinery alone; on the editor's 8-tab strip that was
            # the largest single slice of the hovered frame. Visual parity
            # with the old button params: active tabs get the filled rect,
            # inactive draw label-only (alpha=0), hover brightens the text
            # like the button's hovered text_value boost. Framework
            # shadows/z-offset on the active tab are gone (no draw_state) -
            # the fast-dock tradeoff.
            if active:
                # flat_button applies its own shadow when it draws a bg (the
                # inactive alpha=0 tabs stay flat).
                clicked = flat_button(
                    label, draw_state, view_id=f"tab_{i}",
                    width=tab_width, height=btn_height,
                    color=tab_color, factor=tab_factor,
                    tint_value=new_value + 0.23 - 0.03)
            else:
                clicked = flat_button(
                    label, draw_state, view_id=f"tab_{i}",
                    width=tab_width, height=btn_height,
                    color=tab_color, factor=tab_factor, alpha=0.0,
                    tint_value=new_value if not tinted else 0.1,
                    saturation=1.0 if tinted else 0.3,
                    text_value=1.0 if not tinted else 0.9)

        if clicked:
            changed = True
            if io.key_shift or as_toggles:
                if active:
                    selected.remove(tab)
                else:
                    selected.append(tab)
            else:
                selected = [tab]

        same_line()

    imgui.dummy(0, 0)

    if changed:
        Core.melty.refresh_nested_windows(draw_state)

    return changed, selected


@render_func(with_header=draw_header, is_tree=False, shadow=False)
def draw_enum_tabs(input_value: type, tab_state: TabState):
    enum_states = list(input_value)
    selected = tab_state.selected_tabs

    changed, new_selected = draw_tab_bar(selected, collection=enum_states)
    if changed:
        tab_state.selected_tabs = new_selected

    return False, input_value


def draw_debug(x, y, label, color=(1, 0, 0), size=16):
    draw_list: _DrawList = imgui.get_overlay_draw_list()
    draw_list.add_circle_filled(x, y, size, imgui.get_color_u32_rgba(*color, 1.0))
    draw_list.add_text(x + size + 2, y - size / 2, imgui.get_color_u32_rgba(*color, 1.0), label)


def draw_lens(lens, draw_state):
    """Render a single Lens against draw_state: resolve its root, then either
    focus the live leaf in place (in-place kinds) or run its generated
    parse→focus→save chain (code kinds). Returns (changed, _)."""
    from src.lsd.gl_gui.view.core_conversion.chain_converters import focus
    root = lens.root(draw_state)
    if root is None:
        imgui.text_colored(f"{lens.kind or lens.label}: n/a here", 0.5, 0.5, 0.5)
        return False, root
    if lens.chain is None:
        return focus(root, path=lens.path, default=lens.default, kind=lens.kind, name=lens.label + lens.name)
    return draw_any(root, chain=lens.chain(root), name=lens.label)


@render_func(use_cache=True, show_bg=True, selectable=False)
def draw_tint_context(input_value: DrawState, tab_state: TabState = None, **kwargs):

    if Toggles.debug_set_anywhere:
        source_name = get_source_for("tint", input_value)
        imgui.text(f"Tint source: {source_name}")

    # The live framework-resolved tint - except mid-round-trip, when the
    # pending UI value shows so the widget doesn't snap back while the
    # write→save→hotswap frames play out (anywhere_value).
    tint_changed, tint_value = draw_tuple(anywhere_value("tint", input_value), name="Tint: set anywhere", height=30,
                                          width=40,  
                                          show_name=False, show_header=False)
    if tint_changed:
        set_anywhere("tint", tint_value, input_value)

    return False, None


@window
@render_func()
def context_menu_settings(input_value, draw_state):
    code_file_io(draw_context_menu, mode=Modes.NEW_CODE)


def run_scoped_eval(code, view_func, draw_state, local_vars):
    """Run `code` with the view function's ACTUAL call-time locals in scope.

    Called from the render wrapper (core_render) right before it invokes the
    view function, so `local_vars` is the exact set of arguments the function is
    about to receive -- its initial locals (`input_value`, `draw_state`, and
    every kwarg by name). On top of those we layer the function's module globals
    (so the snippet resolves the same free names the body would) plus the `value`
    and `ds` aliases. Locals win over globals, mirroring normal scoping.

    Delegates to the same eval/exec + stdout-capture machinery the MCP
    `eval_python` tool uses, so a trailing expression's repr comes back alongside
    any printed output. The namespace is a fresh dict layered over a *copy* of
    the module globals, so assignments in the snippet don't leak back into the
    module.
    """
    from src.lsd.gl_gui.mcp_eval import _run_code
    ns = {}
    if view_func is not None:
        # Same free names the function body resolves.
        ns.update(getattr(view_func, "__globals__", {}))
    if local_vars:
        ns.update(local_vars)  # the view function's call-time locals
    ns.setdefault("draw_state", draw_state)
    ns.setdefault("ds", ns.get("draw_state"))
    ns.setdefault("value", ns.get("input_value"))

    # Record the EXACT eval scope (the function's call-time locals + the ds/value
    # aliases) into the per-function metadata cache, so the eval tab's autocomplete
    # gives type-accurate suggestions on the next load. Keyed on the wrapper
    # (draw_state._view_func) to match what the eval tab looks up. Globals aren't
    # recorded -- they're resolved live from the function's __globals__ at
    # completion time. Best-effort; a hiccup here must never fail the eval.
    try:
        from src.lsd.gl_gui.func_metadata import FuncsMetadata
        cache_key = getattr(draw_state, "_view_func", None) or view_func
        scope = dict(local_vars) if local_vars else {}
        for alias in ("draw_state", "ds", "value", "input_value"):
            scope[alias] = ns.get(alias)
        FuncsMetadata.record(cache_key, scope)
    except Exception:
        pass

    out, result, error = _run_code(code, ns)
    parts = []
    if out.strip():
        parts.append(out.rstrip())
    if result is not None:
        parts.append("=> " + result)
    if error:
        parts.append(error.rstrip())
    return "\n".join(parts) if parts else "(no output)"


# ── Context menu tabs ────────────────────────────────────────────────────────
# Each tab body is its own render_func. draw_context_menu places one per selected
# column by calling it with column=t_idx; the tab then owns a single-column
# region, so the inner views inside it no longer pass column themselves.

class _SourceItem(str):
    """A source name as a dropdown value. `.tint` colors its row and the
    trigger via _dd_obj_tint — yellow marks the source actively driving
    the param."""

    def __new__(cls, name, tint=None):
        self = str.__new__(cls, name)
        self.tint = tint
        return self


_ACTIVE_SRC_TINT = (0.9, 0.8, 0.2)


@render_func(use_cache=True, show_bg=False, show_header=False, disable_scroll=False,
             searchable=True, show_name=False, selectable=False)
@window(tint=(0.767, 0.671, 0.183))
def draw_info_tab(input_value, search_text='', draw_state=None, unique=None, **kwargs):
    """One row per view param: a source dropdown for LOOKING at the different
    input sources, plus the value stored at the selected source — editable
    when that source is writable, an inline + when it doesn't set the param
    there yet. The dropdown defaults to the source actively driving the
    param (yellow row/trigger); switching it never deletes or moves
    anything, it just changes which source you're viewing/editing.
    Selection lives on THIS tab's draw_state (misc) — pure view state."""
    from src.lsd.gl_gui.view.core_views.anywhere import (_ABOVE_DRAW_STATE,
                                                         _unset_value)
    target = input_value
    # locate_all_params: the view's own params PLUS the header's
    # (with_header function inputs - icon, show_name, name_color, etc),
    # deduped, view params first. Same read/write semantics.
    proxy = target.locate_all_params
    srcs = _sources_for(target)
    writable = set(srcs["writable"])

    # ONE pass over the registry per render - the active source is simply
    # the highest-priority writable source with a SET value. The old shape
    # re-walked every source dict per PARAM (_setting_source per row, plus
    # the default_write_source cue re-counting key overlaps), which is
@render_func(use_cache=True, show_bg=False, show_header=False, disable_scroll=False,
             searchable=True, show_name=False, selectable=False)
@window(tint=(0.767, 0.671, 0.183))
def draw_info_tab(input_value, search_text='', draw_state=None, unique=None, **kwargs):
    """One row per view param: a source dropdown for LOOKING at the different
    input sources, plus the value stored at the selected source — editable
    when that source is writable, an inline + when it doesn't set the param
    there yet. The dropdown defaults to the source actively driving the
    param (yellow row/trigger); switching it never deletes or moves
    anything, it just changes which source you're viewing/editing.
    Selection lives on THIS tab's draw_state (misc) — pure view state."""
    from src.lsd.gl_gui.view.core_views.anywhere import (_ABOVE_DRAW_STATE,
                                                         _unset_value)
    target = input_value
    # locate_all_params: the view's own params PLUS the header's
    # (with_header function inputs - icon, show_name, name_color, etc),
    # deduped, view params first. Same read/write semantics.
    proxy = target.locate_all_params
    srcs = _sources_for(target)
    writable = set(srcs["writable"])

    # ONE pass over the registry per render - the active source is simply
    # the highest-priority writable source with a SET value. The old shape
    # re-walked every source dict per PARAM (_setting_source per row, plus
    # the default_write_source cue re-counting key overlaps), which is
    # O(params × sources × keys) through lazy bubbling parse wrappers.
    _prio = {s: _source_priority(srcs["kinds"].get(s)) for s in srcs["sources"]}
    _ordered_all = sorted(srcs["sources"], key=_prio.get)
    active_map = {}      # param -> highest-priority setter
    setters_map = {}     # param -> [every source setting it, priority order]
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

    # ── Search - the DEFAULT search path. Term resolution mirrors
    # draw_search: a injected str/SearchTerm in search_text (the menu's
    # search box / an ancestor session), or this tab's OWN find-bar term -
    # the search_text kwarg is only injected from an ancestor session, so a
    # self-triggered Ctrl+F never arrives through it and must be read off
    # draw_state.search_text directly.
    _term = search_text or (draw_state.search_text if draw_state.search_active else "")
    if isinstance(_term, SearchTerm):
        _session = _term
    elif draw_state.search_active and draw_state._search_session is not None:
        _session = draw_state._search_session
    else:
        _session = None
    term = str(_term).lower() if _term else ""

    # Param NAMES are matched by THIS view - one result slot per matching
    # row, in draw order. Rows without a value widget (the cue rows, "not set
    # here") have no child matcher of their own, so without this claim they
    # are invisible to the search count/highlight/nav. Child editors still
    # claim their content matches separately (no double count: names here,
    # content there).
    # NB: `for p in proxy` yields (param, value) PAIRS (ParamProxy's one
    # weird deviation from dict) - keys() yields plain keys.
    _param_names = tuple(p.lower() for p in proxy.keys())
    def _search_matcher(t, session, _names=_param_names):
        q = str(t).lower()
        if q:
            session.claim(sum(1 for n in _names if _fuzzy_key_match(q, n)))
    draw_state._search_matcher = _search_matcher

    # The view's pre-body walk (search_walk) stashed the local ordinal of the
    # global-current match on us when one of our names holds it.
    _current_local = draw_state._search_active_local if term else None
    _match_ord = 0
    _current_row_y = None

    any_changed = False
    index = 0
    # Grouped proxy: the view's own params, then the header's, each its
    # own nested live dict (GroupedParamProxy). A dim divider separates the
    # header section; rows are identical either side of it.
    for _glabel, _group in ((None, proxy.get('params')),
                            ('header', proxy.get('header'))):
        if not _group:
            continue
        if _glabel:
            imgui.dummy(0, 6)
            text(_glabel, name=f'grp_{_glabel}', editable=False)
        for param, value in _group.items():
            index = index+1
            # Highlight, don't hide: every row stays visible (like the default
            # search everywhere else), matching rows get the glow via the
            # search_match/search_current kwargs their header reads.
            is_match = bool(term) and _fuzzy_key_match(term, param.lower())
            is_current = (is_match and _current_local is not None
                          and _match_ord == _current_local)
            if is_match:
                _match_ord += 1
            if is_current:
                _current_row_y = imgui.get_cursor_screen_pos()[1]
            _search_kw = {"search_match": is_match, "search_current": is_current}
            if parses_ready:
                # The ACTIVE source: the highest-priority setter (precomputed in
                # active_map, one registry pass) - unless a diverged auto_param
                # outranks it at runtime (the ds beats every setter not in
                # _ABOVE_DRAW_STATE; the ds row only registers whitelisted
                # attrs, so read the dict directly. A bare `x in
                # target.__dict__` would be wrong: DrawState.__init__ stamps its
                # own fields on every instance).
                setting = active_map.get(param)
                ds_has = param in (getattr(target, "auto_params", None) or {})
                if ds_has and (setting is None
                               or _prio[setting][0] not in _ABOVE_DRAW_STATE):
                    active = "draw_state"
                else:
                    active = setting
                _active_cache[param] = active
                known = True
            else:
                # ACTIVE-SOURCE CACHE DISABLED (for A/B): never serve cached
                # picks - loading rows draw no dropdown until sources are live.
                # Re-enable by restoring: known = param in _active_cache;
                # active = _active_cache.get(param)
                known = False
                active = None

            # Names only need to be distinct among SIBLINGS - core_render's
            # push_state scopes them per parent view, so no unique threading.
            row_uid = param

            if known:
                # Dropdown of ALL sources in SourcePriority order, active one
                # tinted + row-wrapped - memoized per distinct active source
                # (_options_for above), not rebuilt per param.
                options, _row_tints = _options_for(active)

                # Selection is per-tabview state; default = the active source.
                sel_key = f"src_sel::{param}"
                sel = draw_state.misc.get(sel_key)
                if sel not in options:
                    if active is not None:
                        sel = active
                    elif parses_ready:
                        sel = _default_source_once()
                    else:
                        sel = "draw_state"

                # Subtle trigger: no button bg/shadow, short, narrow - it's a
                # provenance label with a popover, not a primary control.
                # Per-row trash INSIDE the popover: clear the param at THAT
                # source without closing the list, so several sources can be
                # cleared in one visit. Only rows that actually SET the param
                # get one (setters_map, plus the ds when an auto_param diverged);
                # codec rows are excluded - clear_anywhere can't reverse the
                # codec's per-file/render_kwargs fan-out yet.
                def _clear_at(src, _p=param):
                    from src.lsd.gl_gui.view.core_views.anywhere import clear_anywhere
                    if clear_anywhere(_p, target, str(src)) is not None:
                        target.invalidate()
                        draw_state.invalidate()
                _row_actions = {s: _clear_at for s in setters_map.get(param, ())
                                if srcs["kinds"].get(s) != "codec"}
                if param in (getattr(target, "auto_params", None) or {}):
                    _row_actions["draw_state"] = _clear_at
                pick_changed, new_pick = draw_dropdown(
                    options.get(sel, sel), collection=options, width=181, z_offset=0,
                    shadow=False, show_button_bg=False, trigger_height=22, show_bg=False,
                    text_pad=3, row_tints=_row_tints, row_actions=_row_actions,
                    text_toward_bg=_text_toward_bg,
                    name=f"src_{row_uid}_dd{index}", show_header=False)
                if pick_changed and new_pick:
                    sel = str(new_pick)
                    draw_state.misc[sel_key] = sel
            else:
                sel = None
                imgui.dummy(181, 22)     # hold the dropdown slot - no reflowing it yet

            imgui.same_line()

            if not parses_ready:
                # Sources still loading: stored-at-source reads would hit
                # placeholders - bind the widget to the RESOLVED value and route
                # edits through the automatic pick when the sources are real.
                item_return = draw_any(value, name=param,
                                       show_bg=False, show_header=True, **_search_kw)
                item_changed, out_val = item_return[0], item_return[1]
                if item_changed:
                    _group[param] = out_val
                    any_changed = True
                imgui.dummy(0, 2)
                continue

            # The value AT the selected source (not the resolved value) - that's
            # what looking at a dropdown shows. draw_state reads the live attr.
            sdict = srcs["sources"].get(sel)
            stored = sdict.get(param) if isinstance(sdict, dict) else None
            if sel == "draw_state" and stored is None:
                stored = (getattr(target, "auto_params", None) or {}).get(
                    param, target.__dict__.get(param))
            sel_writable = sel in writable or sel == "draw_state"

            # In-flight display cache (_sa_pending - the same one anywhere_value
            # serves): a slow-source write, and EVERY write while a drag is held
            # (deferred), hasn't reached the source host yet - a raw stored read
            # snaps the slider back to the stale value next frame ("stuck").
            # Serve the pending UI value while the trip is in progress, but only
            # when this row's selected source is the one the write targeted.
            # proxy.items() above runs anywhere_value per param, which retires
            # entries once the live value moves beyond their at-set baseline.
            _pending = getattr(target, "_sa_pending", None)
            if _pending and param in _pending:
                _lastsrc = getattr(target, "_sa_last_source", None) or {}
                if _lastsrc.get(param, sel) == sel:
                    stored = _pending[param][0]

            if stored is None:
                if sel_writable:
                    # Selected source doesn't set the param yet: + stamps a value
                    # into it (creating the entry); next frame the widget takes
                    # over. draw_button is the header's value-row button
                    # (draw_float's +) - same header chrome as widget rows.
                    clicked, _ = draw_button("+", name=f"+##add_{row_uid}",
                                             label="+", display_name=param,
                                             show_name=True, show_bg=False,

                                             wrap=True, min_width=24, **_search_kw)
                    if clicked:
                        # Resolved value when there is one; a None (header params
                        # nothing sets) stamps the DECLARED signature default -
                        # stamp with None would create an entry that still reads
                        # as unset (the "+ does nothing" feel).
                        stamp = value
                        if stamp is None:
                            from src.lsd.gl_gui.view.core_views.anywhere import (
                                signature_default_for)
                            stamp = signature_default_for(param, target)
                        set_anywhere(param, stamp, target, allow_any=True,
                                     ds_fallback=True, source=sel)
                        any_changed = True
                else:
                    text(f"{param}: not set here", name=f"ro_{row_uid}",
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
                text(f"{param}: {stored}", name=f"ro_{row_uid}", editable=False,
                     **_search_kw)
            imgui.dummy(0, 2)

        # Scroll the current search match into view - only on real full-search
        # frames (term change not Enter/arrow nav), same gate as draw_collection.
        if (_current_row_y is not None and _session is not None
                and getattr(_session, "scroll_to", False)):
            _scroll_into_view(draw_state, _current_row_y, _current_row_y + 24)

    if any_changed:
        target.invalidate()
        # The rows themselves are cached: re-render so the active tint and
        # stored values reflect the write this frame (parse/dict writes are
        # synchronous; the hosts' notify covers the later reload/hotswap).
        draw_state.invalidate()
        request_render()
    return False, input_value



@render_func(use_cache=True, show_bg=False, show_header=False, show_name=False, selectable=False)
def draw_config_tab(input_value, **kwargs):
    """List the inspected view function's configurable parameters and their
    current values (kwarg override, else signature default)."""
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

#
# # I want to use the order of the enum elements as a way of determining the source of the input.
# # From the frameworks perspective it just sees parameters injected. Melty is routing data from many different sources
# # and does its best to pick sensible defaults when there are multiple sources. Which source takes priority depends on
# # how the code is written, and the code may change. Rather than trying to infer the priority, we are just hard coding it
# # so. I can adjust the order of the elements in the enum to reflect the runtime behavior of Melty. This is just my
# # rough memory of which sources take priority.
# class SourcePriority(Enum):
#     MODE = 0
#     RENDER_FUNC = 1
#     AT_DEFAULT_CODE_TYPE = 2
#     AT_DEFAULT_CODE_TYPE = 3
#     LIVE_COMMENT = 4
#     CALLER_0 = 5 # Caller is a litter more complicated, we need the whole call state as potential sources. Similar thing for mode I think.
#     CALLER_1 = 6 # Hopefully there's a better way than hard coding the caller depth
#
# def get_value_for_source(attr_name, input_source, draw_state):
#
#     value_at_source = None # Do the same thing we do in the input tab, but cleaner
#     return input_source, value_at_source
#
# def set_anywhere(attr_name, value, draw_state):
#
#     # Use the melty routing system to set the value at its source,
#     # be that code, memory, code comments, decoration or enum.
#
#     # The live tab can already figure out which sources exist, so we can easily have a list of potential sources to pull
#     # from.
#     input_matrix = {} # Map possible values for each source, same as input tab
#     sources_that_exist = [SourcePriority.MODE, SourcePriority.RENDER_FUNC] # infer from input matrix
#
#     sorted_by_priority = sorted(sources_that_exist, key=lambda s: s.value) # Sort by the enum value, lower is highest priority
#     top_priority_source = sorted_by_priority[0]
#     input_matrix[top_priority_source] = value # This should update wherever it happens to be, if the framework did its job
#
#     # We could cross compare with the current value in draw_state._kwargs[attr_name] and see if it matches. Not bullet proof but would
#     # catch a bunch of bugs. Don't throw but maybe do a notify()


@render_func(use_cache=True, show_bg=False, live=False, mode=Modes.WINDOW, show_header=False, show_name=False, selectable=False)
def draw_live_tab(input_value, **kwargs):
    """List the inspected view function's configurable parameters and their
    current values (kwarg override, else signature default)."""
    imgui.text("Re-renders view frequently, bad for performance but good for debugging")

    params_to_view = ["unique", ("abs_left", "left_offset"), ("abs_top", "top_offset"), "scroll_offset", ("abs_top_true", "top_offset_true"), ("width", "height"), ("content_width", "content_height"), "layer", "z_offset"]
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


def draw_func_tab(input_value, name=None, disable_scroll=True, width=None,
                  height=None, select_line=None, select_seq=0, **kwargs):
    """Editable source of the inspected view function; hotswaps on save.
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
    view_func = input_value._view_func
    if view_func is not None:
        from src.lsd.gl_gui.view.core_conversion.new_converters import (
            code_file_io, draw_text_from_code_cache)
        view_func_name = view_func.__name__ if hasattr(view_func, '__name__') else str(view_func)
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
    target = input_value  # (possibly walked-up) target's draw_state
    eval_view_func = target._view_func

    # Autocomplete scope. The exact call-time locals are only known once an eval
    # actually fires (core_scoped_eval pulls them then). To give type-aware
    # suggestions BEFORE the first eval, pre-record an initial scope from what
    # the target draw_state already exposes -- input_value/value, draw_state/ds,
    # and every explicit kwarg. core_scoped_eval refines this to exact on first eval.
    from src.lsd.gl_gui.func_metadata import FuncsMetadata, eval_completion_source
    _scope = {"input_value": target._raw_input_value, "value": target._raw_input_value,
              "draw_state": target, "ds": target}
    if isinstance(target._kwargs, dict):
        _scope.update(target._kwargs)
    FuncsMetadata.record(eval_view_func, _scope)

    code = getattr(target, '_eval_code', None)
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
        target._eval_code = new_code
        code = new_code

    def _fire_eval():
        # Stash the snippet + arm the trigger, then force the target to actually
        # re-render (bypassing its cache) so its wrapper runs func() -- and our
        # eval hook -- this/next frame.
        target._eval_code = code
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
        draw_text(eval_result, name=f"eval_result##{unique}",
                  show_bg=True, show_header=True,
                  show_name=True, editable=False, height=400, min_height=400,
                  wrap=True, bg_offset=-100, width=draw_state.content_width,
              
                  tint=(0.0, 0.0, 0.0))
    return False, input_value


from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost

class ContextMenuState:
    def __init__(self):
        self.render_func_str = None
        self.render_func_dict = None
        self.class_str = None
        self.class_dict = None
        # One (str_host, dict_host) pair per call site shown - the direct
        # caller, its caller, ... up to Toggles.caller_walk_steps. Innermost-first.
        self.call_site_hosts = []
        self.mode_str = None
        self.mode_dict = None
        # What the cached hosts above were built FOR. This menu's up/down nav
        # retargets the same tab draw_state (and thus this same cm_state) at an
        # ancestor view, so the hosts must rebuild when the target changes.
        self.host_key = None
        # Tuple of (filename, lineno) the caller hosts above were built for.
        self.call_site_keys = None
        self.mode_key = None
        # (file, lineno) of class_to_show's definition - for the class-var /
        # class-default jump buttons. Cached because inspect.getsourcelines()
        # AST-parses the WHOLE module file (see the host_key block below), so it
        # must not run per frame. Invariant for a given host_key.
        self.class_loc = None


class _InstanceAttrSource(dict):
    """The 'instance attr' source row: the value object's own whitelisted
    params (core_render.OBJ_ATTR_PARAMS — the attrs the wrapper injects, e.g.
    Loras.tint). Reads snapshot at collection time; a write goes straight to
    setattr on the LIVE object — in place and immediate, no code round trip
    (the same storage the instance_attr lens edits). Unlike the host-backed
    sources, a plain setattr triggers NOTHING — so the write also invalidates
    the target view's subtree (the wrapper re-injects the attr on the next
    render, which is also what clears the anywhere in-flight cache)."""

    def __init__(self, obj, target_ds=None):
        from src.lsd.gl_gui.view.core_views.core_render import OBJ_ATTR_PARAMS
        super().__init__({p: getattr(obj, p) for p in OBJ_ATTR_PARAMS
                          if getattr(obj, p, None) is not None})
        self._obj = obj
        self._target_ds = target_ds

    def __setitem__(self, k, v):
        setattr(self._obj, k, v)
        super().__setitem__(k, v)
        ds = self._target_ds
        if ds is not None:
            # invalidate_up: the attr (tint) paints the whole subtree's bg -
            # cached middle tiles would blit-skip dirty grandchildren.
            ds.invalidate_up(max_depth=6)
            request_render()


class _CodecSource(dict):
    """The 'codec' source row: the ACTIVE codec's render_kwargs — the
    wrapper's lowest kwargs merge layer and the provenance color (the green
    on import views etc.). The codec rides every draw_state as ds._codec.

    Writes are PER-FILE when the element resolves to one: the codec stamps
    the attribute into that file's entry in AppModel.file_meta_collection
    (Codec.update_file_meta), which persists with the root save and feeds the
    folder tree's row kwargs. Reads overlay that entry back over the
    codec-wide render_kwargs. Only when no file resolves does a write mutate
    the LIVE class attr in place — immediate but codec-wide and in-memory."""

    def __init__(self, codec, target_ds):
        rk = getattr(codec, "render_kwargs", None)
        super().__init__(rk if isinstance(rk, dict) else {})
        self._codec = codec
        self._target_ds = target_ds
        # Per-FILE overlay: attributes previously attached to this element's
        # file (Codec.update_file_meta → AppModel.file_meta_collection) read
        # back over the codec-wide render_kwargs, so the row round-trips
        # across saves. `order` is folder-tree bookkeeping, not a param.
        entry = codec.file_meta_entry(target_ds)
        if isinstance(entry, dict):
            self.update({k: v for k, v in entry.items() if k != "order"})

    def __setitem__(self, k, v):
        # Per-file first: when the element resolves to a file, the attribute
        # belongs to THAT file - the codec stamps it into the file-metadata
        # object (persisted with the root; the folder tree re-applies it as
        # row kwargs). Only when no file resolves does the write fall through to
        # the codec-wide live render_kwargs.
        if not self._codec.update_file_meta(self._target_ds, k, v):
            rk = getattr(self._codec, "render_kwargs", None)
            if not isinstance(rk, dict):
                rk = {}
                self._codec.render_kwargs = rk
            rk[k] = v
        super().__setitem__(k, v)
        self._target_ds.invalidate_up(max_depth=6)
        request_render()


class _ChildKwargsSource(dict):
    """The child_kwargs row when the parent's class-level @defaults doesn't
    (yet) carry child_kwargs in SOURCE: displays the live dict, and the first
    write lazily creates `child_kwargs={...}` inside the class-level defaults
    PARSE — a non-dunder key, so plain bubbling item-writes wrap the new dict
    and dirty the host (no __overrides__-style special casing needed). The
    save + the parent-class recompile stamp then make it code."""

    def __init__(self, defaults_parse, live):
        super().__init__({k: v for k, v in (live or {}).items()
                          if isinstance(k, str) and not k.startswith("__")})
        self._dp = defaults_parse

    def __setitem__(self, k, v):
        ck = self._dp.get("child_kwargs")
        if not isinstance(ck, dict):
            self._dp["child_kwargs"] = {}      # bubbling: wraps + dirties
            ck = self._dp["child_kwargs"]
        ck[k] = v
        super().__setitem__(k, v)


class _DrawStateAttrSource(dict):
    """The 'draw state' source row: whitelisted params read from the target
    draw_state's own fields (ds.tint — the style cascade's last fallback,
    persisted with window state). The DEFAULT source: SourcePriority ranks it
    last, so it only ever drives when nothing else sets the param. Writes go
    setattr-on-the-ds, in place, plus the same subtree invalidation the
    instance adapter does."""

    def __init__(self, target_ds):
        from src.lsd.gl_gui.view.core_views.core_render import OBJ_ATTR_PARAMS
        super().__init__({p: getattr(target_ds, p) for p in OBJ_ATTR_PARAMS
                          if getattr(target_ds, p, None) is not None})
        self._target_ds = target_ds

    def __setitem__(self, k, v):
        setattr(self._target_ds, k, v)
        super().__setitem__(k, v)
        self._target_ds.invalidate_up(max_depth=6)
        request_render()


class _LazyOverrideEntry(dict):
    """Stand-in '# [<key>]' source for a site with NO override comment yet.
    Reads as the empty entry dict; the FIRST write (the matrix's + button)
    materializes __overrides__['__<key>__'] in the owning parse and the save
    synthesizes the comment line (_patch_leading_override's creation branch).
    On the next walk the real entry exists and registers instead.

    Bubbling treats `__…` keys as internal bookkeeping — writes to them are
    stored RAW (no wrap, no dirty mark), which is exactly right for parse
    bookkeeping and exactly wrong here: naive creation leaves plain dicts the
    host never hears about (tint shows — the marker reads the same tree — but
    nothing saves). So the new structure is installed onto the host's bubble
    root explicitly, and the LEAF write goes through the wrapped entry, whose
    non-internal key fires the standard notify → dirty → save path."""

    def __init__(self, root, entry_key=None):
        # entry_key=None targets the root node's OWN __overrides__ - a
        # leading comment above a class/function def - instead of a
        # '__<key>__' field slot on its parent.
        super().__init__()
        self._root = root
        self._entry_key = entry_key

    def __setitem__(self, k, v):
        from src.lsd.gl_gui.view.core_conversion.bubbling import install_bubbling
        root_node = self._root
        ovs = root_node.get("__overrides__")
        if not isinstance(ovs, dict):
            ovs = {}
        if self._entry_key is not None and not isinstance(ovs.get(self._entry_key), dict):
            ovs[self._entry_key] = {}
        broot = getattr(root_node, "_bubble_root", None)
        if broot is not None:
            # Plain dicts can't reclass - install returns the wrapped copy,
            # so store THAT (raw store: internal key). Idempotent if ovs
            # already bubbles.
            ovs = install_bubbling(ovs, broot)
        root_node["__overrides__"] = ovs
        entry = ovs if self._entry_key is None else ovs[self._entry_key]
        entry[k] = v  # non-internal key on the wrapped entry → notify → dirty
        super().__setitem__(k, v)  # same-frame reads (row refresh) see it too



def collect_input_sources(input_value, cm_state, class_to_show=None):
    """Every editable input source behind a view, collected WITHOUT drawing —
    the shared engine of draw_input_tab and set_anywhere. `input_value` is the
    TARGET draw_state; `cm_state` caches the code hosts across calls (pass the
    context menu's, or any per-target instance). Returns a dict:
      sources    {source_name: parse dict}  (placeholder {} when unparsed)
      tints      {source_name: codec tint}
      locations  {source_name: (file, line)} for jump buttons
      kinds      {source_name: kind caption} ("signature" / "caller +N" / ...)
      writable   source names whose dict is a REAL parse node (writes save)
    """
    # Per-frame memo: the input tab plus the tint tab's get_sources_for /
    # from_anywhere / set_anywhere all this for the same target within one
    # frame, and the parses can't change mid-frame - build once per
    # (frame, class_to_show) per cm_state. Profiled at avg 2.5ms / max 12.7ms
    # per build; tripling it per frame was the input-tab drag fps drop.
    _memo_key = (Core.melty.frame_count, class_to_show)
    if getattr(cm_state, "_collect_key", None) == _memo_key:
        return cm_state._collect_cache

    # Source/cst hosts come from the process-wide code-host cache, keyed by the
    # live reference - every menu opened on the same render_func/class/call
    # site shares ONE host pair, so the code isn't re-loaded and re-parsed per
    # open (code_hosts_for in new_converters; earlier like this tab used to
    # build inline).
    host_key = (input_value._view_func, class_to_show)
    if cm_state.host_key != host_key:
        cm_state.host_key = host_key
        cm_state.render_func_str, cm_state.render_func_dict = code_hosts_for(input_value._view_func)
        cm_state.class_str, cm_state.class_dict = code_hosts_for(class_to_show)
        # The nav retargeted us at a different view; its call sites differ (and
        # may not be captured yet — see the lazy capture in draw_context_menu).
        cm_state.call_site_hosts = []
        cm_state.call_site_keys = None
        # The class's source location feeds the class-var/class-default jump
        # buttons. inspect.getsourcelines() on a CLASS AST-parses the ENTIRE
        # module file (CPython 3.9+ _ClassFinder) - ~34ms on a 7k-line file like
        # libcst_conversion.py (IntParse, the parent of a parsed int field) -
        # so it CANNOT run per frame. class_to_show is part of host_key, so the
        # location only changes on a rebuild: read it once, here.
        cm_state.class_loc = None
        if isinstance(class_to_show, type):
            try:
                cm_state.class_loc = (inspect.getsourcefile(class_to_show),
                                      inspect.getsourcelines(class_to_show)[1])
            except (TypeError, OSError):
                cm_state.class_loc = None

    # The caller CHAIN - the direct caller, its caller, ... up to
    # Toggles.caller_walk_steps real (non-dispatch) frames, innermost-first.
    # Read off the cached _call_stack (never the live stack: a drag re-renders
    # with parents skipped, which would wipe every host). The stack can lag a
    # frame or two AFTER retargeting (lazy one-shot capture on the wrapper's
    # next render), so recompute every pass and rebuild the hosts only when the
    # resolved sites change.
    from src.lsd.gl_gui.view.core_conversion.chain_converters import caller_chain
    walk_steps = max(1, int(Toggles.caller_walk_steps or 1))
    caller_frames = caller_chain(getattr(input_value, "_call_stack", None))[:walk_steps]
    caller_site_keys = tuple((f, ln) for f, ln, _ in caller_frames)
    # getattr: a cm_state created before this field existed (hotswap of an
    # already-open inputs tab) lacks caller_site_keys - treat as "needs rebuild".
    if caller_site_keys != getattr(cm_state, "call_site_keys", None):
        cm_state.call_site_keys = caller_site_keys
        cm_state.call_site_hosts = [code_hosts_for(CallSite(f, ln))
                                    for f, ln in caller_site_keys]

    # The ACTIVE mode driving this view - the wrapper stamps it into the
    # view's kwargs when a mode config matches (kwargs['current_mode'],
    # core_render; 'mode' for the recursive variant), so it rides on the
    # target's _kwargs. The host is for the mode's ENUM CLASS (whose source
    # holds every member), keyed per class so retargeting at a view under a
    # different mode enum rebuilds; which member to show is re-read each pass.
    current_mode = input_value._kwargs.get('current_mode')
    mode_cls = current_mode.__class__ if current_mode is not None else None
    if cm_state.mode_key != mode_cls:
        cm_state.mode_key = mode_cls
        cm_state.mode_str, cm_state.mode_dict = (
            code_hosts_for(mode_cls) if mode_cls is not None else (None, None))

    # ── Every input source, one parameter at a time ──────────────────────────
    # Collect each parsed source dict, match against the render function's
    # parameter list via param_source_matrix, and hand the matrix to
    # draw_param_matrix: a per-parameter SCREEN (dropdown within params)
    # listing every possible source - param default, caller, mode, class
    # default, function decoration - set or not. Sources register here even
    # when empty/unparsed (placeholder {}), so each screen always shows the
    # full source list; registration order is the screens' row order.
    # Rebuilt every frame from the live parses (cheap dict copy), so a
    # background parse landing or an edit in any source shows up immediately.
    # Edits to a cell write back into the SOURCE dict they came from
    # (apply_param_source_changes) - those dicts are the hosts'
    # bubbling-wrapped parse nodes, so the edit marks the owning host dirty
    # and rides its normal chain_out/save path. Placeholder rows are plain
    # empty dicts: they never grow cells, so no write-back can land in them.
    # Each source maps to the CODEC that owns its data - the codec's
    # render_kwargs tint IS the source color. The matrix cells are parse
    # fragments that can't adopt it naturally (type-based codec fragments), so
    # the tint map rides into draw_param_matrix, which applies it manually
    # and prominently per row.
    from src.lsd.gl_gui.view.core_conversion.new_codecs import (
        FunctionCodec, CallerCodec, DecorationsCodec, TypeCodec, ModeCodec)

    def _codec_tint(codec):
        return (getattr(codec, "render_kwargs", None) or {}).get("tint")

    sources = {}
    source_tints = {}
    source_locations = {}
    source_kinds = {}
    writable_sources = []

    def _add_source(sname, sdict, codec, location=None, kind=None):
        # Register even when the source sets nothing or hasn't parsed yet -
        # the per-param screen draws EVERY source row, absent ones as "not
        # set". The placeholder is a fresh empty dict, never written to;
        # only REAL parse dicts (even empty ones) are writable, so the
        # matrix's +/× buttons know where a stamped cell can legally land.
        # `kind` is the row's caption (signature / caller / mode / ...);
        # sname stays the concrete spelling (def draw_x / Mode.WINDOW / ...).
        if isinstance(sdict, dict):
            writable_sources.append(sname)
        sources[sname] = sdict if isinstance(sdict, dict) else {}
        source_tints[sname] = _codec_tint(codec)
        if kind:
            source_kinds[sname] = kind
        if location is not None and location[0] is not None:
            source_locations[sname] = location

    # Source names are the concrete spelling shown on the row button
    # (def draw_x / Mode.WINDOW / @render_func(draw_x), ...); the generic
    # origin goes into `kind`, shown as a caption above the button. Locations
    # feed the jump-to buttons.
    view_fn = inspect.unwrap(input_value._view_func)
    fn_name = getattr(view_fn, "__name__", "?")
    try:
        fn_file = inspect.getsourcefile(view_fn)
    except TypeError:
        fn_file = None
    fn_loc = (fn_file, getattr(getattr(view_fn, "__code__", None),
                               "co_firstlineno", None))

    # cls_name is cheap (__name__); cls_loc is cached on host key change above
    # (inspect.getsourcelines AST-parses the whole module file - never per frame).
    cls_name = class_to_show.__name__ if isinstance(class_to_show, type) else "None"
    # getattr: a cm_state created before this field existed (hotswap of an
    # already-open inputs tab) lacks the field - None just degrades the jump
    # location until the menu retargets and the host_key block recomputes it.
    cls_loc = getattr(cm_state, "class_loc", None)

    # One caller source per real frame walked (caller, caller's caller, ...),
    # innermost-first; each its own editable CallSite host parsed above. The
    # captions read "caller", "caller +1", ...; the row button shows the
    # concrete frame name (disambiguated with a ^N suffix when two frames share a
    # name, since source names are dict keys). If the stack hasn't been
    # captured yet (no real callers), still register one empty "caller"
    # row so the source-list shape stays consistent.
    caller_rows = []  # (sname, sdict, location, kind)
    _seen_caller_names = set()
    for i, ((_str_host, dict_host), (filename, lineno, func_name)) in enumerate(
            zip(cm_state.call_site_hosts, caller_frames)):
        cdict = dict_host.deep.unwrap() if dict_host else None
        cname = func_name or "caller"
        if cname in _seen_caller_names:
            cname = f"{cname} ^{i}"
        _seen_caller_names.add(cname)
        kind = "caller" if i == 0 else f"caller +{i}"
        caller_rows.append((cname, cdict, (filename, lineno), kind))
    if not caller_rows:
        caller_rows.append(("caller", None, None, "caller"))

    # MODE - the active mode is entry in its enum class source (e.g.
    # `NEW_CODE = {types...: ModeOverrides(kwargs={...})}` in mode.py), not as
    # the kwargs dict that entry stamps into this view. A mode can hold
    # SEVERAL type-keyed entries (TEXT_ONLY); the parse can't be type-matched
    # (its keys are unevaluated source), so pick the candidate whose keys best
    # overlap the LIVE matched config (get_config_for) - the entry that
    # actually drove THIS view. Edits merge into the mode_dict host's parse
    # and ride its normal chain_out/save back into the enum's source file.
    mode_label = str(current_mode) if current_mode is not None else "None"
    mode_kwargs, mode_loc = None, None
    if current_mode is not None and cm_state.mode_dict is not None:
        candidates = [c for c in
                      cm_state.mode_dict.deep[current_mode.name].kwargs.all()
                      if isinstance(c, dict)]
        live_keys = set()
        if isinstance(getattr(current_mode, "value", None), dict):
            live_cfg = current_mode.get_config_for(input_value._raw_input_value)
            if live_cfg is not None and live_cfg.kwargs:
                live_keys = set(live_cfg.kwargs)
        mode_kwargs = max(candidates,
                          key=lambda c: len(live_keys & set(c)), default=None)
        # inspect.getsourcelines on a class AST-parses its WHOLE module (the
        # class_loc lesson) - cache the member's location per (class, member),
        # never recompute per frame.
        _ml_key = (mode_cls, current_mode.name)
        if getattr(cm_state, "_mode_loc_key", None) != _ml_key:
            cm_state._mode_loc_key = _ml_key
            cm_state._mode_loc = None
            try:
                cls_lines, cls_start = inspect.getsourcelines(mode_cls)
                member_off = next(
                    (i for i, l in enumerate(cls_lines)
                     if l.lstrip().startswith((f"{current_mode.name} =",
                                               f"{current_mode.name}="))), 0)
                cm_state._mode_loc = (inspect.getsourcefile(mode_cls),
                                      cls_start + member_off)
            except (TypeError, OSError):
                pass
        mode_loc = cm_state._mode_loc

    # Registration order IS the per-param screen's row order: param default
    # (signature), caller, mode, class var, class default, function decoration.
    _add_source(f"def {fn_name}", cm_state.render_func_dict.deep.parameters(), FunctionCodec,
                location=fn_loc, kind="signature")
    for cname, cdict, cloc, ckind in caller_rows:
        _add_source(cname, cdict, CallerCodec, location=cloc, kind=ckind)
    _add_source(mode_label, mode_kwargs, ModeCodec, location=mode_loc, kind="mode")
    # CLASS VAR - the data class's body assignments (`tint = (...)` on the
    # class itself). The class parse IS that dict (fields + __cst__/comment
    # bookkeeping, filtered out by param_source_matrix), so a + here writes a
    # brand-new class var assignment and × removes one, via the same
    # bubbling/save path as every other source.
    _add_source(f"class {cls_name}",
                cm_state.class_dict.deep.unwrap() if cm_state.class_dict else None,
                TypeCodec, location=cls_loc, kind="class var")
    _add_source(f"@defaults({cls_name})", cm_state.class_dict.deep.decorators.defaults(),
                TypeCodec, location=cls_loc, kind="class default")
    # INSTANCE ATTR - the value instance's own whitelisted params (Lamp.tint):
    # what core_render's OBJ_ATTR_PARAMS injection reads. Skip-when-absent
    # like the other value-side rows; dicts/classes carry their config in
    # __overrides__ / class rows above.
    _raw_obj = getattr(input_value, "_raw_input_value", None)
    if _raw_obj is not None and not isinstance(_raw_obj, (dict, list, type)):
        _ia = _InstanceAttrSource(_raw_obj, target_ds=input_value)
        if _ia:
            _add_source(f"{type(_raw_obj).__name__} instance", _ia,
                        TypeCodec, kind="instance attr")
    # CHILD KWARGS - a parent view can drive this view's params via
    # child_kwargs={...} (draw_collection merges it into every child call).
    # The dict itself can be attached to ANY of the PARENT'S own sources (its
    # caller, decorator, comment, instance attr...), so resolve it with the
    # registry machinery ON THE PARENT: from_anywhere("child_kwargs",
    # parent). Cheap gatekeeping: only parents whose live _kwargs actually
    # carry a child_kwargs dict pay the (per-frame-memoized) parent
    # check; the root's self-parent loop is excluded. Recursion up the
    # ancestry terminates the same way: it only walks through parents that
    # themselves receive child_kwargs.
    _pds_ck = getattr(input_value, "_parent", None)
    if _pds_ck is not None and _pds_ck is not input_value:
        _my_key = (input_value._kwargs or {}).get("key")
        _ck_live = (_pds_ck._kwargs or {}).get("child_kwargs")
        _ck_live = _ck_live if isinstance(_ck_live, dict) and _ck_live else None
        _mode_src = None      # the parent's mode source name, set below
        if _ck_live is not None or isinstance(_my_key, str):
            # Ensure the PARENT's registry exists (fresh sessions have no
            # _sa_cm_state until something collects it) - memoized per frame,
            # and this path only runs when a menu/tab is open on a child.
            _psrcs = _sources_for(_pds_ck)
            _pcm = getattr(_pds_ck, "_sa_cm_state", None)
            _pdeco = (_pcm.class_dict.deep.decorators()
                      if _pcm is not None and _pcm.class_dict is not None else None)
            _pcls = (_pcm.host_key or (None, None))[1] if _pcm is not None else None
            _pcls_name = getattr(_pcls, "__name__", "?")
            _cls_defaults = None       # first non-attr @defaults parse entry
            if isinstance(_pdeco, dict):
                for _dk, _dv in _pdeco.items():
                    if not (isinstance(_dk, str) and _dk.split("#", 1)[0] == "defaults"
                            and isinstance(_dv, dict)):
                        continue
                    _dattr = _dv.get("attr") or _dv.get("attrib")
                    if _dattr is None:
                        if _cls_defaults is None:
                            _cls_defaults = _dv
                    elif str(_dattr).strip("'\"") == _my_key:
                        # ATTR-TARGETED @defaults(attr="<this field>", ...):
                        # its own source row on the field's view.
                        _add_source(f"@defaults({_pcls_name}.{_my_key})", _dv,
                                    TypeCodec, kind="attr default")
            # CHILD KWARGS (MODE) - the parent's MODE entry can carry its own
            # child_kwargs={...} (Modes.NEW_CODE stamps column_widths on every
            # child). It reaches this view through the same merge as any other
            # child_kwargs, but it lives in the mode ENUM's source, so it gets
            # its own row: an edit here writes mode.py, not the parent's
            # caller/@defaults. Absent a lazy-create adapter, but the first
            # write stamps `child_kwargs={...}` onto the mode entry's kwargs
            # parse (bubbling wraps + dirties; the mode host's code persists).
            _mode_src = next((s for s, k in _psrcs["kinds"].items()
                              if k == "mode"), None)
            _mode_parse = _psrcs["sources"].get(_mode_src) if _mode_src else None
            if isinstance(_mode_parse, dict) and _mode_src in set(_psrcs["writable"]):
                _mck = _mode_parse.get("child_kwargs")
                if not isinstance(_mck, dict):
                    _mck = _ChildKwargsSource(_mode_parse, None)
                _add_source("child_kwargs (mode)", _mck, ModeCodec,
                            location=_psrcs["locations"].get(_mode_src),
                            kind="mode child kwargs")
        if _ck_live is not None:
            # CHILD KWARGS — the dict driving this view from the parent.
            # Prefer the CODE-backed setter (the first source DRIVING
            # child_kwargs - from_anywhere's pick, inlined so the same target
            # also yields the row's jump location: the caller line /
            # @defaults class line the dict lives at). If no source sets it
            # yet, a lazy-create adapter over the class-level @defaults dict
            # makes the first write EDIT CODE (the live dict alone silently
            # kept writes in memory).
            # The MODE setter has its own row above (it outranks any other
            # parent sources, so it would otherwise always BE this row and the
            # non-mode setters would never be visible/editable): resolve this
            # row from the parent's sources MINUS mode.
            _psrcs_nm = _psrcs
            if _mode_src is not None:
                _psrcs_nm = dict(_psrcs, sources={k: v for k, v in _psrcs["sources"].items()
                                                  if k != _mode_src})
            _ck_target = _driving_source(_psrcs_nm, "child_kwargs")
            _ck_dict = (_psrcs_nm["sources"][_ck_target].get("child_kwargs")
                        if _ck_target is not None else None)
            _ck_loc = (_psrcs["locations"].get(_ck_target)
                       if _ck_target is not None else None)
            if not (isinstance(_ck_dict, dict) and _ck_dict):
                _ck_dict = (_ChildKwargsSource(_cls_defaults, _ck_live)
                            if isinstance(_cls_defaults, dict) else _ck_live)
                _ck_loc = getattr(_pcm, "class_loc", None) if _pcm is not None else None
            _add_source("child_kwargs", _ck_dict, TypeCodec,
                        location=_ck_loc, kind="child kwargs")
    # CODEC - the active codec's render_kwargs (ds._codec, stashed by the
    # wrapper for every view): the lowest kwargs merge layer and the
    # provenance color (import views' green). Skip-when-absent like the
    # other value-side rows.
    _codec_obj = getattr(input_value, "_codec", None)
    if _codec_obj is not None:
        _cs = _CodecSource(_codec_obj, input_value)
        if _cs:
            _add_source(f"codec {getattr(_codec_obj, '__name__', type(_codec_obj).__name__)}",
                        _cs, TypeCodec, kind="codec")
    # DRAW STATE - the ds's own kwargs (ds.tint): the lowest source, ranked
    # last, so windows tinted only by their persisted draw_state (the "blue
    # window no other claims" case) still resolve to a real, writable row.
    _dsa = _DrawStateAttrSource(input_value)
    if _dsa:
        _add_source("draw_state", _dsa, TypeCodec, kind="draw state")
    # ── Sources parsed off the VALUE itself ──────────────────────────────────
    # The value flowing through the view can be (or sit inside) a parse node
    # of the owning window's's bubbling tree (e.g. a nested ClassParse in
    # the Toggles window). Kwargs carried by that parse are live inputs the
    # class_to_show rows (keyed on the value's runtime TYPE) miss, and since
    # the dicts are bubbling trees, a cell edit marks the host dirty and saves
    # through its normal path - no extra save need. Walk a few parents so a
    # primitive leaf (scroll_speed) still finds its owning class parse, same
    # as the class_to_show walk. Two such sources per parse:
    #   @defaults(<name>)  the parse's decorators.defaults dict - the wrapper
    #                      reads it directly (core_render's
    #                      input_value["decorators"] tint path)
    #   # [<name>]         the '# [tint=(...)]' override comment on the
    #                      value's source line: a leaf child carries it in
    #                      __overrides__; a primitive field's comment lives on
    #                      the PARENT parse under __<key>__ (fed to the
    #                      child's kwargs by draw_collection). Saves update
    #                      the comment via _patch_leading_override /
    #                      _patch_field_overrides.
    # Both skip when absent (same rule as @window below) - a placeholder row
    # on every parsed value would be permanent noise.
    def _root_file_loc(ds, span):
        # Map a span (relative to the root host parse's source) to a file
        # location: nested parses carry no file_path, so find the ancestor
        # parse that has file_path + line_offset. The window ds holds it as
        # _input_value (post-convert), so check both. None → no jump location.
        while span is not None and ds is not None:
            for _r in (getattr(ds, "_raw_input_value", None),
                       getattr(ds, "_input_value", None)):
                if isinstance(_r, GeneralParse) and getattr(_r, "file_path", None):
                    return (str(_r.file_path), _r.line_offset + span.start_line)
            if ds._parent is ds:
                break
            ds = ds._parent
        return None

    _pds, _pwalk = input_value, 4
    while _pds is not None and _pwalk >= 0:
        _praw = getattr(_pds, "_raw_input_value", None)
        if isinstance(_praw, GeneralParse):
            _pdecos = _praw.get("decorators")
            _pdefaults = _pdecos.get("defaults") if isinstance(_pdecos, dict) else None
            _pname = getattr(getattr(_praw.get("__cst__"), "name", None),
                             "value", None)
            # The name-carry guard covers viewing ClassParse's own parse -
            # the class_to_show row already shows it there.
            if (isinstance(_pdefaults, dict) and _pdefaults and _pname
                    and f"@defaults({_pname})" not in sources):
                _add_source(f"@defaults({_pname})", _pdefaults, TypeCodec,
                            location=_root_file_loc(_pds, getattr(_praw, "span", None)),
                            kind="code class default")
            _povs = _praw.get("__overrides__")
            if _pds is input_value:
                # The view targets the parse itself - its own leading comment.
                _ov_dict, _ov_name = _povs, _pname
                _ov_span = getattr(_praw, "span", None)
            else:
                # Primitive leaf - the parent parse holds its comment under
                # __<key>__; the leaf's parent key rides in its kwargs (the
                # 'key' entry draw_collection passes every child).
                _tkey = (input_value._kwargs or {}).get("key")
                _ov_dict = (_povs.get(f"__{_tkey}__")
                            if isinstance(_povs, dict) and isinstance(_tkey, str)
                            else None)
                _ov_name = _tkey
                # _child_spans maps field keys to their assignment's span
                # (_record_child) - jump lands on the field's line, the
                # comment's one above.
                _ov_span = (getattr(_praw, "_child_spans", None) or {}).get(_tkey)
            if _ov_name:
                if not (isinstance(_ov_dict, dict) and _ov_dict):
                    # No comment yet - lazy row, same as the live-view path
                    # below: the matrix's + materializes the entry, the
                    # wrapper synthesizes the `# [...]` line. For the parse
                    # itself (a class/function node) the entry is its OWN
                    # __overrides__ (leading comment on the def); for a
                    # leaf it's the parent's __<key>__ slot. Module parses
                    # have no _pname, so they register nothing - no noise.
                    _ov_dict = (_LazyOverrideEntry(_praw)
                                if _pds is input_value
                                else _LazyOverrideEntry(_praw, f"__{_ov_name}__"))
                _add_source(f"# [{_ov_name}]", _ov_dict, TypeCodec,
                            location=_root_file_loc(_pds, _ov_span),
                            kind="code comment")
            break
        # LIVE-VIEW windows/markers: the value flowing through them is a
        # runtime capture, not a parse node - but the marker stamps the owning
        # scope's parse dict on the draw_state (live_root/live_key, the same
        # data its comment-args splat reads), so the site's `# [<key>]`
        # comment registers as an input source exactly like a primitive
        # leaf's. Edits write to the editor host's bubbling tree and save
        # through its normal path.
        _lroot = getattr(_pds, "live_root", None)
        if isinstance(_lroot, dict):
            _lkey = getattr(_pds, "live_key", None)
            if isinstance(_lkey, str):
                _lovs = _lroot.get("__overrides__")
                _lov = (_lovs.get(f"__{_lkey}__")
                        if isinstance(_lovs, dict) else None)
                if not isinstance(_lov, dict):
                    # No override yet - register a lazy entry so the matrix's
                    # + can create `# [tint=(...)]` the same way it stamps a
                    # missing @defaults(row).
                    _lov = _LazyOverrideEntry(_lroot, f"__{_lkey}__")
                _lspan = (getattr(_lroot, "_child_spans", None) or {}).get(_lkey)
                _add_source(f"# [{_lkey}]", _lov, TypeCodec,
                            location=_root_file_loc(_pds, _lspan),
                            kind="code comment")
            break
        if _pds._parent is _pds:
            break
        _pds = _pds._parent
        _pwalk -= 1
    # @window on the class (e.g. `@window(tint=(0.11,0.12,0.14))` on Toggles) -
    # its kwargs drive the window rendering the value, so it's an input source.
    # Same skip-when-absent rule as the fn-side @window below.
    _cls_window_deco = (cm_state.class_dict.deep.decorators.window()
                        if cm_state.class_dict else None)
    if isinstance(_cls_window_deco, dict) and _cls_window_deco:
        _add_source(f"@window({cls_name})", _cls_window_deco,
                    DecorationsCodec, location=cls_loc, kind="class decoration")
    # @render_func(...) - the view func's decorator kwargs. Registered for
    # EVERY value (not just function values, the old gate): decorator kwargs
    # outrank signature defaults in the wrapper gauntlet, so a param set
    # there (fast_toggle's show_cache=False) is the TRUE driver and hiding
    # the row made provenance lie. The app-wide blast radius of an edit is
    # real but the row is only ever written by an explicit pick.
    _add_source(f"@render_func({fn_name})",
                cm_state.render_func_dict.deep.decorators.render_func(),
                DecorationsCodec, location=fn_loc, kind="decoration")
    # @window only exists as a source on actually-@window-decorated funcs -
    # a placeholder row here would be permanent noise on every other tab.
    _window_deco = cm_state.render_func_dict.deep.decorators.window()
    if isinstance(_window_deco, dict) and _window_deco:
        _add_source(f"@window({fn_name})", _window_deco,
                    DecorationsCodec, location=fn_loc, kind="window decoration")


    cm_state._collect_cache = {
        "sources": sources, "tints": source_tints,
        "locations": source_locations, "kinds": source_kinds,
        "writable": tuple(writable_sources)}
    cm_state._collect_key = _memo_key
    return cm_state._collect_cache


@render_func(use_cache=True, show_bg=False, show_header=False, show_name=False, selectable=False, disable_scroll=False,
             temp=True, searchable=True)
def draw_input_tab(input_value, cm_state:ContextMenuState, draw_state, wrap=True, unique=None, class_to_show=None,
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
                                           width=draw_state.content_width-0,
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
    for _h in (cm_state.render_func_dict, cm_state.class_dict,
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
def draw_class_tab(input_value, class_to_show=None, class_is_parent=False, class_name='',  **kwargs):
    """Editable class source. For a primitive field this is the parent object's
    class (e.g. Lora for a Lora.rank float) -- labelled so the source is clear.
    Routes through Mode.FILE_TREE — the same cache-backed code_file_io path a
    folder-files leaf uses — so all editors share one code path."""
    from src.lsd.gl_gui.view.mode import Mode
    if class_is_parent:
        text(f"Parent type of {class_name}", name="Source",
             editable=False, tint=(1.0, 0.64, 0.113))
    cls_change, new_cls = draw_any(class_to_show, mode=Mode.FILE_TREE,
                                   name=class_to_show.__name__)
    return False, input_value


@render_func(use_cache=True, show_bg=False, show_header=False, show_name=False, selectable=False)
def draw_mode_tab(input_value, draw_state, current_mode=None, **kwargs):
    """Show the current Mode's value string."""
    if current_mode is not None:
        mode_change, new_mode = text(str(current_mode.value), width=draw_state.content_width,
                                     name=str(current_mode))
    return False, input_value


def _ancestor_call_line(target_ds, ancestor_ds):
    """File-absolute line inside `ancestor_ds`'s view function whose statement
    (transitively) rendered `target_ds`'s element — e.g. inspecting a button
    drawn by draw_tab_bar and walking up one scope resolves the `button(...)`
    call line in draw_tab_bar. The func tab auto-selects it.

    Scans the TARGET's cached _call_stack (innermost-first tuples, captured
    once on menu-open — never the live stack, see the capture note in
    core_render) for the nearest frame executing the ancestor's function; that
    frame's lineno is the call statement. A view rendered inside a deferred
    layer has a stack that bottoms out at the layer loop, so each deferred
    ancestor's queue-time _deferred_call_stack is appended to continue the
    chain outward. None when the ancestor's frame isn't in the chain (stack
    not captured yet, or the ancestor rendered from cache with parents
    skipped)."""
    view_func = getattr(ancestor_ds, "_view_func", None)
    if view_func is None:
        return None
    code = getattr(inspect.unwrap(view_func), "__code__", None)
    if code is None:
        return None
    stack = list(getattr(target_ds, "_call_stack", None) or ())
    node = target_ds
    for _ in range(64):
        if getattr(node, "_is_deferred_layer", False):
            stack.extend(getattr(node, "_deferred_call_stack", None) or ())
        parent = getattr(node, "_parent", None)
        if node is ancestor_ds or parent is None or parent is node:
            break
        node = parent
    for filename, lineno, func_name in stack:
        if func_name == code.co_name and filename == code.co_filename:
            return lineno
    return None


@render_func(use_cache=True, disable_scroll=True, show_header=False,
             header_same_line=False, show_tint=False, show_name=False, is_tree=False)
def draw_context_menu(input_value, draw_state, cursor_hover_inverted, func, unique=None, search_text='',
                      search_active=False,
                      enter_key_down=None, tab_state: TabState = None, **kwargs):
   
    context_menu_offset = input_value.context_menu_offset
    # imgui.text(type(input_value._input_value).__name__)
    imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] - 1, imgui.get_cursor_screen_pos()[1] - 18))
    # if up_key_pressed:
    #     print("Up key pressed")g
    fa_up_arrow = ""
    fa_down_arrow = ""
    if input_value._parent.id is not None:
        if button(fa_up_arrow, height=50)[0]:
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
        if button(fa_down_arrow, height=50)[0]:
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
    if button(f" ", height=30, tint=(0,0,0,1.0), name=f"screenshot_window##{unique}")[0]:
        from src.lsd.gl_gui.screenshot import request_view_capture

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
    if button(f" claude", height=30, tint=(0,0,0,1.0), name=f"claude_session##{unique}")[0]:
        from src.lsd.gl_gui.screenshot import request_view_capture
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
            from src.lsd.gl_gui.view.playground.claude_terminals import (
                launch_claude_session, open_claude_terminals_window)
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
    if button(f"{bug_icon}", height=30, tint=(0.42, 0.24, 0.06, 1.0),
              name=f"recapture_trace##{unique}")[0]:
        _rc_target = input_value
        for _ in range(context_menu_offset):
            if _rc_target._parent is None or _rc_target._parent is _rc_target:
                break
            _rc_target = _rc_target._parent
        _rc_target._call_site_captured = False
        _rc_target._call_site_requested = True
        if _rc_target._is_deferred_layer:
            _rc_target._deferred_stack_requested = True
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
    from src.lsd.gl_gui.view.core_conversion.bubbling import base_of_bubbling
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

    indices = list(range(len(tab_names)))

    if not tab_state.selected_tabs:
        tab_state.selected_tabs = [indices[Toggles.ContextMenu.default_tab]]

    current_mode = input_value._kwargs.get('mode', None)
    mode_tab = str(current_mode)
    if current_mode is not None:
        tab_names.append(mode_tab)

    # Indices list

    imgui.dummy(0,1)
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

    imgui.dummy(0,2)

    # Columns stripped: render the selected tab(s) stacked at the menu's full
    # width - one all across the menu, several fill the height. No Column calls,
    # so nothing here triggers the window-frame edge system (the source of the
    n_sel = max(1, len(tab_state.selected_tabs))
    full_w = draw_state.content_width
    clip = draw_state.abs_clip_rect
    top_y = imgui.get_cursor_screen_pos()[1]
    tab_h = max(60.0, (clip[3] - top_y) / n_sel - 6) if clip is not None else None

    for t_idx, static_tab in enumerate(tab_state.selected_tabs):
        size_kw = {"width": full_w}
        if tab_h is not None:
            size_kw["height"] = tab_h
        if static_tab >= len(tab_names):
            # A persisted selection that outran the current tab set: fall back
            # to the func tab rather than indexing out of range.
            draw_func_tab(input_value, name=f"func_tab_{t_idx}##{unique}",
                          disable_scroll=True, select_line=func_tab_select_line,
                          select_seq=func_tab_select_seq, **size_kw)
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
                          select_seq=func_tab_select_seq, **size_kw)

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
    imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] - 1, imgui.get_cursor_screen_pos()[1] - 18))
    imgui.text(f"{input_value._raw_input_value.__class__.__name__}")

    imgui.dummy(0, 30)
    return False, input_value


@render_func(show_bg=True, use_cache=True, shadow=False, with_header=draw_header)
def draw_undo_manager(input_value, **kwargs):
    """Render both undo timelines — edits (input_value is the UndoManager
    class, handed in by @window) and NavUndo's navigation stack — grouped by
    undo step (one user action), newest first. Changes from the same action
    share a group_id and undo together, so we draw a separator between groups
    and indent the changes within each."""
    for title, stack in (("Edits", input_value.stack), ("Navigation", NavUndo.stack)):
        history = list(stack.history)
        group_count = len({c.group_id for c in history})
        imgui.text(f"{title}: {group_count} undo step(s), {len(history)} change(s)"
                   f"  (redo: {len(stack.redo_stack)})")
        prev_gid = None
        shown = 0
        for change in reversed(history):
            if change.group_id != prev_gid:
                imgui.separator()
                prev_gid = change.group_id
            from_val = str(change.old)[:20]  # truncate long values for readability
            to_val = str(change.new)[:20]
            imgui.text(f"  {change.display_name}: {from_val} -> {to_val}")
            shown += 1
            if shown >= 12:
                imgui.text(f"... and {len(history) - shown} more")
                break
        imgui.dummy(1, 8)
    return False, input_value


@render_func(use_cache=True, temp=True)
def draw_drop_down_item(input_value, name="", unique=0, shadow=False, draw_state=None, **kwargs):
    hovered = draw_state._bounding_hovered
    clicked, _ = button(name, name=f"{unique}{name}_dd_item", show_bg=True, height=25,
                        shadow=False, hovered=hovered)

    # Hover highlight: paint a translucent wash over this item's own box (its
    # draw_state is the row, so abs_left/abs_top + width/height frame it exactly)
    # straight onto the window draw list so it sits over the button fill.
    if hovered:
        dl = imgui.get_window_draw_list()
        dl.add_rect_filled(draw_state.abs_left, draw_state.abs_top,
                           draw_state.abs_left + draw_state.width,
                           draw_state.abs_top + draw_state.height,
                           imgui.get_color_u32_rgba(1, 1, 1, 0.16),
                           rounding=getattr(draw_state, 'corner_radius', 6))

    if clicked:
        return True, name

    return False, input_value



@render_func(use_cache=True, show_bg=False, selectable=False,
             tint=(0.083, 0.10, 0.144),
             is_tree=False, show_name=True, with_header=draw_header)
@window
def draw_dropdown(input_value, collection, name, draw_state, unique, drop_down_state: DropDownState, shadow=True, text_align="left", **kwargs):
    """Root of a recursive dropdown. Renders a trigger button showing the current
    selection; clicking it opens the (click-o-open) root popover. Nested dict
    rows inside the popover open their own sub-menus on hover. Returns
    (changed, selected_leaf) when the user picks a value.

    Open/closed is a single global slot -- ``Melty.popover_focused_ds`` holds the
    draw_state of whichever dropdown's popover is currently shown. Each dropdown
    decides it's open by identity (``popover_focused_ds is draw_state``), so
    opening one popover implicitly closes every other (they all fail the test).
    ``drop_down_state`` is the per-view scratch object the framework re-injects
    every frame; we stash the last picked leaf on it for the trigger label."""
    from src.lsd.gl_gui.view.mode import Mode

    # DEBUG
    # Is THIS dropdown the one whose popover is showing?
    is_open = Melty.popover_focused_ds is draw_state
    _DD_DBG = False  # TEMP: default False for dropdown-close investigation
    if _DD_DBG:
        _pf = Melty.popover_focused_ds
        if is_open or _pf is not None:
            try:
                _mx, _my = imgui.get_mouse_pos()
                _clk = imgui.is_mouse_clicked(0); _dn = imgui.is_mouse_down(0)
            except Exception:
                _mx = _my = -1.0; _clk = _dn = None
            with open("/tmp/dd_debug.log", "a") as _f:
                _f.write(f"[DD-DBG] f={Melty.frame_count} name={name!r} ds={id(draw_state)} "
                  f"pf={id(_pf) if _pf is not None else None} is_open={is_open} "
                  f"mouse=({_mx:.0f},{_my:.0f}) clk={_clk} down={_dn} "
                  f"sel_path={getattr(drop_down_state, 'selected_path', ())} "
                  f"open_frame={getattr(Melty, '_popover_open_frame', None)}\n")

    # Label shows the last pick (sticky across frames via drop_down_state),
    # falling back to the raw input value.
    # Title shows the LABEL/key of the current selection (e.g. "red"), not the raw
    # value (which may be a tuple/number); selected_label is stamped at pick-time.
    _sel_label = getattr(drop_down_state, "selected_label", "") or ""
    current = _sel_label if _sel_label else (str(input_value) if input_value is not None else "")
    caret = "" if is_open else ""  # fa-chevron-down / fa-chevron-right

    # Compact mode: in a very narrow slot (e.g. an inline table cell) there's no room
    # for the caret + button chrome, so show NOTHING but the selected value. Still a
    # real (bg-less) button, so it stays clickable to open the popover. Threshold is
    # tunable via compact_below (px).
    #
    # The trigger's slot width: an explicit caller `width` wins, only without one
    # do we fall back to the measured content_width. Measurement must never feed
    # back into the trigger size - an open popover inflates content_width, which
    # would flip compact mode off and balloon the trigger (~240px), wrapping the
    # header row it sits in: the disagreement between the drawn size and the
    # measured item rect is exactly what reads as animation jitter.
    _slot_w = kwargs.get("width") or draw_state.content_width
    compact = _slot_w < kwargs.get("compact_below", 34) and len(input_value) > 2
    compact = False
    bg_offset = 4 if is_open else 7

    # A compact trigger hugs its glyph: minimal text pad and a centered label,
    # so a small chevron/icon cell doesn't balloon to full text label width.
    trigger_pad = kwargs.get("text_pad", 6 if compact else 15)
    trigger_align = "center" if compact else text_align
    trigger_w = max(15 if compact else 18, _slot_w)

    # The label must FIT the fixed trigger width in pixels - a long label
    # overflows the button rect and the measured item rect disagrees with
    # the drawn size, the same disagreement the width comment above calls
    # out as height jitter. Ellipsis-trim via a text width, not a
    # character count (glyph widths vary wildly with font/monospace).
    _label_px = max(4.0, trigger_w - trigger_pad * 2)
    if compact:
        # Show the VALUE itself (not the label/key). For a name->glyph dropdown
        # the trigger must hug the glyph, not become the picked value's name.
        drop_down_display_str = _dd_fit_label(
            str(input_value if input_value is not None else current), _label_px)
    else:
        drop_down_display_str = _dd_fit_label(f"{caret} {str(current)}", _label_px)

    trigger_h = (getattr(draw_state, "content_height", 0) or 25) if compact else 25
    # Colour the trigger by the selected item's embedded tint (input_value is the
    # current selection passed by the caller), falling back to the view's tint.
    trigger_tint = _dd_obj_tint(input_value, draw_state.tint)
    # Quiet text dimming (text_toward_bg - the info tab's info rows): a
    # selection WITHOUT an embedded tint fades toward the background, so a
    # tinted one (the active source in yellow) is what draws the eye.
    _ttb = kwargs.get("text_toward_bg", 0.0)
    trigger_text_value = 1.023
    if _ttb and getattr(input_value, "tint", None) is None:
        trigger_text_value = 1.023 * (1.0 - min(max(float(_ttb), 0.0), 1.0))
    clicked, _ = button(drop_down_display_str, name=f"{name}_dd_trigger{unique}", show_bg=False, width=trigger_w,
                         show_button_bg=kwargs.get("show_button_bg", True),
                         shadow=shadow, tint=trigger_tint,
                         text_value=trigger_text_value,
                         height=kwargs.get("trigger_height", 19), disable_scroll=True,
                        z_offset=0, text_align=trigger_align, bg_offset=bg_offset, text_pad=trigger_pad)

    if clicked:

        was_open = is_open
        if _DD_DBG:
            with open("/tmp/dd_debug.log", "a") as _f:
                _f.write(f"[DD-DBG] CLICK toggle f={Melty.frame_count} name={name!r} was_open={was_open}\n")
        Melty.popover_focused_ds = None if is_open else draw_state
        is_open = Melty.popover_focused_ds is draw_state
        if is_open and not was_open:
            Melty._popover_open_frame = Melty.frame_count  # grace the opening click
            # Fresh open: start with an empty query and give the search box a few
            # frames to grab text focus so the user can type to filter immediately.
            drop_down_state.search_query = ""
            drop_down_state.search = ""
            drop_down_state._focus_search = 8
            # Start the highlight on the last-selected item (expanded to it) rather
            # than the top, so re-opening starts where you left off.
            _sp = _dd_as_tuple(getattr(drop_down_state, "selected_path", ()))
            drop_down_state.cursor_path = _sp
            drop_down_state.open_path = _sp[:-1] if _sp else ()
            drop_down_state._kbd_mode = True
            drop_down_state._last_mouse = None
            drop_down_state._had_focus = False
        if not is_open:
            _dd_close(drop_down_state)
            draw_state.invalidate()

        request_render()


    # The popover is a latching window -- the first call registers it and it
    # stays alive, so we always call it and toggle visibility with `closed`
    # rather than skipping the call (skipping would leave the last-open frame
    # painted). closed=True hides the whole subtree. window_pos pins it just
    # under the trigger (relative to this dropdown window) so it doesn't drift
    # off as a free-floating draggable; temp keeps it ephemeral. root_state
    # carries the single open-path the recursion expands; path_prefix starts
    # empty at the root. A pick bubbles back as (changed, value).
    if is_open:
        # A mouse move switches back to hover mode so the highlight follows the
        # pointer again (until the next arrow key locks keyboard mode).
        _mp = imgui.get_mouse_pos()
        _lm = getattr(drop_down_state, "_last_mouse", None)
        if _lm is not None and (abs(_mp[0] - _lm[0]) > 0.5 or abs(_mp[1] - _lm[1]) > 0.5):
            drop_down_state._kbd_mode = False
        drop_down_state._last_mouse = (_mp[0], _mp[1])

    changed, new_item = draw_dd_menu(collection, tint=draw_state.tint,
                                     name=f"{unique}_menu",
                                     closed=not is_open, temp=True, shadow=False,
                                     window_pos=(0, trigger_h -_DD_ROW_H), max_height=500,
                                     parent_window=draw_state, swoosh=False, disable_scroll=False,
                                     row_tints=kwargs.get("row_tints"),
                                     row_actions=kwargs.get("row_actions"),
                                     text_toward_bg=kwargs.get("text_toward_bg", 0.0),
                                     root_state=drop_down_state, path_prefix=())
    if is_open:
        if changed:
            _p = _dd_as_tuple(getattr(drop_down_state, "_picked_path", ()))
            drop_down_state.selected_path = _p
            drop_down_state.selected_label = _dd_label_for_path(collection, _p)
            if _DD_DBG:
                with open("/tmp/dd_debug.log", "a") as _f:
                    _f.write(f"[DD-DBG] CLOSE via menu-pick f={Melty.frame_count} name={name!r} picked={_p}\n")
            Melty.popover_focused_ds = None  # picking dismisses the popover
            _dd_close(drop_down_state)
            draw_state.invalidate()
            request_render()
            return True, new_item

        # The dropdown owns the keyboard while open: its search box holds text
        # focus (taken on open, released on close), so OTHER text editors gate off
        # (they all check Melty.text_focused_ds) and never act on the same keys. We
        # only handle nav keys while we actually hold that focus - that's what
        # keeps the arrow/Enter/Esc collisions with whatever editor was active.
        box_tile = getattr(drop_down_state, "_search_box_tile", None)
        text_focused = (Melty.text_focused_ds is not None and box_tile is not None
                        and getattr(Melty.text_focused_ds, "_tile_id", None) == box_tile)

        # Esc dismisses the open dropdown (and releases its text focus via
        # _dd_close). Ungated: the global text box Esc handler may have already
        # cleared the box's focus this same frame, so we don't require it here.
        if any(k == glfw.KEY_ESCAPE for k, _ in Core.melty.frame_key_events):
            if _DD_DBG:
                with open("/tmp/dd_debug.log", "a") as _f:
                    _f.write(f"[DD-DBG] CLOSE via Esc f={Melty.frame_count} name={name!r}\n")
            Melty.popover_focused_ds = None
            _dd_close(drop_down_state)
            draw_state.invalidate()
            request_render()
            return False, input_value

        # Arrows / Enter only while we own the keyboard, so they don't also drive
        # whatever editor was active when the dropdown opened.
        if text_focused:
            search = getattr(drop_down_state, "search", "") or ""
            picked = _dd_handle_keys(collection, drop_down_state, search=search,
                                     text_focused=text_focused)
            if picked is not UNSET_VALUE:
                _p = _dd_as_tuple(getattr(drop_down_state, "_picked_path", ()))
                drop_down_state.selected_path = _p
                drop_down_state.selected_label = _dd_label_for_path(collection, _p)
                Melty.popover_focused_ds = None
                _dd_close(drop_down_state)
                draw_state.invalidate()
                request_render()
                return True, picked

        # Click-outside dismissal: a fresh left click landing on neither the
        # trigger nor anywhere inside the popover subtree closes it (matches
        # native popover behaviour). Selection clicks already returned above, and
        # sub-menus open on hover, so this only fires for genuine outside clicks.
        if imgui.is_mouse_clicked(0) and not clicked:
            mx, my = imgui.get_mouse_pos()
            under = Core.melty.bvh_query(mx, my)
            if not any(_ds_in_subtree(ds, draw_state) for ds in under):
                if _DD_DBG:
                    with open("/tmp/dd_debug.log", "a") as _f:
                        _f.write(f"[DD-DBG] CLOSE via click-outside f={Melty.frame_count} name={name!r} "
                          f"mouse=({mx:.0f},{my:.0f}) under={[id(d) for d in under]} ds={id(draw_state)}\n")
                Melty.popover_focused_ds = None
                _dd_close(drop_down_state)
                draw_state.invalidate()
                request_render()

        # Focus settle (bounded, NOT a permanent repaint loop): right after open
        # the search box asks for text focus, but the opening click's
        # request_focus can race it the same frame. While the box hasn't
        # confirmed focus and we're still within the small retry budget, re-run
        # so it asks again; it gets within a frame or two and this stops.
        # Steady-state open repaints nothing - mouse changes invalidate via
        # _dd_set_cursor, keys via begin_frame. The retry must invalidate the
        # BOX's own chain (invalidate_up from its tile): the menu is a separate
        # cached window subtree, so invalidating just this trigger view never
        # re-rendered the box on a reopen and request_focus never re-fired.
        if getattr(drop_down_state, "_focus_search", 0) > 0:
            box_tile = getattr(drop_down_state, "_search_box_tile", None)
            if box_tile is not None:
                Melty.cache.invalidate_up(box_tile, force=True)
            Melty.cache.invalidate(draw_state._tile_id, force=True)
            request_render()

    return False, input_value


def _ds_in_subtree(node, ancestor, max_depth=64):
    """True if ``node`` is ``ancestor`` or a descendant of it. Walks the
    ``_parent`` chain, stopping on the root's self-loop (root._parent is root).
    A context-menu window counts as inside its TARGET's subtree: the menu's
    parent chain doesn't run through the view it was opened on, so a click in
    a menu over an open popover read as click-outside and dismissed the
    popover (and the menu with it). The menu is recognized by the existing
    mutual link — its input_value is the target draw_state, whose
    ``context_menu_ds`` points back at the menu — so no new state."""
    seen = 0
    while node is not None and seen < max_depth:
        if node is ancestor:
            return True
        target = getattr(node, "_raw_input_value", None)
        if target is not None and getattr(target, "context_menu_ds", None) is node:
            node = target
            seen += 1
            continue
        parent = getattr(node, "_parent", None)
        if parent is None or parent is node:
            break
        node = parent
        seen += 1
    return False


def _dd_entries(container):
    """Normalized (key, value, label, is_branch) rows for one level. Dict rows
    read by their key (small/medium); list/tuple rows by their value
    (left/center/right) since the index isn't meaningful to the user."""
    if isinstance(container, dict):
        items = list(container.items())
        labelled = [(k, v, str(k)) for k, v in items]
    else:
        labelled = [(i, v, str(v)) for i, v in enumerate(container)]
    return [(k, v, lbl, isinstance(v, (dict, list))) for k, v, lbl in labelled]


def _dd_subtree_matches(value, search):
    """True if `search` (already lowercased) appears anywhere in this value's
    subtree, so a branch stays visible while searching when a descendant matches."""
    if not search:
        return True
    if isinstance(value, dict):
        return any(search in str(k).lower() or _dd_subtree_matches(v, search)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(_dd_subtree_matches(v, search) for v in value)
    return search in str(value).lower()


def _dd_visible_entries(container, search=""):
    """Rows shown for a level under `search`: a leaf whose label matches, or a
    branch matching by label OR holding a matching descendant. Empty `search`
    keeps everything."""
    if not isinstance(container, (dict, list)):
        return []
    rows = _dd_entries(container)
    if not search:
        return rows
    return [(k, v, lbl, br) for (k, v, lbl, br) in rows
            if search in lbl.lower() or (br and _dd_subtree_matches(v, search))]


def _dd_walk(collection, path):
    """Descend `collection` along a key/index `path`, returning the node there or
    None if the path no longer resolves (e.g. after a search prunes it)."""
    node = collection
    for k in path:
        try:
            node = node[k]
        except (KeyError, IndexError, TypeError):
            return None
    return node


def _dd_rows_at(collection, path, search):
    """Visible rows at `path`, applying the once-a-branch-matches-by-label rule:
    if any ancestor key on `path` matched the search by its own label, that whole
    subtree counts as a match, so deeper levels are shown unfiltered."""
    container = _dd_walk(collection, path)
    ancestor_matched = bool(search) and any(search in str(k).lower() for k in path)
    return _dd_visible_entries(container, "" if ancestor_matched else search)


def _dd_first_match_leaf(container, search, prefix=()):
    """DFS for the path to the first selectable leaf the search reveals, so the
    cursor can jump straight to it (auto-expanding the branches above). A branch
    that matches by its own label contributes its first leaf unfiltered."""
    for key, value, label, is_branch in _dd_visible_entries(container, search):
        path = tuple(prefix) + (key,)
        if not is_branch:
            return path
        sub_search = "" if (search and search in label.lower()) else search
        sub = _dd_first_match_leaf(value, sub_search, path)
        if sub is not None:
            return sub
    return None


def _dd_as_tuple(x):
    """Coerce a stored path-state to a tuple. The states are meant to be key
    tuples, but DropDownState is a DictConversion and its machinery can alias a
    complex stored value (e.g. a Lora) across fields; this keeps the dropdown
    robust to any input type by never iterating a non-sequence."""
    if isinstance(x, tuple):
        return x
    if isinstance(x, list):
        return tuple(x)
    return ()


def _dd_fit_label(s, px):
    """`s` ellipsis-trimmed to render within `px` (imgui text metrics), so a
    fixed-width trigger never draws wider than its own button rect."""
    if px <= 0:
        return ""
    if imgui.calc_text_size(s)[0] <= px:
        return s
    while s and imgui.calc_text_size(s + "…")[0] > px:
        s = s[:-1]
    return s + "…"


def _dd_obj_tint(obj, fallback=None):
    """An object's embedded tint (a 3+-tuple `.tint`, e.g. on a Lora), else
    `fallback`. Used to colour each row by its value and the trigger by the
    selected value."""
    t = getattr(obj, "tint", None)
    if isinstance(t, (tuple, list)) and len(t) >= 3:
        return tuple(t)
    return fallback


def _dd_label_for_path(collection, path):
    """Display label for a selected leaf path: the KEY for a dict entry (e.g.
    "red"), the VALUE for a list entry (e.g. "left"). Used for the trigger title
    so it reads as a name, not a raw value (which may be a tuple/number)."""
    if not path:
        return ""
    parent = _dd_walk(collection, tuple(path[:-1]))
    if isinstance(parent, dict):
        return str(path[-1])
    return str(_dd_walk(collection, tuple(path)))


def _dd_set_cursor(root_state, cursor_path, is_branch, menu_ds=None):
    """Point the highlight at `cursor_path` and derive the open path from it: a
    branch expands its own sub-menu, a leaf collapses back to its parent level.
    This is the single writer for both hover and keyboard, so they stay in sync.

    When the OPEN path changes, the containing menu's row tiles must re-run:
    a branch row stamps `closed` on its submenu window only when its body
    actually runs, and the blit cache has no kwargs key — a clean row tile
    blit-skips even though its open_path input changed, leaving the submenu
    stamped open forever (one leaked submenu per branch row swept). Keyboard
    nav never leaked because begin_frame cascades invalidate_up from the menu
    tile on every nav key; this is the hover-side equivalent. `menu_ds` is the
    level's menu-window draw_state (row tiles are its direct tile children)."""
    if root_state is None:
        return
    new_cursor = tuple(cursor_path)
    new_open = tuple(cursor_path) if is_branch else tuple(cursor_path[:-1])
    old_open = _dd_as_tuple(root_state.open_path)
    if (new_cursor == _dd_as_tuple(root_state.cursor_path)
            and new_open == old_open):
        return
    root_state.cursor_path = new_cursor
    root_state.open_path = new_open
    if new_open != old_open:
        _dd_invalidate_rows(root_state, menu_ds)


def _dd_invalidate_rows(root_state, menu_ds=None):
    """Cascade-invalidate a menu window's tile subtree so every row body
    re-runs and re-stamps `closed` on its submenu from the current open_path.
    Without a menu_ds at hand (close paths), resolve the root menu through the
    search box tile — its parent_window is the root menu. bypass_clip so a
    branch row scrolled out of the menu viewport is cleaned too (otherwise
    scrolling it back in would revive its stale-open submenu). Submenu windows
    are tile ROOTS (not tile children of their spawner rows), so this reaches
    rows, not window interiors — which is all closing needs."""
    if menu_ds is None and root_state is not None:
        box_tile = getattr(root_state, "_search_box_tile", None)
        if box_tile is not None:
            box_ds = Melty.cache.key_to_draw_state.get(box_tile)
            menu_ds = getattr(box_ds, "parent_window", None) if box_ds is not None else None
    if menu_ds is None or getattr(menu_ds, "_tile_id", None) is None:
        return
    from src.lsd.gl_gui.view.invalidation_tracker import Note
    Melty.cache.invalidate_up(menu_ds._tile_id, force=True, bypass_clip=True,
                              note=Note(name="dd open_path change", tint=(1, 0.6, 0.2)))
    request_render()


def _dd_scroll_cursor_into_view(menu_ds, row_index, row0_offset=0.0):
    """Nudge a menu window's scroll_offset the minimal amount so keyboard-cursor
    row `row_index` is fully visible. Rows are a fixed _DD_ROW_H pitch drawn
    flush from the content origin in the manual-loop path; `row0_offset` covers
    a level that draws chrome above its rows. Stateless — pure geometry from
    the live draw_state, clamped to the wrapper-published _max_scroll_y so this
    writer never fights core_render's own clamp."""
    if menu_ds is None or row_index is None or row_index < 0:
        return
    view_h = menu_ds.abs_clipped_height - menu_ds.header_height - menu_ds.footer_height
    sx, sy = menu_ds.scroll_offset
    row_top = row0_offset + row_index * _DD_ROW_H
    row_bot = row_top + _DD_ROW_H
    new_sy = sy
    if view_h > 0 and row_bot > new_sy + view_h:    # below the viewport: must scroll down
        new_sy = row_bot - view_h
        max_y = menu_ds._max_scroll_y
        if max_y is not None:
            new_sy = min(new_sy, max_y)
    if row_top < new_sy:                            # above the viewport (top-aligns a tiny view)
        new_sy = row_top
    new_sy = max(0.0, new_sy)
    if new_sy != sy:
        menu_ds.scroll_offset = (sx, new_sy)
        # Own the repaint edge: scroll is BAKED into the blit capture at render
        # time and only the wheel-event callback repaints on scroll change, so a
        # clean tile would replay the old-offset capture (the caller's repaint gate
        # doesn't always fire - e.g. a snap-to-top when the filtered list is
        # value-identical). Change-edge-gated by `new_sy != sy` above, never
        # per-frame. bypass_clip: the popup can protrude outside its parent.
        if menu_ds._tile_id is not None:
            from src.lsd.gl_gui.view.invalidation_tracker import Note
            Melty.cache.invalidate_up(menu_ds._tile_id, force=True, bypass_clip=True,
                                      note=Note(name="dd scroll-into-view", tint=(1, 0.6, 0.2)))
        request_render()


def _dd_close(root_state):
    """Reset popover state on close: collapse the open/cursor paths, clear the
    search query, and release the search box's text focus if it held it."""
    if root_state is None:
        return
    open_was = _dd_as_tuple(root_state.open_path)
    root_state.open_path = ()
    root_state.cursor_path = ()
    root_state.search_query = ""
    root_state.search = ""
    root_state._focus_search = 0
    root_state._had_focus = False
    # A branch row left stamped open would blit-skip on reopen and revive its
    # submenu window even though open_path was reset - dirty the rows now (the
    # flags persist while the menu is hidden) so the first reopen render
    # re-stamps every submenu closed.
    if open_was:
        _dd_invalidate_rows(root_state)
    box_tile = getattr(root_state, "_search_box_tile", None)
    tf = Melty.text_focused_ds
    if tf is not None and box_tile is not None and getattr(tf, "_tile_id", None) == box_tile:
        Melty.text_focused_ds = None


def _dd_handle_keys(collection, root_state, search="", text_focused=False):
    """Arrow-key navigation while the popover is open. Up/Down move within the
    current level, Right (or Enter on a branch) descends, Left collapses to the
    parent, Enter on a leaf picks it. Returns the picked leaf value, or
    UNSET_VALUE when nothing was chosen this frame. Reads the GLFW-callback key
    queue so it works without the menu being hovered."""
    if root_state is None:
        return UNSET_VALUE
    keys = list(Core.melty.frame_key_events)

    def pressed(*codes):
        return any(k in codes for k, _ in keys)

    down = pressed(glfw.KEY_DOWN)
    up = pressed(glfw.KEY_UP)
    right = pressed(glfw.KEY_RIGHT)
    left = pressed(glfw.KEY_LEFT)
    enter = pressed(glfw.KEY_ENTER, glfw.KEY_KP_ENTER)
    # While typing a query, Left/Right are the search box's text cursor, not tree
    # navigation, so the dropdown doesn't double-act on them.
    if text_focused and search:
        left = right = False
    if not (down or up or right or left or enter):
        return UNSET_VALUE

    # A nav key fired: switch to keyboard-select mode so hover stops moving the
    # cursor until the mouse actually moves again (cleared in draw_dropdown).
    root_state._kbd_mode = True

    # Navigation level = the cursor's parent; rows = its (search-filtered)
    # siblings. Fall back to the root level if the cursor path went stale.
    cursor = _dd_as_tuple(getattr(root_state, "cursor_path", ()))
    level = cursor[:-1]
    rows = _dd_rows_at(collection, level, search)
    if not rows:
        level = ()
        rows = _dd_rows_at(collection, level, search)
        cursor = ()
    if not rows:
        return UNSET_VALUE

    level_keys = [r[0] for r in rows]
    had_cursor = bool(cursor) and cursor[-1] in level_keys
    idx = level_keys.index(cursor[-1]) if had_cursor else 0

    # Left collapses the current sub-menu and highlights its parent row.
    if left and level:
        root_state.cursor_path = tuple(level)
        root_state.open_path = tuple(level[:-1])
        request_render()
        return UNSET_VALUE

    # From no selection, the first Up/Down just focuses on the first row; otherwise
    # it steps (wrapping). Right/Enter act on whatever row is current.
    if had_cursor:
        if down:
            idx = (idx + 1) % len(rows)
        elif up:
            idx = (idx - 1) % len(rows)
    key, value, label, is_branch = rows[idx]
    new_cursor = tuple(level) + (key,)

    # Right / Enter on a branch descends into its first visible child.
    if (right or enter) and is_branch:
        kids = _dd_rows_at(collection, new_cursor, search)
        if kids:
            ck, cv, _cl, cbr = kids[0]
            _dd_set_cursor(root_state, new_cursor + (ck,), cbr)
            request_render()
            return UNSET_VALUE

    if enter and not is_branch:
        _dd_set_cursor(root_state, new_cursor, False)
        root_state._picked_path = tuple(new_cursor)
        return value

    _dd_set_cursor(root_state, new_cursor, is_branch)
    request_render()
    return UNSET_VALUE


_DD_MENU_W = 170
_DD_ROW_H = 24



def _dd_row_lookup(mapping, value):
    """mapping.get(value), tolerant of UNHASHABLE row values — a BRANCH row's
    value is the nested collection dict itself, which raised TypeError from
    every value-keyed style lookup (row_tags/row_tints/...)."""
    if not mapping:
        return None
    try:
        return mapping.get(value)
    except TypeError:
        return None


def _dd_noop_set(*_a, **_k):
    """No-op set_attr for the draw_collection rendering a dropdown level: the rows
    list is rebuilt each frame, so draw_collection must NOT write a picked value
    back into it — the pick is surfaced via return_item instead."""
    return None


def _dd_leaf_row(key, value, label, draw_state, root_state, path_prefix,
                 cursor_path, tint=None, row_tags=None, row_tints=None,
                 row_suffixes=None, row_actions=None, left_pad=10,
                 text_toward_bg=0.0):
    """Render ONE leaf menu row inline with raw imgui — NO per-row render_func.
    Leaves are the bulk of a big menu, so skipping the dd_menu_row wrapper (its
    own draw_state / cache / BVH / hover machinery, tens of µs each) is the whole
    point: it's what made the 967-icon list crawl while hovering/scrolling. The
    parent draw_dd_menu tile already re-renders every frame it's bounding-hovered
    (core_render hover invalidation), so this row's hover highlight / click stay
    live without a tile of its own. Branch rows still go through dd_menu_row — they
    own a nested submenu and a real draw_state. Returns the picked value when
    clicked, else UNSET_VALUE.

    `draw_state` is the MENU window's draw_state (the level), not a per-row one."""
    row_path = tuple(path_prefix) + (key,)
    is_cursor = _dd_as_tuple(cursor_path) == row_path
    kbd_mode = getattr(root_state, "_kbd_mode", True)

    pos = imgui.get_cursor_screen_pos()
    x, y = pos[0], pos[1]
    w = draw_state.content_width or 0
    h = _DD_ROW_H
    mp = imgui.get_mouse_pos()
    hovered = (x <= mp[0] < x + w) and (y <= mp[1] < y + h)
    if hovered:
        # Clamp hover to the menu window's visible band: rows laid out above the
        # window bounds (scrolled/clipped away) are invisible but this raw imgui
        # math would still hit them - a click on editor text under the popup
        # could pick an unseen row.
        _wt = draw_state._abs_top()
        if not (_wt <= mp[1] < _wt + (draw_state.height or 0)):
            hovered = False
    if hovered and not kbd_mode:
        _dd_set_cursor(root_state, row_path, False, menu_ds=draw_state)

    active = is_cursor if kbd_mode else hovered
    dl = imgui.get_window_draw_list()
    line_h = imgui.get_text_line_height()
    # Per-row symbol tint (the autocomplete popup's background colors): drawn
    # as a colored BACKGROUND wash with near-white text over it - the editor's
    # wash styling - rather than colored text. Under the active highlight so
    # keyboard/hover selection still reads on tinted rows.
    row_tint = _dd_row_lookup(row_tints, value)
    _ROW_TINT_A = 0.35
    _ROW_TINT_V_CAP = 0.55   # max RGB component - near-white text must stay legible
    _ROW_TINT_S_BOOST = 1.25  # saturation boost on capped tints - keeps hue vivid
    if row_tint is not None:
        r, g, b = row_tint[:3]
        v = max(r, g, b)
        if v > _ROW_TINT_V_CAP:
            k = _ROW_TINT_V_CAP / v
            r, g, b = r * k, g * k, b * k
            # Boost saturation by pulling the lower channels up from the max
            # (raw channel arithmetic - hue is the channel ORDER, matters).
            m = max(r, g, b)
            r = max(0.0, m - (m - r) * _ROW_TINT_S_BOOST)
            g = max(0.0, m - (m - g) * _ROW_TINT_S_BOOST)
            b = max(0.0, m - (m - b) * _ROW_TINT_S_BOOST)
        dl.add_rect_filled(x, y + 1, x + w, y + h - 1,
                           imgui.get_color_u32_rgba(r, g, b, _ROW_TINT_A),
                           rounding=getattr(draw_state, 'corner_radius', 6))
    if active:
        dl.add_rect_filled(x, y, x + w, y + h,
                           imgui.get_color_u32_rgba(1, 1, 1, 0.16),
                           rounding=getattr(draw_state, 'corner_radius', 6))

    # Raw imgui.text_colored() for the label, coloured by the value's embedded
    # tint (e.g. a Lora's .tint) falling back to the menu tint - same source as
    # dd_menu_row. Vertically centred in the fixed-height row: set_cursor pins the
    # next row exactly h below (matches the full menu's item_spacing.y=0), so
    # leaves and dd_menu_row branches line up.
    if row_tint is not None:
        # White nudged toward the wash color: legible on the tinted bar while
        # still reading as that symbol's hue.
        color = tuple(min(1.0, c * 0.25 + 0.75) for c in row_tint[:3])
    else:
        color = _dd_obj_tint(value, tint)
        color = Tint.dd_text(requested_tint=color)
        # Quiet-row dimming: pull the label toward the menu bg so tinted
        # rows (the info popup's active-source yellow) stand out. Skipped for
        # washed rows above - their label is already contrast-managed.
        if text_toward_bg and Melty.bg_color_stack:
            _bg = Melty.bg_color_stack[-1]
            _k = min(max(float(text_toward_bg), 0.0), 1.0)
            color = tuple(c * (1 - _k) + _b * _k
                          for c, _b in zip(color[:3], _bg[:3]))
    imgui.set_cursor_screen_pos((x + left_pad, y + (h - line_h) * 0.5))

    if active:
        # Active-row label: lerp toward white so it clears the white@0.16 wash
        # (the wash lightens the row fill while the label is its plain text
        # value - light-on-light, 2.8:1 in a dark-tinted editor). Keeping 40%
        # of the original chroma leaves per-item tints recognizable.
        color = tuple(min(1.0, c * 0.4 + 0.6) for c in color[:3])
    color = *(color[:3]), 1.0
    imgui.text_colored(str(label), *color)

    # Dim '(param, param2)' suffix right after a callable's signature (autocomplete
    # rows): same tint as the label but reduced alpha, so it's subtle on
    # plain, tinted and active rows alike. imgui text (not raw dl.add_text) so
    # the row's measured width includes it and auto-resize fits the popup.
    sfx = _dd_row_lookup(row_suffixes, value)
    if sfx:
        imgui.same_line(spacing=0)
        imgui.text_colored(sfx, color[0], color[1], color[2], 0.45)

    imgui.set_cursor_screen_pos((x, y + h))

    # Dimmed tag, right-aligned (autocomplete's func/class/... label). Opaque
    # backing mask first so a long label can't run off it; the mask reuses the
    # menu fill (+active wash) so it's invisible on plain and highlighted rows.
    tag = _dd_row_lookup(row_tags, value)
    if tag:
        tw = imgui.calc_text_size(tag).x
        tx = x + w - tw - 10
        ty = y + (h - line_h) * 0.5
        if Melty.bg_color_stack:
            r, g, b = Melty.bg_color_stack[-1][:3]
            if row_tint is not None:
                # Match the tinted wash under it, else the opaque mask reads
                # as a gray notch on top of the colored bar.
                r = r * (1 - _ROW_TINT_A) + row_tint[0] * _ROW_TINT_A
                g = g * (1 - _ROW_TINT_A) + row_tint[1] * _ROW_TINT_A
                b = b * (1 - _ROW_TINT_A) + row_tint[2] * _ROW_TINT_A
            if active:
                r, g, b = r * 0.84 + 0.16, g * 0.84 + 0.16, b * 0.84 + 0.16
            mask = imgui.get_color_u32_rgba(min(max(r, 0.0), 1.0), min(max(g, 0.0), 1.0),
                                            min(max(b, 0.0), 1.0), 1.0)
            dl.add_rect_filled(tx - 6, y + 1, tx + tw + 6, y + h - 1, mask)
        dl.add_text(tx, ty, imgui.get_color_u32_rgba(0.55, 0.6, 0.72, 0.85), tag)

    # Per-row ACTION (`row_actions`: dict value→callable, or single callable for
    # every row): a right-aligned trash icon whose click runs the action and
    # CONSUMES the click - no pick, popover stays open, so several rows can
    # be acted on in one visit (the info tab's clear-at-action).
    _act = None
    if row_actions is not None:
        _act = (_dd_row_lookup(row_actions, value)
                if isinstance(row_actions, dict) else row_actions)
    if _act is not None:
        _ax = x + w - 24
        _over_act = hovered and mp[0] >= _ax - 4
        dl.add_text(_ax, y + (h - line_h) * 0.5,
                    imgui.get_color_u32_rgba(0.85, 0.32, 0.28,
                                             0.95 if _over_act else 0.4),
                    "")
        if _over_act and imgui.is_mouse_clicked(0):
            _act(value)
            request_render()
            return UNSET_VALUE

    if hovered and imgui.is_mouse_clicked(0):
        root_state._picked_path = tuple(row_path)
        return value
    return UNSET_VALUE


@render_func(use_cache=True, show_bg=True, shadow=True, selectable=False, temp=True,
             closable=True, melty_window=False, auto_resize=True, with_header=None, 
             max_height=420, min_width=300, swoosh=False, min_height=33)
def draw_dd_menu(input_value, draw_state, root_state=None, unique=0, path_prefix=(), tint=None,
                 show_search=True, text_align="right", row_tags=None, row_tints=None,
                 row_suffixes=None, row_actions=None, text_toward_bg=0.0,
                 full_render=False, **kwargs):
    """One level of the dropdown, drawn as its own temp popover window. Iterates
    the level's entries and renders each as a row (`_dd_menu_row`); a leaf click
    or a pick inside a nested sub-menu bubbles back up as (changed, value).

    Expansion is driven by `root_state.open_path` (a single chain of keys, see
    DropDownState) — NOT by each row testing its own hover. A row renders its
    sub-menu only when `open_path` runs through it, so at most one sub-menu is
    open per level and hidden siblings can never resurface. `path_prefix` is this
    level's key chain from the root; each row's full path is prefix + its key.

    `show_search` draws the root-level filter box. Autocomplete (the code editor's
    suggestion popup) passes False: the editor itself owns text focus and the
    half-typed identifier IS the filter, so a second focus-stealing search box
    would fight it. The caller pre-filters the rows in that case.

    The popover and its rows are CACHED tiles with NO kwargs cache key —
    a clean row tile blit-skips even when its cursor/open path inputs changed.
    Repaints are therefore driven by explicit invalidation only: an open_path
    change cascades invalidate_up from the menu tile inside _dd_set_cursor
    (this is what re-runs a stale branch row so it stamps its submenu window
    closed — see the leak note there), keys via the begin_frame popover hook
    (or the code editor's per-event invalidate_up) — invalidate_up
    specifically, since it cascades to the row tiles; a plain invalidate
    leaves the inner dd_rows collection clean and it blit-skips."""
    if show_search and not path_prefix and root_state is not None:
        # Root owns the search box. Single-line so Up/Down/Enter pass through to
        # menu nav; it auto-focuses once when the menu opens (_focus_search).
        q = getattr(root_state, "search_query", "") or ""
        box = draw_text(q, name=f"dd_search{unique}", show_name=False, searchable=False,
                        single_line=True, is_search_box=True, is_tree=False,
                        with_header=None, with_footer=None, show_bg=True, shadow=False,
                        request_focus=getattr(root_state, "_focus_search", 0) > 0,
                        font=Font.JETBRAINS_MONO_19,
                        tint=tint, return_extras=True)
        q_changed, new_q = box[0], box[1]
        box_ds = box[2] if len(box) > 2 else None
        if box_ds is not None:
            root_state._search_box_tile = box_ds._tile_id
            # Focus retry is a bounded countdown: the first popover's
            # move-to-front clears text focus the frame the box first grabs it
            # (apply_move_to_front(): parent != front window), so a one-shot
            # request is lost. Re-request for a few frames until it lands, then
            # clear (0). Draw_dropdown drives the re-runs while this is > 0.
            if Melty.text_focused_ds is box_ds:
                root_state._focus_search = 0
            elif getattr(root_state, "_focus_search", 0) > 0:
                root_state._focus_search -= 1
        new_search = str(new_q or "").strip().lower()
        if q_changed:
            root_state.search_query = new_q
            # Jump the cursor onto the first matching leaf, auto-expanding all
            # branches above it, so the leaf is visible and one Enter selects it
            # (instead of Enter-to-open-then-Enter-to-pick).
            if new_search:
                leaf = _dd_first_match_leaf(input_value, new_search)
                if leaf is not None:
                    _dd_set_cursor(root_state, leaf, False, menu_ds=draw_state)
                else:
                    _dd_set_cursor(root_state, (), False, menu_ds=draw_state)
            else:
                _dd_set_cursor(root_state, (), False, menu_ds=draw_state)
        root_state.search = new_search

    search = str(getattr(root_state, "search", "") or "")
    # Cursor/open paths are passed down as ROW INPUTS (not read by the row's
    # root_state): a row is a use_cache=True tile, so its highlight only repaints
    # when an input changes. Throwing the paths through makes keyboard nav and the
    # default-selection-on-open repaint (hover repaints via a separate hook).
    open_path = _dd_as_tuple(getattr(root_state, "open_path", ()))
    cursor_path = _dd_as_tuple(getattr(root_state, "cursor_path", ()))

    ancestor_matched = bool(search) and any(search in str(k).lower() for k in path_prefix)
    rows = _dd_visible_entries(input_value, "" if ancestor_matched else search)
    # if not rows and search:
    #     imgui.dummy(180, 6)
    #     text("  No matches", width=180, height=_DD_ROW_H, name="dd_nomatch",
    #          text_colour=(1, 1, 1))
    #     return False, None

    # Per-row kwargs shared by both render paths. Each row is a
    # (key, value, label, is_branch) tuple handed to dd_menu_row, which computes
    # its own path/cursor/open state from path_prefix + root_state. full_render is
    # threaded down so nested sub-menus inherit the same render path.
    row_kwargs = dict(show_bg=False, shadow=False, path_prefix=tuple(path_prefix),
                      root_state=root_state, tint=tint, text_align=text_align, z_offset=0,
                      row_tags=row_tags, row_tints=row_tints, row_suffixes=row_suffixes,
                      cursor_path=cursor_path,
                      open_path=open_path, full_render=full_render)

    if full_render:
        # Render the level's rows through draw_collection so it scrolls +
        # virtualizes (off-screen culling) for free - the manual loop below
        # rendered EVERY row each frame, which crawled for the 967-icon list. A
        # leaf click / sub-menu pick bubbles back as the changed item via
        # return_item; the no-op set_attr keeps draw_collection from writing that
        # value back into `rows`.
        changed, picked = draw_collection(
            rows, name=f"dd_rows_{unique}", show_search=False, show_bg=True
            ,
            with_header=None, mode=None, return_item=True, set_attr=_dd_noop_set,
            item_spacing_y=0, use_cache=True,
            child_kwargs={"view_func": dd_menu_row, **row_kwargs})
        return (True, picked) if changed else (False, input_value)

    # Old method: manually iterate and render EVERY row each frame (no scroll /
    # virtualization). Branch rows still go through the dd_menu_row render_func
    # (they own a nested submenu + a real draw_state); leaf rows - the bulk of a
    # big list - are drawn inline by _dd_leaf_row with raw imgui, avoiding avoid
    # per-row wrapper overhead that made big menus crawl while interacting.
    result = (False, input_value)
    for idx, row in enumerate(rows):
        key, value, label, is_branch = row
        if is_branch:
            changed, picked = dd_menu_row(row, name=f"ddrow_{idx}_{key}", **row_kwargs)
            if changed:
                result = (True, picked)
        else:
            picked = _dd_leaf_row(key, value, label, draw_state, root_state,
                                  tuple(path_prefix), cursor_path, tint=tint,
                                  row_tags=row_tags, row_tints=row_tints,
                                  row_suffixes=row_suffixes,
                                  row_actions=row_actions,
                                  text_toward_bg=text_toward_bg)
            if picked is not UNSET_VALUE:
                result = (True, picked)
    return result

@render_func(use_cache=True, show_bg=False, shadow=False, selectable=False, temp=True, show_add_delete=False,
             with_header=None, disable_scroll=True, min_width=300, swoosh=False, z_offset=-3)
def dd_menu_row(input_value, draw_state, text_align="right", path_prefix=(),
                root_state=None, tint=None, row_tags=None, row_tints=None,
                cursor_path=(), open_path=(), full_render=True, **kwargs):
    """A single menu row. `input_value` is the row TUPLE (key, value, label,
    is_branch) — draw_collection hands each level's rows in one at a time (so it
    can scroll / virtualize the level for free). `cursor_path` / `open_path` are
    passed IN (not read from root_state) so they're cache-key inputs: a row is a
    use_cache=True tile, so its keyboard highlight / open chevron only repaint when
    an input changes. Leaves are a button returning the VALUE on click; branch rows
    show a chevron and own a nested `draw_dd_menu` to their right, shown only while
    on the open path. Hovering points the shared cursor here; the highlight is
    painted over the row box when hovered / keyboard-current.

    `row_tags` (optional) maps a value -> short dim string drawn right-aligned —
    the code editor's completion popup uses it for the kind label (func/class/…)."""
    key, value, label, is_branch = input_value
    row_path = tuple(path_prefix) + (key,)
    open_path = _dd_as_tuple(open_path)
    cursor_path = _dd_as_tuple(cursor_path)
    sub_open = open_path[:len(row_path)] == row_path
    is_cursor = cursor_path == row_path
    tag = _dd_row_lookup(row_tags, value)

    hovered = draw_state._bounding_hovered
    # Colour the row by its value's embedded tint (e.g. a Lora's .tint), falling
    # back to the per-row override (autocomplete's symbol tints), then the menu
    # tint for plain values.
    row_tint = _dd_row_lookup(row_tints, value)
    tint = _dd_obj_tint(value, row_tint or tint)
    fa_chrevron_right = f"\uf054"

    kbd_mode = getattr(root_state, "_kbd_mode", True)
    if hovered and not kbd_mode:
        _dd_set_cursor(root_state, row_path, is_branch,
                       menu_ds=draw_state.parent_window)

    active = is_cursor if kbd_mode else hovered
    if active:
        dl = imgui.get_window_draw_list()
        dl.add_rect_filled(draw_state.abs_left, draw_state.abs_top,
                           draw_state.abs_left + draw_state.width,
                           draw_state.abs_top + draw_state.height,
                           imgui.get_color_u32_rgba(1, 1, 1, 0.16),
                           rounding=getattr(draw_state, 'corner_radius', 6))

    chevron = f"  {fa_chrevron_right}" if is_branch else "    "  # fa-chevron-right
    if not full_render:
        # Lightweight row: draw the label with raw imgui.text() instead of the
        # full button/draw_text render_func. There's no click report from
        # imgui.text(), so derive one from this row's hover (the caller's
        # bounding-box hover) plus a fresh left mouse-down.
        imgui.text(f"{label}{chevron}")
        clicked = hovered and imgui.is_mouse_clicked(0)
    elif is_branch:
        # +0.9 value on the active row lifts the label over the white@0.16 wash
        # (button's own +1.5 hover boost keys off `hovered`, which is False for
        # the keyboard-cursor row); hue is retained, only brightness moves.
        clicked, _ = button(f"{label}{chevron}", name=f"{label}_ddrow", width=draw_state.content_width - 10,
                            height=_DD_ROW_H, hovered=hovered, text_value=0.56 + (0.9 if active else 0.0),
                            text_saturation=1.349, shadow=False,
                             rounding=0, show_button_bg=False, show_bg=False, use_cache=True,
                            text_align=text_align, tint=tint)
    else:
        clicked, _ = button(f"{label}{chevron}", name=f"{label}_ddrow", show_button_bg=False,
                            width=draw_state.content_width - 10, height=_DD_ROW_H, hovered=hovered,
                            text_saturation=0.716, z_offset=0, shadow=False, show_bg=False, use_cache=True,
                            text_align=text_align, tint=tint)

    # In keyboard-select mode the arrow keys paint the highlight; hover neither
    # moves the cursor nor paints, until the mouse moves (draw_dropdown clears it).


    # ONE highlight, framed to this row's box. Mouse mode keys off the live hover;
    # keyboard mode keys off the cursor. Using a separate source per mode (rather
    # than hover OR cursor) avoids briefly painting both the stale-cursor row and
    # the freshly-hovered row, which doubled the wash and looked inconsistent.


    # Dim kind tag, right-aligned over the row (drawn last so it sits above the
    # highlight). The name is left-aligned by the caller's text_align. A long
    # label could run under a long tag, so the tag gets an opaque backing rect
    # first: the parent window's actual bg fill (bg_color_stack top = nearest
    # show_bg ancestor) with the active-row wash (white @ 0.16, see above)
    # re-composed in, so the mask is invisible on both active and highlighted
    # rows while still cutting off the label.
    if tag:
        dl = imgui.get_window_draw_list()
        tw = imgui.calc_text_size(tag).x
        tx = draw_state.abs_left + draw_state.width - tw - 10
        ty = draw_state.abs_top + (draw_state.height - imgui.get_text_line_height()) * 0.5
        if Melty.bg_color_stack:
            r, g, b = Melty.bg_color_stack[-1][:3]
            if active:
                r, g, b = r * 0.84 + 0.16, g * 0.84 + 0.16, b * 0.84 + 0.16
            mask = imgui.get_color_u32_rgba(min(max(r, 0.0), 1.0), min(max(g, 0.0), 1.0),
                                            min(max(b, 0.0), 1.0), 1.0)
            dl.add_rect_filled(tx - 6, draw_state.abs_top + 1,
                               tx + tw + 6, draw_state.abs_top + draw_state.height - 1, mask)
        dl.add_text(tx, ty, imgui.get_color_u32_rgba(0.55, 0.6, 0.72, 0.85), tag)

    if is_branch:
        # Always call the sub-menu (so off-path ones stay registered but hidden
        # via closed=True, never leaving a stale painted frame); only the on-path
        # branch actually draws. Pinned to the right of this row with window_pos.
        changed, picked = draw_dd_menu(value, name=f"{label}_submenu", tint=tint,
                                       closed=not sub_open, temp=True, use_cache=False,
                                       window_pos=(draw_state.width, -_DD_ROW_H), show_add_delete=False,
                                       parent_window=draw_state, disable_scroll=False,
                                       full_render=full_render, row_tints=row_tints,
                                       root_state=root_state, path_prefix=row_path)
        if changed:
            return True, picked
    elif clicked:
        root_state._picked_path = tuple(row_path)
        return True, value

    return False, value


@render_func(is_default_for=(DrawState), tint=(0.2, 0.6, 0.8), show_bg=True, shadow=False, with_header=None)
def draw_draw_state_info(input_value: DrawState):
    imgui.text(f"DrawState")
    imgui.text(f"Tile ID: {input_value._tile_id}")
    imgui.text(f"Content WxH: {input_value.content_width} x {input_value.content_height}")


@render_func(use_cache=True, with_header=draw_header, show_bg=True, is_default_for=Pending)
def draw_pending(input_value, draw_state=None):
    imgui.text(input_value.originated.__name__)
    imgui.push_text_wrap_pos(draw_state.left + draw_state.content_width)
    imgui.text_wrapped(str(input_value.status))
    imgui.pop_text_wrap_pos()

    return False, input_value


from src.lsd.gl_gui.model.core_model.core_enums import PendingAction


@render_func(use_cache=True, show_header=False, shadow=True)
def pending_window(input_value, button_name, pending=None, draw_state=None,
                   show_revert=False, show_load=False):
    draw_text(str(pending.status), width=draw_state.width, name="Status", show_bg=True, shadow=False, with_footer=None)
    imgui.dummy(0, 5)

    if button(str(button_name), width=100, height=25)[0]:
        return True, PendingAction.APPLY
    if show_revert:
        same_line()
        if button("Revert", width=100, height=25, color=(0.8, 0.3, 0.3),
                  factor=0.3, value=0.0, text_value=2.0, saturation=0.4)[0]:
            return True, PendingAction.REVERT
    if show_load:
        same_line()
        if button("Load", width=100, height=25, color=(0.3, 0.5, 0.8), factor=0.8)[0]:
            return True, PendingAction.LOAD

    return False, input_value


@render_func(use_cache=True, max_height=893, auto_resize=False, min_width=702, layer_offset=1, searchable=False)
def draw_search(input_value=None, draw_state=None, unique=0):
    """Floating find bar for searchable views that have no header. Rendered as
    a Mode.WINDOW from core_render when search is active; draws the shared
    render_search UI against the owning view's draw_state (search_owner)."""
    owner = input_value
    # draw_search(owner, unique=owner._tile_id, draw_state=draw_state)
    search_ds = owner
    regrab_focus = True

    from src.lsd.gl_gui.view.core_views.text_editor import draw_text
    # Re-grab gated on this search still being active (focused_ds is the owner):
    # a deliberate click away clears focused_ds (via clear_focus) so the box
    # releases focus and stays open-but-unfocused, while a spurious clear during
    # typing leaves focused_ds intact so the box reclaims focus.
    focus_search = ((not search_ds._search_was_active)
                    or (regrab_focus and Melty.text_focused_ds is None
                        and Melty.focused_ds is search_ds)
                    or search_ds._search_focus_pending)
    # One-shot open/Ctrl+F frame (NOT the spurious-clear regrab): select the
    # whole term so typing replaces, and a single delete clears it.
    focus_fresh = ((not search_ds._search_was_active)
                   or search_ds._search_focus_pending)
    search_ds._search_focus_pending = False
    search_ds._search_was_active = True
    search_icon = ""
    imgui.align_text_to_frame_padding()
    imgui.text(search_icon)
    imgui.same_line()
    x_width = 30

    width = draw_state.content_width
    _box = draw_text(search_ds.search_text, searchable=False, is_search_box=True,
                     width=draw_state.content_width - x_width - 24,
                     shadow=False, name=search_icon + str(unique),
                     with_header_end=None, wrap=True, z_offset=-1, single_line=True,
                     with_footer=None, tint=search_ds.tint,
                     show_name=False, show_header=False,
                     request_focus=focus_search, select_all_on_focus=focus_fresh,
                     return_extras=True)
    search_change, new_search = _box[0], _box[1]
    _box_ds = _box[2] if len(_box) > 2 else None
    # While the find box holds text focus, mark this search as the active one so
    # Enter/arrow nav goes here - including after clicking back into the box.
    if _box_ds is not None and Melty.text_focused_ds is _box_ds:
        Melty.focused_ds = search_ds
    if search_change:
        search_ds.search_text = new_search
        # Re-render the owner's whole subtree so every child view recomputes its
        # matches against the new term and the combined index stays in sync.
        Melty.cache.invalidate_up(search_ds._tile_id, force=True, max_depth=12)
        request_render()
    # initial use

    # Match count + prev/next buttons. The count and current index are
    # populated by the searchable view's body (e.g. the text editor); the
    # arrows step the active match and ask the body to scroll it into view.

    imgui.same_line()
    from src.lsd.gl_gui.view.core_views.new_core_view import button
    fa_x_icon = ""

    imgui.set_cursor_screen_pos((draw_state.abs_left + width - x_width, imgui.get_cursor_screen_pos()[1]))
    # imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0], imgui.get_cursor_screen_pos()[1] + 2))
    if button(fa_x_icon, name=f"{unique}##fa_x_icon", show_bg=False,
              use_cache=True, height=23, shadow=True, z_offset=4, max_height=40,
              tile_mode=TileMode.MAX, color=(9, 1, 1, 0))[0]:
        search_ds.search_active = False
        search_ds._search_was_active = False
        # Keep search_text so reopening the find bar restores the last query.
        Melty.text_focused_ds = None

    total = search_ds.text_search_count
    if total > 0:
        imgui.align_text_to_frame_padding()
        imgui.text_colored(f"{search_ds.text_search_current + 1}/{total}",
                           0.66, 0.74, 0.82, 1.0)
        imgui.same_line(spacing=2)
        nav = 0
        if imgui.small_button(f"##search_prev{unique}"):
            nav = -1
        imgui.same_line(spacing=2)
        if imgui.small_button(f"##search_next{unique}"):
            nav = 1
        # Enter / Down = find next, Shift+Enter / Up = find prev, Ctrl+Enter =
        # "click" the selected result - but only while the FIND BOX (not the
        # underlying editor) holds text focus, so Enter still inserts newlines
        # when you click into the editor. We gate on the box holding text focus
        # directly rather than on `focused_ds is search_ds`: clicking back into
        # the box runs clear_focus, which nulls focused_ds (the searched view
        # isn't under the mouse to be protected), so keying off focused_ds
        # silently dropped Enter-nav after a mouse refocus. text_focused_ds is
        # set straight by the box's own key handler, so it survives that.
        # The find box is single-line, so Up/Down don't move its cursor and are
        # free for stepping matches. Drained from the frame-callback key queue
        # (not imgui.is_key_pressed) so it isn't dropped on slow frames.
        if _box_ds is not None and Melty.text_focused_ds is _box_ds:
            if any(k == glfw.KEY_DOWN for k, _ in Melty.frame_key_events):
                nav = 1
            elif any(k == glfw.KEY_UP for k, _ in Melty.frame_key_events):
                nav = -1
            # Enter steps to the next match, Shift+Enter to the previous, and
            # HOLDING Enter rapid-fires - imgui's synthesized auto-repeat
            # (io.key_repeat_delay/rate) is read here because GLFW's REPEAT events
            # don't reach the key queue on every platform (Wayland); keep the loop
            # rendering while it is held so that cadence is sampled. Ctrl+Enter
            # "clicks" the selected result and stays single-shot (from the queue).
            _enter = [m for k, m in Melty.frame_key_events
                      if k in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER)]
            _enter_repeat = (imgui.is_key_pressed(glfw.KEY_ENTER, repeat=True)
                             or imgui.is_key_pressed(glfw.KEY_KP_ENTER, repeat=True))
            if imgui.is_key_down(glfw.KEY_ENTER) or imgui.is_key_down(glfw.KEY_KP_ENTER):
                request_render()
            if _enter or _enter_repeat:
                if _enter and (_enter[-1] & glfw.MOD_CONTROL):
                    # "Click" the selected result: hit-test the BVH at the center
                    # of the current match's rect and send a mouse down to the
                    # front-most view there (the actual target, e.g. a managed
                    # window's name label) - exactly what a real click resolves
                    # to. The view's own click handling does the rest (toggle a
                    # window, focus an input, ...). Queued + the view invalidated so
                    # it re-renders and reads the click next frame (the find UI
                    # renders too late to inject for this frame).
                    from src.lsd.gl_gui.view.core_views.new_core_view import search_activate_target
                    from src.lsd.gl_gui.events.input_handler import InputEvent
                    _target = search_activate_target(Melty.search_current_node)
                    if _target is not None and _target.width and _target.height:
                        _cx = _target.abs_left + _target.width / 2.0
                        _cy = _target.abs_top + _target.height / 2.0
                        _hits = [h for h in Melty.bvh_query(_cx, _cy)
                                 if h._tile_id is not None and not h.just_shadow]
                        _top = _hits[0] if _hits else _target
                        Melty.search_click_pending = (
                            _top._tile_id,
                            InputEvent("left_mouse", "down", tile_id=_top._tile_id, x=_cx, y=_cy))
                        # Re-render the owner's subtree next frame (same as nav) so
                        # the top view actually re-runs and reads the injection.
                        Melty.cache.invalidate_up(search_ds._tile_id, force=True, max_depth=12)
                        request_render()
                elif not imgui.get_io().key_ctrl:
                    nav = -1 if imgui.get_io().key_shift else 1

        if nav != 0:
            # total is the combined count across all views; stepping wraps over
            # the whole result set. Flag a scroll and invalidate the owner's
            # subtree so every child view recomputes and the one holding the new
            # global-current match scrolls to it.
            search_ds.text_search_current = (search_ds.text_search_current + nav) % total
            search_ds._search_nav_pending = True
            Melty.cache.invalidate_up(search_ds._tile_id, force=True, max_depth=12)
            request_render()
    elif search_ds.search_text:
        imgui.align_text_to_frame_padding()
        imgui.text_colored("No results", 0.74, 0.5, 0.5, 1.0)
        # Enter with the find box focused force-recomputes the result set. "No
        # results" can be stale - the searched views may have been rebuilt since
        # the count was last done (e.g. a fresh load from disk) - so re-run the
        # sub-view walk on demand rather than leaving it stuck at zero. Flagging
        # _search_nav_pending makes the owner's pre-body walk re-count next frame
        # (same key source + box-focus gate as the nav block above).
        if _box_ds is not None and Melty.text_focused_ds is _box_ds:
            if any(k in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER)
                   for k, _ in Melty.frame_key_events):
                search_ds._search_nav_pending = True
                Melty.cache.invalidate_up(search_ds._tile_id, force=True, max_depth=12)
                request_render()
    else:
        imgui.align_text_to_frame_padding()
        imgui.text_colored("", 0.74, 0.5, 0.5, 1.0)



    if not input_value.search_active:
        draw_state.closed = True

    return False, input_value


@render_func(use_cache=True, show_header=True, selectable=False, with_header=draw_header)
def draw_single(input_value: any, view_func=None, mode: any = None, **kwargs):
    changed, return_val = view_func(input_value, mode=mode)
    return changed, return_val


@render_func(use_cache=False, show_header=True, max_height=30, selectable=False, with_header=draw_header)
def draw_blank(input_value: any, **kwargs):
    return False, None


def draw_any(input_value: any = None, view_func=None, mode: any = None, chain=None, **kwargs):
    # ── New chain system (opt-in) ─────────────────────────────
    # if chain is not None:
    #     from src.lsd.gl_gui.view.core_conversion.chain import run_chain
    #     return run_chain(chain, input_value, **kwargs)

    # print(f"{kwargs.get('key', None)}: draw_any called with type {type(input_value).__name__} and view_func {view_func.__name__ if view_func else None}")

    # # meta selection
    kwargs_view_func = view_func
    key = kwargs.get("key", None)
    real_type = kwargs.get("real_type", type(input_value))
    collection_type = kwargs.get("type_collection", type(kwargs.get("collection", None)))

    # if view_func is None:
    #     view_func = Core.melty.get_default_view_function(real_type=real_type, collection_type=collection_type, attrib_key=key)
    #
    # if view_func is None:
    #     view_func = draw_collection

    if view_func is None:
        new_default = Core.melty.get_default_view_function(real_type=real_type, collection_type=collection_type,
                                                      attrib_key=key)
        if new_default is None:
            new_default = draw_collection
        if view_func is None:
            view_func = new_default

    # Explicit chain= (e.g. a lens) runs the render_func chain executor directly,
    # bypassing type/mode routing. Mode-derived chains are still handled below.
    if chain is not None:
        return run_chain(input_value, chain=chain, **kwargs)

    if mode is None:
        mode = Core.melty.mode_stack[-1] if len(Core.melty.mode_stack) > 0 else None

    if isinstance(mode, tuple) and len(mode) > 0:
        main_mode = mode[0]
    else:
        main_mode = mode

    # --- Search: forward the active search term to searchable child views ---
    # The term rides Core.melty.search_stack so it reaches the whole subtree. We
    # simply hand it to each searchable view via its `search_text` param and
    # let the view decide what to do with it (the text editor highlights
    # matches in place). No value conversion or filtering happens here.
    # if (len(Core.melty.search_stack) > 0
    #         and (getattr(kwargs_view_func, '_searchable', False) or kwargs.get("searchable", False))
    #         and "search_text" not in kwargs):
    #     kwargs["search_text"] = Core.melty.search_stack[-1]

    if main_mode is not None:
        # Loop over super types
        mode_config = main_mode.get_config_for(input_value)
        if mode_config is not None and mode_config.func is None and (
                mode_config.kwargs.get("convert", None) is not None
                or mode_config.kwargs.get("convert_in", None) is not None):
            convert_in = mode_config.kwargs.get("convert_in", None)
            if convert_in is not None:
                # Infer target type from the return annotation of the last converter
                import inspect
                last_fn = convert_in[-1]
                ret = inspect.signature(last_fn).return_annotation
                convert_to_type = ret if ret is not inspect.Parameter.empty else None
            else:
                convert_to_type = mode_config.kwargs["convert"][-1]
            mode_config = main_mode.get_config_for(the_type=convert_to_type) if convert_to_type is not None else None
            if mode_config is not None and mode_config.func is not None:
                view_func = draw_single
                kwargs_view_func = mode_config.func

        elif mode_config is not None and mode_config.func is not None:
            if isinstance(mode_config.func, tuple):
                kwargs['chain'] = mode_config.func
                kwargs['route'] = mode_config.route
                view_func = run_chain
            else:
                view_func = mode_config.func
                kwargs_view_func = view_func

    # kwargs['use_cache'] = True
    kwargs['mode'] = mode
    kwargs['view_func'] = kwargs_view_func

    return_val = view_func(input_value, **kwargs)

    return return_val