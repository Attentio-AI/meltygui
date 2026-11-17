"""Tiles: a Blender style tree of splits whose leaves are tiles, split and joined from their corners.

    VIEWS = (draw_scene, draw_notes)        # a module constant: `@gui` views of a tile's dict
    changed, tiles = draw_tiles(tiles, name='tiles', views=VIEWS, width=width, height=height)

Each tile shows one of `views`, picked from the dropdown in its head, and keeps the pick in its
dict as the view's name. With one view there is no head; with none the tiles are blank.

Place it like any view. An edge within `REACH_EDGE` of the window's edge is tied to it: a divider
pushed against it resizes the window, and the window takes the tiles along. No cell around it.

The value is any dict, edited in place. {} is one tile; a dict with 'cells' is a split of them:

    {}                                          # one tile
    {'cells': [{}, {'cells': [{}, {}]}]}        # a tile beside two stacked ones

`mend` fills in the rest on every run, so nothing below has to be written by the caller:

    split = {'id': 0, 'axis': 'x', 'cells': [node, ...], 'sizes': [pixels, ...]}
    tile  = {'id': 1, 'view': 'draw_scene'}     # and whatever its view keeps there

The root is a split (a root without 'cells' is wrapped in one). `id` is the tile's identity across
splits and joins, unique in the tree: a missing or repeated one is replaced. `axis` is 'x' (side by
side) at the root and the other one than the parent's below it. `sizes` are equal shares until a
divider moves, and written back on every run. A split never holds a split of its own axis once it
was edited (`tidy` keeps that). A view is handed its tile's dict, `id` included.

Gestures, from the grips in three corners of a tile (the bottom left stays free):
- A left drag into the tile splits it. The longer direction picks the axis, the new tile is a copy
  on the corner's side, and the new divider follows the pointer until the release.
- A left drag out of the tile, over a neighbouring tile of the same split, joins: the tile under
  the pointer is the one that goes. Back over the tile the drag began in, that one goes instead.
  Anywhere else nothing is joined.

- `draw_tiles`: one columns / rows layout per split, so dividers collide like any others. A
  divider drag runs the resized tiles only; `draw_tiles` runs once when it ends, for the sizes.
- `pane`, `grip`: a tile with its head and corners.
- `chosen`, `title`: which view a tile shows, and what the dropdown calls it.
- `Hand`: the corner drag in progress. The dragged grip is the only view that runs for a sample:
  `track` moves a split's divider (`follow`) or aims a join (`aim`) from the layout itself, and
  wakes `draw_tiles` only for what needs the tree (`steer`: the cut `begin`, the join `merge`, the
  preview, the release). `split`, `join`, `tidy`: plain edits of the tree.
"""
import meltygui_imgui as imgui

from meltygui.core.input import mouse_cursor
from meltygui.core.rendering.gui_layout_prototype import anchor, columns, rows
from meltygui.core.rendering.gui_prototype import gui
from meltygui.core.rendering.gui_readers import Readers
from meltygui.hdr_color import pack_color
from meltygui.view.choice_view import draw_dropdown
from meltygui.view.value_view import ROW

# Levers: sizes in logical pixels, colours.
GAP = 2.0                                   # around each tile, so twice this between two
MINS = (60.0, 40.0)                         # the smallest tile: width, height
GRIP = 14.0                                 # side of a corner's grab square
MARK = 9.0                                  # leg of the triangle drawn in it
INSET = 1.0                                 # from the tile's corner to the triangle
REACH = 8.0                                 # how far a drag goes before it splits or joins
HEAD = 30.0                                 # height of the strip a tile's dropdown sits in
PICK = 160.0                                # the widest that dropdown gets
TINT = (0.16, 0.23, 0.29, 1.0)              # a blank tile
BAR = pack_color(0.11, 0.16, 0.20)          # the strip behind the dropdown
IDLE = pack_color(1.0, 1.0, 1.0, 0.16)      # a grip
HOT = pack_color(1.0, 1.0, 1.0, 0.55)       # a grip under the pointer, or held
GOING = pack_color(1.0, 0.55, 0.15, 0.42)   # over the tile a release would remove
TEXT = pack_color(1.0, 1.0, 1.0)
NOTE = 'Release to join'
CORNERS = (('nw', True, True), ('ne', False, True), ('se', False, False))   # name, on the left, on the top
AXES = {'x': 0, 'y': 1}
REACH_EDGE = 100.0                          # an edge this near the window's is tied to it
TOKEN = 7000                                # the first name a split's divider drags have for the solver


