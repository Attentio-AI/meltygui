"""`draw_collection`: a dict as rows of key and value, each value drawn by its default view.

    changed, settings = draw_collection(settings, name='settings', width=420, height=600)

A dict among the values is a branch: its row has a chevron, a press on the row opens or closes it
and its entries follow, indented. Every other value is a leaf: the view `default_for` picks
(core/rendering/gui_any.py: by the key's name, the value's shape, its type) in the right hand
column. An edit is written into the dict it came from, so the dicts keep their identity, and the
view returns `(True, input_value)`. `draw_any(a_dict)` comes here.

Cost: what is in view, never the size of the dict.
- The open dicts are a tree of `Node`s holding each dict's keys (`list(dict)`, at C speed). The
  rows at a scroll position are found by a bisection per level (`Node.entry`); `Rows.window` lists them.
- The body runs for a scroll, a press on a branch and an edit of one of its values. It paints the
  labels of the rows in view and declares their value views; a row that was not touched costs
  one unchanged declaration.
- A dict that changed outside is followed where it is looked at: a changed length, a key in view
  that is gone, another dict under an open branch. A dict whose keys were reordered in place with
  the same length shows the old order until it is passed anew.

Levers: the sizes below, `expand=` (levels open by default; opening one costs a pass over its values).
Not here yet: rows of different heights, lists and tuples as branches, add / delete / reorder,
search, undo of an edit.
"""
from bisect import bisect_right

import meltygui_imgui as imgui

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save_exclude
from meltygui.core.rendering.gui_any import default_for, is_default_for
from meltygui.core.rendering.gui_prototype import gui
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
MISSING = object()


def branch(value, key):
    """Whether `value` under `key` is drawn as rows of this view: a dict whose default view is this one."""
    return isinstance(value, dict) and default_for(value, key) is draw_collection


class Node:
    """One open dict: its keys in order, the open dicts among its values (`open`: entry index to Node)
    and the rows it shows (`rows`, its own row excluded). `order` and `starts` are the open entries
    and the row each one stands at, for `entry`. `path` is its keys from the top, `name` the same as
    the prefix of its rows' view identities."""
    __slots__ = ('value', 'keys', 'open', 'order', 'starts', 'rows', 'path', 'name', 'depth')

    def __init__(self, value, path, name):
        self.value, self.keys, self.open = value, list(value), {}
        self.path, self.name, self.depth = path, name, len(path)

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
            for index, (key, item) in enumerate(value.items()):
                if branch(item, key) and path + (key,) not in self.flipped:
                    node.open[index] = self.grow(item, path + (key,), f'{name}{SEPARATOR}{key!r}')
        else:
            for found in self.flipped:
                if len(found) == len(path) + 1 and found[:-1] == path:
                    key = found[-1]
                    item = value.get(key, MISSING)
                    if item is not MISSING and branch(item, key):
                        node.open[node.keys.index(key)] = self.grow(item, found, f'{name}{SEPARATOR}{key!r}')
        node.measure()
        return node

    def check(self, node):
        """Follow the dicts where they changed under the tree: returns the node to use for `node`."""
        value = node.value
        if len(value) != len(node.keys):
            return self.grow(value, node.path, node.name)
        for index, child in list(node.open.items()):
            key = node.keys[index]
            item = value.get(key, MISSING)
            if item is MISSING:
                return self.grow(value, node.path, node.name)
            if item is child.value:
                node.open[index] = self.check(child)
            elif branch(item, key):
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
        item = node.value.get(key, MISSING)
        if item is MISSING or not branch(item, key):
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


@is_default_for(dict)
@gui(scroll=True, listen=('pointer', 'scroll'), width=420, height=480)
def draw_collection(input_value: dict, tree: Tree = None, expand=1, editable=True, width=0., height=0.,
                    scroll_offset=(0., 0.), view_events=None):
    """Draw and edit a dict. `expand` is how many levels are open before the user opens or closes
    any; `editable=False` shows the values read only."""
    rows = tree.rows
    if rows is None or rows.value is not input_value or rows.expand != expand:
        rows = tree.rows = Rows(input_value, expand, map(tuple, tree.flipped))
    elif not view_events:
        # No input brought this run: the dict may have changed outside.
        rows.top = rows.check(rows.top)
    top = scroll_offset[1]
    for event in view_events.get('pointer', ()):
        if event.kind == 'press' and not event.button:
            flipped = rows.flip(int((event.y + top) // ROW))
            if flipped is not None:
                tree.flipped = sorted(flipped, key=repr)
    table, draw_list = look(SLATE), imgui.get_window_draw_list()
    split = max(width * SPLIT, NARROW)
    wide = max(width - split - BAR - PAD, 1.)
    text_y = (ROW - imgui.get_text_line_height()) * .5
    extra = {} if editable else {'editable': False}
    changed = False
    first, count = int(top // ROW), int(height // ROW) + 2
    shown = rows.window(first, count)
    if any(node.keys[index] not in node.value for _, node, index in shown):
        # A key in view is gone: the dict changed outside.
        rows.top = rows.grow(rows.value, (), '')
        shown = rows.window(first, count)
    for row, node, index in shown:
        key = node.keys[index]
        value = node.value[key]
        view = default_for(value, key)
        opens = view is draw_collection and isinstance(value, dict)
        y, x = row * ROW - top, PAD + node.depth * INDENT
        # The label column, painted here in view coordinates: the body runs for every scroll.
        draw_list.push_clip_rect(0., y, split - PAD, y + ROW, True)
        if opens:
            middle = y + ROW * .5
            if index in node.open:
                draw_list.add_triangle_filled(x, middle - 3, x + 9, middle - 3, x + 4.5, middle + 4, table.dim)
            else:
                draw_list.add_triangle_filled(x + 1, middle - 5, x + 8, middle, x + 1, middle + 5, table.dim)
        draw_list.add_text(x + ARROW, y + text_y, table.text, str(key)[:LABEL])
        draw_list.pop_clip_rect()
        if opens:
            draw_list.add_text(split + PAD, y + text_y, table.dim, f'{len(value)} item{"s" * (len(value) != 1)}')
            continue
        edited, value = view(value, key=f'{node.name}{SEPARATOR}{key!r}', left=split, top=row * ROW + GAP,
                             width=wide, height=FIELD, **extra)
        if edited:
            node.value[key] = value
            changed = True
    end(None, name='end', left=0., top=max(rows.count - 1, 0) * ROW, width=1., height=ROW)
    reach = rows.count * ROW - height
    if reach > 0:
        # The scroll thumb: where the rows in view are among all of them.
        size = max(height * height / (reach + height), 24.)
        at = (height - size) * min(top / reach, 1.)
        draw_list.add_rect_filled(width - BAR + 2, at, width - 2, at + size, table.outline, 3.)
    return changed, input_value
