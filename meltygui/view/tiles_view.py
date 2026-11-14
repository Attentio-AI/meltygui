"""Tiles: a Blender style tree of splits whose leaves are tiles, split and joined from their corners.

    changed, tiles = draw_tiles(tiles, name='tiles', tile=draw_scene)

The value is a tree of plain dicts, edited in place:

    split = {'id': 0, 'axis': 'x', 'cells': [node, ...], 'sizes': [pixels, ...]}
    tile  = {'id': 1}                   # and whatever the `tile` view draws

The root is a split. Every `id` is unique in the tree; `sizes` may be left out (equal shares) and
is written back on every run. A split never holds a split of its own axis (`tidy` keeps that).

Gestures, from the grips in three corners of a tile (the bottom left stays free):
- A left drag into the tile splits it. The longer direction picks the axis, the new tile is a copy
  on the corner's side, and the new divider follows the pointer until the release.
- A left drag out of the tile, over a neighbouring tile of the same split, joins: the tile under
  the pointer is the one that goes. Back over the tile the drag began in, that one goes instead.
  Anywhere else nothing is joined.

- `draw_tiles`: one columns / rows layout per split, so dividers collide like any others.
- `pane`, `grip`: a tile with its corners; a grip writes the drag into the shared `hand`.
- `steer`: the gesture, from `hand`. `split`, `join`, `tidy`: plain edits of the tree.
"""
import meltygui_imgui as imgui

from meltygui.core.input import mouse_cursor
from meltygui.core.rendering.gui_layout_prototype import columns, rows
from meltygui.core.rendering.gui_prototype import gui
from meltygui.hdr_color import pack_color

# Levers: sizes in logical pixels, colours.
GAP = 2.0                                   # around each tile, so twice this between two
MINS = (60.0, 40.0)                         # the smallest tile: width, height
GRIP = 14.0                                 # side of a corner's grab square
MARK = 9.0                                  # leg of the triangle drawn in it
INSET = 1.0                                 # from the tile's corner to the triangle
REACH = 8.0                                 # how far a drag goes before it splits or joins
TINT = (0.16, 0.23, 0.29, 1.0)              # a blank tile
IDLE = pack_color(1.0, 1.0, 1.0, 0.16)      # a grip
HOT = pack_color(1.0, 1.0, 1.0, 0.55)       # a grip under the pointer, or held
GOING = pack_color(1.0, 0.55, 0.15, 0.42)   # over the tile a release would remove
TEXT = pack_color(1.0, 1.0, 1.0)
NOTE = 'Release to join'
CORNERS = (('nw', True, True), ('ne', False, True), ('se', False, False))   # name, on the left, on the top
AXES = {'x': 0, 'y': 1}
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
def local(hand):
    """The pointer in the cell of the tile the drag began in: from the cell's corner, gap included."""
    return hand['x'] - hand['ox'] + GAP, hand['y'] - hand['oy'] + GAP


def under(parent, index, hand, spans):
    """Index of the cell of `parent` the pointer is over: `index` itself, a neighbouring tile
    across the whole shared edge, or None."""
    cells = parent['cells']
    axis = AXES[parent['axis']]
    span = spans[cells[index]['id']]
    point = local(hand)
    if not 0.0 <= point[1 - axis] <= span[1 - axis]:
        return None
    along = point[axis]
    if 0.0 <= along <= span[axis]:
        return index
    other = index - 1
    depth = -along
    if along > 0.0:
        other = index + 1
        depth = along - span[axis]
    if not 0 <= other < len(cells):
        return None
    cell = cells[other]
    if 'cells' in cell or depth > spans[cell['id']][axis]:
        return None
    return other