# --- the tree: plain edits ----------------------------------------------------
def find(node, tile):
    """(split, index) of the tile with that id, or None."""
    for index, cell in enumerate(node['cells']):
        if 'cells' not in cell:
            if cell['id'] == tile:
                return node, index
            continue
        found = find(cell, tile)
        if found is not None:
            return found
    return None


def top(node):
    """The highest id in the tree."""
    highest = node['id']
    for cell in node.get('cells', ()):
        highest = max(highest, top(cell))
    return highest


def mend(tree):
    """Make any dict a tree `place` can lay out, in place. A root that is no split ({} too) becomes
    one around a tile that takes what it held. Then, for every node: an id that is missing, no int
    or in the tree already becomes a new one above the highest (the ids are the layout's keys, and
    a tile's view can edit the one in its dict: `draw_any` drags it); a split with no axis gets the
    one its parent does not have, 'x' at the root; a split with no cells gets a blank tile.
    Returns whether anything changed."""
    changed = False
    if 'cells' not in tree:
        tile = dict(tree)
        tree.clear()
        tree['cells'] = [tile]
        changed = True
    seen = set()
    fresh = []
    nodes = [(tree, 'x')]
    while nodes:
        node, axis = nodes.pop()
        if type(node.get('id')) is int and node['id'] not in seen:
            seen.add(node['id'])
        else:
            fresh.append(node)
        if 'cells' not in node:
            continue
        cells = node['cells']
        if not isinstance(cells, list):
            cells = node['cells'] = list(cells) if isinstance(cells, tuple) else []
            changed = True
        if not cells:
            cells.append({})
            changed = True
        if node.get('axis') not in AXES:
            node['axis'] = axis
            changed = True
        other = 'y' if node['axis'] == 'x' else 'x'
        for at, cell in enumerate(cells):
            if not isinstance(cell, dict):
                cell = cells[at] = {}
                changed = True
            nodes.append((cell, other))
    spare = max(seen, default=-1)
    for node in fresh:
        spare += 1
        node['id'] = spare
    return changed or bool(fresh)


def split(tree, parent, index, axis, at, span, before):
    """Cut the tile `parent['cells'][index]`, `span` long on `axis`, at `at` from its near edge.
    The new tile is a copy with an id of its own, before the old one or after it. Returns the
    split that holds the new divider and that divider's index among its edges."""
    old = parent['cells'][index]
    spare = top(tree) + 1
    new = dict(old, id=spare)
    pair = [old, new]
    if before:
        pair = [new, old]
    shares = [at, span - at]
    if AXES[parent['axis']] == axis:
        parent['cells'][index:index + 1] = pair
        parent['sizes'][index:index + 1] = shares
        return parent, index + 1
    inner = {'id': spare + 1, 'axis': 'xy'[axis], 'cells': pair, 'sizes': shares}
    parent['cells'][index] = inner
    return inner, 1


def join(tree, parent, gone, kept):
    """Remove the tile at `gone`; its neighbour at `kept` takes the room."""
    parent['sizes'][kept] += parent['sizes'][gone]
    del parent['cells'][gone]
    del parent['sizes'][gone]
    tidy(tree)


def tidy(node):
    """Restore the shape after an edit: a split left with one cell becomes that cell, and a split
    inside a split of the same axis becomes its cells."""
    cells = node['cells']
    sizes = node['sizes']
    at = 0
    while at < len(cells):
        cell = cells[at]
        if 'cells' not in cell:
            at += 1
            continue
        tidy(cell)
        if len(cell['cells']) == 1:
            cells[at] = cell['cells'][0]            # looked at again: it may be a split of this axis
        elif cell['axis'] == node['axis']:
            scale = sizes[at] / max(sum(cell['sizes']), 1.0)
            cells[at:at + 1] = cell['cells']
            sizes[at:at + 1] = [size * scale for size in cell['sizes']]
        else:
            at += 1
    if len(cells) == 1 and 'cells' in cells[0]:     # the root keeps its dict and id
        only = cells[0]
        node['axis'] = only['axis']
        node['cells'] = only['cells']
        node['sizes'] = only['sizes']


