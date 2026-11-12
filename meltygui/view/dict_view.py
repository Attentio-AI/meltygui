"""`draw_collection`: a dict as rows of key and value, each value drawn by its default view.

    changed, settings = draw_collection(settings, name='settings', width=420, height=600)

It draws every value that is read by keys: a dict and whatever looks like one, a list, a tuple, a
dataclass, a `DictConversion`. How a type is read and written is its `Keyed`
(core/rendering/keyed.py: `keys`, `get`, `put`, `count`); this view asks for nothing else, so
another type is drawn here by a `keyed_as` registration, not by a change to this file. "Dict"
below stands for any such value.

A dict among the values is a branch: its row has a chevron, a press on its key (`branch`) opens or
closes it and its entries follow, indented. Every other value is a leaf: the view `default_for`
picks (core/rendering/gui_any.py: by the key's name, the value's shape, its type) in the right
hand column. An edit is written into the dict it came from (`Keyed.put`), so the dicts keep their
identity, and the view returns `(True, input_value)`. The values of a dict that cannot be written
(a tuple, a mapping proxy, a frozen dataclass) are read only; a dict inside it is still edited.
`draw_any(a_dict)` comes here.

The pointer: this view listens for none of it, so a left drag over a key, between the rows or below
them moves the window the dict is in, and a right drag resizes it, as over any content
(docs/WINDOW_COLLISION_COLUMNS.md, "Gestures"). Only the controls take the pointer, each over what
it draws: a branch's chevron and key, and the value views that use it. A view that is added here
must do the same: listen for `pointer` only for a gesture of its own, and say with
`cache.imgui_input.reach` when that is less than its box.

Cost: what is in view, never the size of the dict.
- The open dicts are a tree of `Node`s holding each dict's keys (`list(dict)`, at C speed; the
  `range` of a list, which costs nothing). The
  rows at a scroll position are found by a bisection per level (`Node.entry`); `Rows.window` lists them.
- The body runs for a scroll, a branch that was opened or closed and an edit of one of its values.
  It paints the keys of the rows in view and declares their views; a row that was not touched
  costs one unchanged declaration.
- A dict that changed outside is followed where it is looked at: a changed length, a key in view
  that is gone, another dict under an open branch. A dict whose keys were reordered in place with
  the same length shows the old order until it is passed anew.

Levers: the sizes below, `expand=` (levels open by default; opening one costs a pass over its values).
Not here yet: rows of different heights, sets, add / delete / reorder, search, undo of an edit.
"""
from bisect import bisect_right

import meltygui_imgui as imgui

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.input import mouse_cursor
from meltygui.core.rendering.core_decoration import no_save_exclude
from meltygui.core.rendering.gui_any import default_for, is_default_for
from meltygui.core.rendering.gui_prototype import gui
from meltygui.core.rendering.keyed import Keyed, keyed
from meltygui.view.value_view import PAD, ROW as FIELD, SLATE, look

ROW = 26.                        # height of a row
GAP = (ROW - FIELD) * .5         # space above and below a row's value
INDENT = 16.                     # pixels a level is indented by
ARROW = 14.                      # width kept for a branch's chevron
SPLIT = .42                      # the label column's share of the width
NARROW = 90.                     # the label column's least width
BAR = 8.                         # width of the scroll thumb's lane
LABEL = 96                       # characters of a key that are drawn
SEPARATOR = '\x1f'               # between the keys of a row's identity
LOCKED = {'editable': False}      # what the values of a dict that cannot be written are drawn with
MISSING = object()


def nests(value, key):
    """Whether `value` under `key` is a branch, drawn as rows of this view: its default view is this one."""
    return default_for(value, key) is draw_collection and keyed(value.__class__) is not None