def begin(tree, parent, index, hand, spans):
    """Decide what a drag from a corner is, once it went `REACH`: into the tile splits it, out of
    it over a neighbour starts a join. Returns whether the tree changed."""
    travel = (hand['x'] - hand['sx'], hand['y'] - hand['sy'])
    if max(abs(travel[0]), abs(travel[1])) < REACH:
        return False
    axis = 1
    if abs(travel[0]) >= abs(travel[1]):
        axis = 0
    before = (hand['on_left'], hand['on_top'])[axis]
    inward = (travel[axis] > 0.0) == before
    if not inward:
        other = under(parent, index, hand, spans)
        if other is not None and other != index:
            gone = parent['cells'][other]['id']
            hand.update(mode='join', other=gone, gone=gone)
        return False
    span = spans[hand['tile']][axis]
    low = MINS[axis]
    high = span - MINS[axis]
    if high < low:
        return False                                # no room for two
    along = local(hand)[axis]
    at = min(max(along, low), high)
    holder, edge = split(tree, parent, index, axis, at, span, before)
    # `lag`: how far the pointer is from a divider the minimum held back. The divider waits for it.
    hand.update(mode='split', axis=axis, holder=holder['id'], edge=edge, before=before, lag=along - at)
    return True


def follow(hand, layouts, spans):
    """Move the divider of a split to the pointer, through the layout's own solver. The divider is
    the near edge of the tile the drag began in when the new tile is before it, else its far edge."""
    layout = layouts.get(hand['holder'])
    # Only a pointer sample of this run is in step with the tiles: moving the divider reruns the
    # view, and the same sample again would move it once more.
    if layout is None or not hand.pop('fresh', False):
        return
    axis = hand['axis']
    ahead = local(hand)[axis]
    if not hand['before']:
        ahead -= spans[hand['tile']][axis]
    if ahead * hand['lag'] > 0.0:
        return                                      # the pointer has not reached the divider yet
    hand['lag'] = 0.0
    if ahead != 0.0:
        hand['token'] = hand.get('token', TOKEN) + 1    # a drag of its own: `ahead` is from where the divider is now
        layout.manager.drag(layout, hand['edge'], ahead, token=hand['token'])


def aim(parent, index, hand, spans):
    """Choose the tile a release would remove: the neighbour under the pointer, or the tile the
    drag began in once the pointer is back over it. None anywhere else."""
    cells = parent['cells']
    at = under(parent, index, hand, spans)
    if at is None:
        hand['gone'] = None
    elif at != index:
        hand['other'] = cells[at]['id']
        hand['gone'] = hand['other']
    else:
        hand['gone'] = hand['tile']


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


def steer(tree, hand, layouts, spans, cache):
    """One step of the corner gesture in `hand`. Returns whether the tree changed."""
    found = find(tree, hand['tile'])
    if found is None or hand['tile'] not in spans:
        hand.clear()
        return False
    parent, index = found
    mode = hand.get('mode')
    released = hand.get('released', False)
    shown = hand.get('gone')
    edited = False
    if mode == 'split':
        follow(hand, layouts, spans)
    elif mode == 'join':
        aim(parent, index, hand, spans)
        if released and hand['gone'] is not None:
            edited = merge(tree, parent, index, hand)
    elif not released:
        edited = begin(tree, parent, index, hand, spans)
    if released:
        if mode == 'split' and hand['holder'] in layouts:
            layouts[hand['holder']].manager.end_gesture()
        hand.clear()
    if edited or hand.get('gone') != shown:
        cache.wake_next(cache.current)              # the next frame draws the new tree or preview
    return edited


# --- the views --------------------------------------------------------------------
@gui(listen=(), tint=TINT)
def blank(input_value: dict):
    """What a tile shows when `draw_tiles` is given no `tile` view."""