# --- the gesture ----------------------------------------------------------------
class Hand(dict):
    """The corner drag in progress, shared by `draw_tiles` and the grips of every tile. The grip
    that is dragged writes its samples here and does what a sample alone can do (`track`); it
    wakes `draw_tiles`, the reader, only when the tree or the join preview has to change.

    The press `sx`, `sy`, the pointer `x`, `y` and the tile's corner `ox`, `oy` are in the window's
    coordinates (the tile moves under a drag). `mode` is 'split' or 'join' once `begin` decided;
    `layout` and `cell` are then the layout the gesture works in and the cell of the tile the drag
    began in, `leaves` the tile id of each of that layout's cells (None for a split)."""

    def __init__(self):
        super().__init__()
        self.readers = Readers()


def local(hand):
    """The pointer in the cell of the tile the drag began in: from the cell's corner, gap included."""
    return hand['x'] - hand['ox'] + GAP, hand['y'] - hand['oy'] + GAP


def cross(layout):
    """The extent of a layout's cells across its axis."""
    position = layout.manager.axes[1 - layout.axis].position
    return position(layout.cross[1]) - position(layout.cross[0])


def under(hand, axis, sizes, across):
    """Index of the cell the pointer is over, among cells `sizes` long on `axis` and `across` wide:
    the cell the drag began in, a neighbouring tile across the whole shared edge, or None."""
    index = hand['cell']
    point = local(hand)
    if not 0.0 <= point[1 - axis] <= across:
        return None
    along = point[axis]
    if 0.0 <= along <= sizes[index]:
        return index
    other = index - 1
    depth = -along
    if along > 0.0:
        other = index + 1
        depth = along - sizes[index]
    if not 0 <= other < len(sizes):
        return None
    if hand['leaves'][other] is None or depth > sizes[other]:
        return None
    return other


def begin(tree, parent, index, hand, layouts):
    """Decide what a drag from a corner is, once it went `REACH`: into the tile splits it, out of
    it over a neighbour starts a join. Returns whether the tree changed."""
    travel = (hand['x'] - hand['sx'], hand['y'] - hand['sy'])
    if max(abs(travel[0]), abs(travel[1])) < REACH:
        return False
    axis = 1
    if abs(travel[0]) >= abs(travel[1]):
        axis = 0
    layout = layouts[parent['id']]
    sizes = layout.sizes()
    across = cross(layout)
    before = (hand['on_left'], hand['on_top'])[axis]
    inward = (travel[axis] > 0.0) == before
    if not inward:
        leaves = [None if 'cells' in cell else cell['id'] for cell in parent['cells']]
        hand.update(cell=index, leaves=leaves)
        other = under(hand, layout.axis, sizes, across)
        if other is not None and other != index:
            hand.update(mode='join', layout=layout, other=leaves[other], gone=leaves[other])
        return False
    span = across
    if layout.axis == axis:
        span = sizes[index]
    low = MINS[axis]
    high = span - MINS[axis]
    if high < low:
        return False                                # no room for two
    along = local(hand)[axis]
    at = min(max(along, low), high)
    holder, edge = split(tree, parent, index, axis, at, span, before)
    cell = edge - 1                                 # of the tile the drag began in, in the holder
    if before:
        cell = edge
    # `lag`: how far the pointer is from a divider the minimum held back. The divider waits for it.
    # No `layout` yet: the holder's is declared by the next run of `draw_tiles`.
    hand.update(mode='split', axis=axis, holder=holder['id'], edge=edge, cell=cell, before=before, lag=along - at)
    return True


def follow(hand):
    """Move the divider of a split to the pointer, through the layout's own solver. The divider is
    the near edge of the tile the drag began in when the new tile is before it, else its far edge."""
    layout = hand.get('layout')
    if layout is None:
        return                                      # the cut is not laid out yet
    axis = hand['axis']
    ahead = local(hand)[axis]
    if not hand['before']:
        ahead -= layout.sizes()[hand['cell']]
    if ahead * hand['lag'] > 0.0:
        return                                      # the pointer has not reached the divider yet
    hand['lag'] = 0.0
    if ahead != 0.0:
        hand['token'] = hand.get('token', TOKEN) + 1    # a drag of its own: `ahead` is from where the divider is now
        layout.manager.drag(layout, hand['edge'], ahead, token=hand['token'])