class Node:
    """One open dict: its keys in order, the open dicts among its values (`open`: entry index to Node)
    and the rows it shows (`rows`, its own row excluded). `order` and `starts` are the open entries
    and the row each one stands at, for `entry`. `path` is its keys from the top, `name` the same as
    the prefix of its rows' view identities. `keyed` is how the dict is read and written."""
    __slots__ = ('value', 'keyed', 'keys', 'open', 'order', 'starts', 'rows', 'path', 'name', 'depth')

    def __init__(self, value, path, name):
        self.keyed = keyed(value.__class__)
        if self.keyed is None:
            raise TypeError(f'draw_collection cannot read a {value.__class__.__name__} by keys: see keyed_as (core/rendering/keyed.py)')
        self.value, self.keys, self.open = value, self.keyed.keys(value), {}
        self.path, self.name, self.depth = path, name, len(path)

    def item(self, key):
        """The value under `key`, or MISSING when the key is gone."""
        try:
            return self.keyed.get(self.value, key)
        except (LookupError, AttributeError):
            return MISSING

    def measure(self):
        """Count the rows again, after `open` or the rows of an open entry changed."""
        self.order, self.starts, below = sorted(self.open), [], 0
        for index in self.order:
            self.starts.append(index + below)
            below += self.open[index].rows
        self.rows = len(self.keys) + below

    def entry(self, row):
        """(entry index, rows of that entry's open dict that come before `row`) for the row `row`
        of this node; -1 rows: `row` is the entry's own."""
        at = bisect_right(self.starts, row) - 1
        if at < 0:
            return row, -1
        index, inside = self.order[at], row - self.starts[at] - 1
        rows = self.open[index].rows
        return (index, inside) if inside < rows else (index + 1 + inside - rows, -1)


class Rows:
    """The rows of one collection: the tree of its open dicts. Made for a dict, a number of levels
    open by default and the paths whose state is flipped; `window` lists rows, `flip` opens and closes."""

    def __init__(self, value, expand, flipped):
        self.value, self.expand, self.flipped = value, expand, set(flipped)
        self.top = self.grow(value, (), '')

    count = property(lambda self: self.top.rows)

    def grow(self, value, path, name):
        """The node of the open dict `value` at `path`, with the nodes of the dicts open below it."""
        node = Node(value, path, name)
        if len(path) + 1 < self.expand:
            # Its dicts are open by default: every one that was not flipped shut.
            for index, key in enumerate(node.keys):
                item = node.item(key)
                if item is not MISSING and nests(item, key) and path + (key,) not in self.flipped:
                    node.open[index] = self.grow(item, path + (key,), f'{name}{SEPARATOR}{key!r}')
        else:
            for found in self.flipped:
                if len(found) == len(path) + 1 and found[:-1] == path:
                    key = found[-1]
                    item = node.item(key)
                    if item is not MISSING and nests(item, key):
                        node.open[node.keys.index(key)] = self.grow(item, found, f'{name}{SEPARATOR}{key!r}')
        node.measure()
        return node

    def check(self, node):
        """Follow the dicts where they changed under the tree: returns the node to use for `node`."""
        value = node.value
        if node.keyed.count(value) != len(node.keys):
            return self.grow(value, node.path, node.name)
        for index, child in list(node.open.items()):
            key = node.keys[index]
            item = node.item(key)
            if item is MISSING:
                return self.grow(value, node.path, node.name)
            if item is child.value:
                node.open[index] = self.check(child)
            elif nests(item, key):
                node.open[index] = self.grow(item, child.path, child.name)
            else:
                del node.open[index]
        node.measure()
        return node

    def window(self, first, count):
        """[(row, node, entry index)] of up to `count` rows from row `first`."""
        found = []
        self.walk(self.top, first, first + count, first, found)
        return found

    def walk(self, node, first, stop, row, found):
        index, inside = node.entry(first)
        keys, open = node.keys, node.open
        while row < stop and index < len(keys):
            if inside < 0:
                found.append((row, node, index))
                row += 1
            child = open.get(index) if open else None
            if child is not None and row < stop:
                row = self.walk(child, max(inside, 0), stop, row, found)
            index, inside = index + 1, -1
        return row

    def flip(self, row):
        """Open or close the branch at `row`. Returns the paths now flipped, or None when the row is no branch."""
        found = self.window(row, 1)
        if not found:
            return None
        _, node, index = found[0]
        key = node.keys[index]
        item = node.item(key)
        if item is MISSING or not nests(item, key):
            return None
        path = node.path + (key,)
        self.flipped ^= {path}
        if index in node.open:
            del node.open[index]
        else:
            node.open[index] = self.grow(item, path, f'{node.name}{SEPARATOR}{key!r}')
        self.settle(self.top)
        return self.flipped

    def settle(self, node):
        for child in node.open.values():
            self.settle(child)
        node.measure()


@no_save_exclude('rows')
class Tree(DictConversion):
    """Which branches of one collection are open: `flipped` holds the paths (tuples of keys) whose
    state is not the default one. `rows` is the running `Rows`, made from the dict."""

    def __init__(self):
        super().__init__()
        self.flipped = []
        self.rows = None


@gui(listen=())
def end(input_value):
    """Nothing: its position is the last row, which gives the collection its scroll range."""