@gui(cursor=mouse_cursor.RESIZE_ALL, width=GRIP, height=GRIP)
def grip(input_value: dict, tile=0, on_left=True, on_top=True, left=0.0, top=0.0, width=0.0, height=0.0, event=None,
         cache=None):
    """One corner of a tile. Writes its drag into `input_value`, the hand of `draw_tiles`, in the
    window's coordinates (the tile moves under a drag): the press `sx`, `sy`, the pointer `x`, `y`
    and the tile's corner `ox`, `oy`. `fresh` marks a new sample."""
    pressed = event.left_mouse_down()
    dragged = event.left_mouse_drag()
    released = event.left_mouse_up() and input_value.get('tile') == tile
    hot = event.is_hovered() or event.is_left_mouse_held()
    if pressed or dragged:
        corner = cache.position(cache.current)
        x = corner[0] + event.x
        y = corner[1] + event.y
        if pressed or input_value.get('tile') != tile:      # a drag begins
            input_value.clear()
            input_value.update(tile=tile, on_left=on_left, on_top=on_top, sx=x, sy=y)
        input_value.update(x=x, y=y, ox=corner[0] - left, oy=corner[1] - top, fresh=True)
    if released:
        input_value['released'] = True
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
    return pressed or dragged or released, input_value


@gui(listen=())
def pane(input_value: dict, tile=None, hand=None, going=False, width=0.0, height=0.0):
    """A tile: the caller's view, a grip in three corners, and the join preview over them."""
    changed, _ = tile(input_value, name='tile', left=0.0, top=0.0, width=width, height=height)
    for name, on_left, on_top in CORNERS:
        left = width - GRIP
        if on_left:
            left = 0.0
        top = height - GRIP
        if on_top:
            top = 0.0
        moved, _ = grip(hand, name=name, tile=input_value['id'], on_left=on_left, on_top=on_top, left=left, top=top)
        if moved:
            changed = True
    if going:
        draw_list = imgui.get_window_draw_list()
        draw_list.add_rect_filled(0.0, 0.0, width, height, GOING)
        draw_list.push_clip_rect(0.0, 0.0, width, height, True)
        draw_list.add_text(8.0, 28.0, TEXT, NOTE)
        draw_list.pop_clip_rect()
    return changed, input_value


def place(node, tile, hand, layouts, spans):
    """Declare the layout of one split and the tiles in it. Fills `layouts` (split id: layout)
    and `spans` (tile id: its cell's width and height), and writes the split's `sizes` back."""
    axis = AXES[node['axis']]
    cells = node['cells']
    sizes = node.get('sizes')
    if sizes is not None and len(sizes) != len(cells):
        sizes = None
    make = rows
    if axis == 0:
        make = columns
    keys = tuple(cell['id'] for cell in cells)
    measured = []
    changed = False
    with make(keys, key=node['id'], sizes=sizes, mins=MINS[axis], padding=GAP) as layout:
        layouts[node['id']] = layout
        for cell in cells:
            with layout.cell(cell['id']) as inner:
                span = (inner[0] + 2.0 * GAP, inner[1] + 2.0 * GAP)
                measured.append(span[axis])
                if 'cells' in cell:
                    edited = place(cell, tile, hand, layouts, spans)
                else:
                    spans[cell['id']] = span
                    edited, _ = pane(cell, key=cell['id'], tile=tile, hand=hand, going=hand.get('gone') == cell['id'])
                if edited:
                    changed = True
    node['sizes'] = measured
    return changed


@gui(listen=(), no_save=('hand',))
def draw_tiles(input_value: dict, tile=None, hand=None, draw_state=None, cache=None):
    """The tree of tiles over the whole view. `tile` is the `@gui` view each tile is drawn with,
    given the tile's dict; without one the tiles are blank. Returns (True, tree) when a tile was
    split or joined, or a tile's view changed its dict."""
    if tile is None:
        tile = blank
    if hand is None:                # the corner drag in progress, shared with every grip
        hand = {}
        draw_state.hand = hand
    layouts = {}
    spans = {}
    changed = place(input_value, tile, hand, layouts, spans)
    if hand.get('tile') is not None and steer(input_value, hand, layouts, spans, cache):
        changed = True
    return changed, input_value