def aim(hand, axis, sizes, across):
    """Choose the tile a release would remove: the neighbour under the pointer, or the tile the
    drag began in once the pointer is back over it. None anywhere else."""
    at = under(hand, axis, sizes, across)
    if at is None:
        hand['gone'] = None
    elif at != hand['cell']:
        hand['other'] = hand['leaves'][at]
        hand['gone'] = hand['other']
    else:
        hand['gone'] = hand['tile']


def track(hand):
    """What the dragged grip does with a new sample: a split's divider follows the pointer, a join
    aims. Returns whether `draw_tiles` has to run: the drag went far enough to be decided, the
    tile a join would remove is another one, or the button was released."""
    if hand.get('released', False):
        return True
    mode = hand.get('mode')
    if mode == 'split':
        follow(hand)
        return False
    if mode == 'join':
        layout = hand['layout']
        shown = hand['gone']
        aim(hand, layout.axis, layout.sizes(), cross(layout))
        return hand['gone'] != shown
    travel = max(abs(hand['x'] - hand['sx']), abs(hand['y'] - hand['sy']))
    return travel >= REACH


def merge(tree, parent, index, hand):
    """Join on the release, if the two tiles are still neighbours. Returns whether the tree changed."""
    cells = parent['cells']
    for other in (index - 1, index + 1):
        if 0 <= other < len(cells) and cells[other]['id'] == hand['other'] and 'cells' not in cells[other]:
            if hand['gone'] == hand['tile']:
                join(tree, parent, index, other)
            else:
                join(tree, parent, other, index)
            return True
    return False


def steer(tree, hand, layouts, cache):
    """The part of the corner gesture that needs the tree: the cut, the join, the end. Runs when
    a grip woke `draw_tiles` (`track`). Returns whether the tree changed."""
    found = find(tree, hand['tile'])
    if found is None:
        hand.clear()
        return False
    parent, index = found
    mode = hand.get('mode')
    released = hand.get('released', False)
    shown = hand.get('gone')
    edited = False
    if mode == 'split':
        hand['layout'] = layouts.get(hand['holder'])    # the grip moves its divider from now on
    elif mode == 'join':
        if released and hand['gone'] is not None:
            edited = merge(tree, parent, index, hand)
    elif not released:
        edited = begin(tree, parent, index, hand, layouts)
    if released:
        if mode == 'split' and hand.get('layout') is not None:
            hand['layout'].manager.end_gesture()
        hand.clear()
    if edited or hand.get('gone') != shown:
        cache.wake_next(cache.current)              # the next frame draws the new tree or preview
    return edited


# --- the views --------------------------------------------------------------------
@gui(listen=(), tint=TINT)
def blank(input_value: dict):
    """What a tile shows when `draw_tiles` is given no views."""


def title(view):
    """What the dropdown calls a view: its function's name without `draw_`, as words."""
    return view.__name__.removeprefix('draw_').replace('_', ' ').capitalize()


def chosen(tile, views):
    """The view the tile names in 'view'. The first one when it names none of them: a new tile,
    or a view that was renamed."""
    for view in views:
        if view.__name__ == tile.get('view'):
            return view
    return views[0]


@gui(cursor=mouse_cursor.RESIZE_ALL, width=GRIP, height=GRIP)
def grip(tile=0, on_left=True, on_top=True, hand=None, left=0.0, top=0.0, width=0.0, height=0.0, event=None, cache=None):
    """One corner of a tile. Writes its drag into `hand`, moves the gesture on (`track`) and wakes
    `draw_tiles` when that asks for it. It has no value of its own and returns nothing: an edit
    reported by one grip would run every other grip, which all share the hand."""
    pressed = event.left_mouse_down()
    dragged = event.left_mouse_drag()
    released = event.left_mouse_up() and hand.get('tile') == tile
    hot = event.is_hovered() or event.is_left_mouse_held()
    if pressed or dragged:
        corner = cache.position(cache.current)
        x = corner[0] + event.x
        y = corner[1] + event.y
        if pressed or hand.get('tile') != tile:             # a drag begins
            hand.clear()
            hand.update(tile=tile, on_left=on_left, on_top=on_top, sx=x, sy=y)
        hand.update(x=x, y=y, ox=corner[0] - left, oy=corner[1] - top)
    if released:
        hand['released'] = True
    if (dragged or released) and hand.get('tile') == tile and track(hand):
        hand.readers.wake()
    x = width - INSET
    across = -MARK
    if on_left:
        x = INSET
        across = MARK
    y = height - INSET
    down = -MARK
    if on_top:
        y = INSET
        down = MARK
    colour = IDLE
    if hot:
        colour = HOT
    imgui.get_window_draw_list().add_triangle_filled(x, y, x + across, y, x, y + down, colour)