@gui(listen=('pointer',), cursor=mouse_cursor.HAND)
def branch(input_value: bool, label='', height=0., view_events=None):
    """The chevron and key of a dict's row. `input_value` is whether the dict is open; a press flips
    it. It is as wide as what it draws: beside it the row is the window's to be moved by."""
    pressed = any(event.kind == 'press' and not event.button for event in view_events.get('pointer', ()))
    opened = input_value != pressed
    table, draw_list = look(SLATE), imgui.get_window_draw_list()
    middle = height * .5
    if opened:
        draw_list.add_triangle_filled(0., middle - 3, 9., middle - 3, 4.5, middle + 4, table.dim)
    else:
        draw_list.add_triangle_filled(1., middle - 5, 8., middle, 1., middle + 5, table.dim)
    draw_list.add_text(ARROW, (height - imgui.get_text_line_height()) * .5, table.text, label)
    return pressed, opened


@is_default_for(Keyed)
@gui(scroll=True, listen=('scroll',), width=420, height=480)
def draw_collection(input_value: object, tree: Tree = None, expand=1, editable=True, width=0., height=0.,
                    scroll_offset=(0., 0.), view_events=None):
    """Draw and edit a dict, or any value that is read by keys (core/rendering/keyed.py). `expand` is
    how many levels are open before the user opens or closes any; `editable=False` shows the values
    read only."""
    rows = tree.rows
    if rows is None or rows.value is not input_value or rows.expand != expand:
        rows = tree.rows = Rows(input_value, expand, map(tuple, tree.flipped))
    elif not view_events:
        # No input brought this run: the dict may have changed outside.
        rows.top = rows.check(rows.top)
    top = scroll_offset[1]
    table, draw_list = look(SLATE), imgui.get_window_draw_list()
    split = max(width * SPLIT, NARROW)
    wide = max(width - split - BAR - PAD, 1.)
    text_y = (ROW - imgui.get_text_line_height()) * .5
    changed = False
    first, count = int(top // ROW), int(height // ROW) + 2
    shown = rows.window(first, count)
    items = [node.item(node.keys[index]) for _, node, index in shown]
    if any(item is MISSING for item in items):
        # A key in view is gone: the dict changed outside.
        rows.top = rows.grow(rows.value, (), '')
        shown = rows.window(first, count)
        items = [node.item(node.keys[index]) for _, node, index in shown]
    flips = []
    for (row, node, index), value in zip(shown, items):
        if value is MISSING:
            continue
        key = node.keys[index]
        view = default_for(value, key)
        inner = keyed(value.__class__) if view is draw_collection else None
        label, name = str(key)[:LABEL], f'{node.name}{SEPARATOR}{key!r}'
        y, x = row * ROW - top, PAD + node.depth * INDENT
        if inner is not None:
            # A branch: its chevron and key are a view that a press flips, no wider than they are.
            wide_key = min(ARROW + imgui.calc_text_size(label).x + PAD, max(split - PAD - x, 1.))
            flipped, _ = branch(index in node.open, key=name, label=label, left=x, top=row * ROW, width=wide_key, height=ROW)
            if flipped:
                flips.append(row)
            held = inner.count(value)
            draw_list.add_text(split + PAD, y + text_y, table.dim, f'{held} item{"s" * (held != 1)}')
            continue
        # A leaf's key, painted here in view coordinates: the body runs for every scroll.
        draw_list.push_clip_rect(0., y, split - PAD, y + ROW, True)
        draw_list.add_text(x + ARROW, y + text_y, table.text, label)
        draw_list.pop_clip_rect()
        put = node.keyed.put
        edited, value = view(value, key=name, left=split, top=row * ROW + GAP, width=wide, height=FIELD,
                             **({} if editable and put else LOCKED))
        if edited:
            put(node.value, key, value)
            changed = True
    for row in reversed(flips):
        # (The rows below it are where they were in this run; the view runs again for the branch's answer.)
        rows.flip(row)
        tree.flipped = sorted(rows.flipped, key=repr)
    end(None, name='end', left=0., top=max(rows.count - 1, 0) * ROW, width=1., height=ROW)
    reach = rows.count * ROW - height
    if reach > 0:
        # The scroll thumb: where the rows in view are among all of them.
        size = max(height * height / (reach + height), 24.)
        at = (height - size) * min(top / reach, 1.)
        draw_list.add_rect_filled(width - BAR + 2, at, width - 2, at + size, table.outline, 3.)
    return changed, input_value
