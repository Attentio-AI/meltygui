"""Import graph — every project file as a labelled box, every import a line.

The whole `file_graph.ImportGraph` drawn straight to the window draw_list
(the file_tree / fast_dock model: no per-node widgets, event params for
gestures, blit cache while idle). The graph's layered layout
(file_graph.layout_graph: column = dependency depth, left → right, rows
ordered to keep lines short) gives the ORDER; this view sizes it in
pixels — each node is a rounded box around its file name, a column is as
wide as its widest box, rows share one pitch — so nothing overlaps, then
projects it: screen = origin + (box − centre) · fit · zoom + pan, where
zoom 1 fits the whole graph in the window.

Gestures: middle-drag pans, wheel zooms about the cursor, click a box to
select it (its importers light in the file's tint, its imports in the
washed variant, everything else fades), Esc clears, `/` resets the camera.
Box size follows USAGE (importer count, log scale) through the font scale,
so the hubs read at a glance. Shares the graph with render_file_tree
through file_graph.current(): either window's Build button serves both.
"""

from __future__ import annotations

import colorsys
import math
from pathlib import Path

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color
from meltygui.core.melty import Melty
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.core.core_render import render_func
from meltygui.core.rendering.window_decoration import window
from meltygui.core.layout.header_runtime import _brightness_clamp_fn
import meltygui.model.import_graph_model as file_graph
from meltygui.core.files.file_tree_core import _meta
from meltygui.core.files.file_tree_core import _tint_of
from meltygui.core.files.file_tree_core import open_file


from meltygui.view.graph_view import render_import_graph
render_import_graph = window(initial={'width': 720, 'height': 620}, tint=(0.42, 0.36, 0.54), disable_scroll=True)(render_import_graph)