@gui(listen=())
def pane(input_value: dict, views=(blank,), hand=None, going=False, width=0.0, height=0.0):
    """A tile: the dropdown that picks its view when there are several, that view under it, a grip
    in three corners, and the join preview over them. A new size is not a change of the tile's
    dict: `draw_tiles` owns the layout, runs when a divider moves and writes the sizes itself.
    Reporting one would run every view of this dict, `draw_tiles` and its caller a second time."""
    view = chosen(input_value, views)
    head = 0.0
    picked = False
    if len(views) > 1:
        head = HEAD
        imgui.get_window_draw_list().add_rect_filled(0.0, 0.0, width, head, BAR)
        room = width - 2.0 * (GRIP + GAP)           # between the grips of the two top corners
        picked, view = draw_dropdown(view, name='view', choices=views, label=title, left=GRIP + GAP,
                                     top=(head - ROW) * 0.5, width=min(PICK, room))
    if picked:
        input_value['view'] = view.__name__
    changed, _ = view(input_value, name='tile', left=0.0, top=head, width=width, height=height - head)
    if picked:
        changed = True
    for name, on_left, on_top in CORNERS:
        left = width - GRIP
        if on_left:
            left = 0.0
        top = height - GRIP
        if on_top:
            top = 0.0
        grip(name=name, tile=input_value['id'], on_left=on_left, on_top=on_top, hand=hand, left=left, top=top)
    if going:
        draw_list = imgui.get_window_draw_list()
        draw_list.add_rect_filled(0.0, 0.0, width, height, GOING)
        draw_list.push_clip_rect(0.0, 0.0, width, height, True)
        draw_list.add_text(8.0, 28.0, TEXT, NOTE)
        draw_list.pop_clip_rect()
    return changed, input_value


def place(node, views, hand, layouts):
    """Declare the layout of one split and the tiles in it. Fills `layouts` (split id: layout)
    and writes the split's `sizes` back.
    Returns whether the tree changed: a tile's view edited its dict, or the sizes are new.
    Asking the layout for its sizes is what runs `draw_tiles` once when a divider drag ends
    (`Layout.sizes`); during the drag only the resized tiles run."""
    axis = AXES[node['axis']]
    cells = node['cells']
    sizes = node.get('sizes')
    if not isinstance(sizes, (list, tuple)) or len(sizes) != len(cells):
        sizes = None                                # equal shares
    make = rows
    if axis == 0:
        make = columns
    keys = tuple(cell['id'] for cell in cells)
    changed = False
    with make(keys, key=node['id'], sizes=sizes, mins=MINS[axis], padding=GAP) as layout:
        layouts[node['id']] = layout
        for cell in cells:
            with layout.cell(cell['id']):
                if 'cells' in cell:
                    edited = place(cell, views, hand, layouts)
                else:
                    edited, _ = pane(cell, key=cell['id'], views=views, hand=hand, going=hand.get('gone') == cell['id'])
                if edited:
                    changed = True
        measured = layout.sizes()
    if measured != node.get('sizes'):
        node['sizes'] = measured
        changed = True
    return changed


@gui(listen=(), no_save=('hand',))
def draw_tiles(input_value: dict, views=(blank,), hand=None, draw_state=None, cache=None):
    """The tree of tiles over the whole view. `views` are the `@gui` views a tile can show, each
    given the tile's dict: keep them in a module constant. Returns (True, tree) when a tile was
    split or joined, another view was picked, a tile's view changed its dict, the sizes of a
    split are new, or `mend` completed the tree."""
    if hand is None:                # the corner drag in progress, shared with every grip
        hand = Hand()
        draw_state.hand = hand
    hand.readers.note()             # a grip wakes this view when the gesture needs the tree
    anchor(REACH_EDGE)
    layouts = {}
    changed = mend(input_value)
    if place(input_value, views, hand, layouts):
        changed = True
    if hand.get('tile') is not None and steer(input_value, hand, layouts, cache):
        changed = True
    return changed, input_value
