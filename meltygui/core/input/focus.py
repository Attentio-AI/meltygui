"""Keyboard focus and temporary popups of one `@gui` surface.

One view owns the keyboard (`keyboard`): its `event` has the keys and the typed
text, and `event.is_focused()` is true (core/input/event.py). A temporary popup
is a view called with `popup=<open event>`; it is open exactly while it is on
`stack`:

    edited, picked = options(value, name='options', popup=pressed, left=0.0, top=height + 2.0)

Nobody keeps an open flag. The rules, all in this file:

- A truthy `popup=` opens it. One whose body asks for keys takes the keyboard
  (`ran`: after its first run, which is where it asks).
- A press outside its subtree closes it and still reaches what is under it.
  A popup opened from a popup is its descendant: a press in the child keeps
  the parent. The press that closed a popup cannot open it again, so a press
  on its anchor toggles.
- Escape closes the innermost one and is not delivered as a key.
- The keyboard leaving a popup that held it closes that popup.
- Closing gives the keyboard back to the view that had it at the opening,
  when it is still inside the popup, and runs the declaring view again.
- `cache.focus.close()` in a popup's body closes it (a pick was made).

`within(node)` is for a view that wants to know whether the keyboard is with
it or with a popup opened from it (an editor with `focus=True` does not take
the keyboard back from its own picker).
"""
from meltygui.core.input.event import KEYS
from meltygui.core.windowing.window_constants import KEY_ESCAPE


class Popup:
    """One open popup: its identity handle, node, declaring view and the keyboard's owner when it opened.
    `fresh` holds until its body has run in this opening (`Focus.ran`)."""
    __slots__ = ('handle', 'node', 'parent', 'back', 'fresh')

    def __init__(self, handle, node, parent, back):
        self.handle, self.node, self.parent, self.back = handle, node, parent, back
        self.fresh = True


class Focus:
    def __init__(self, cache):
        self.cache = cache
        self.keyboard = None       # the view that gets keys and text
        self.stack = []            # open popups, outermost first; each is a descendant of the one before
        self.open = {}             # identity handle -> Popup, for the declaration's lookup
        self.shut = set()          # handles the current press closed: it cannot open them again
        self.closed = {}           # handle -> node of a popup just closed, until its declaring view ran (`result`)
        self.dropped = False       # the keyboard's owner was retired, not replaced

    def inside(self, node, root):
        """Whether `node` is `root` or below it; a popup is below the view that declared it."""
        graph = self.cache.graph
        while node:
            if node == root:
                return True
            node = graph.parent(node) if graph.contains(node) else 0
        return False

    def within(self, node):
        """Whether the keyboard is with `node`, a view in it or a popup opened from it."""
        return self.keyboard is not None and self.inside(self.keyboard, node)

    def take(self, node):
        """Move the keyboard to `node` (None releases it). A popup that held it and does not hold `node` closes."""
        old = self.keyboard
        if node == old:
            return
        tell = self.cache.imgui_input.keyboard
        tell(old, False)
        self.keyboard, self.dropped = node, False
        tell(node, True)
        if old is not None:
            for at, popup in enumerate(self.stack):
                if self.inside(old, popup.node) and not self.inside(node, popup.node):
                    self.shed(at)
                    break

    def shown(self, handle, request):
        """Whether the popup `handle` is to be declared: it is open, or `request` opens it."""
        return handle in self.open or (bool(request) and handle not in self.shut)

    def result(self, handle, input_value):
        """(changed, value) for the declaration of a closed popup: what it returned as it closed (a pick), once."""
        node = self.closed.pop(handle, None) if self.closed else None
        if node is not None and self.cache.graph.contains(node):
            changed, value = self.cache.graph.result(node, True)
            if changed:
                return True, value
        return False, input_value

    def push(self, handle, node, parent):
        """The popup `handle` was declared as `node` by `parent`: open it unless it is."""
        if handle in self.open:
            self.open[handle].node = node
            return
        # Only a chain of popups is open: one that does not hold the new one closes.
        for at, popup in enumerate(self.stack):
            if not self.inside(parent, popup.node):
                self.shed(at)
                break
        popup = self.open[handle] = Popup(handle, node, parent, self.keyboard)
        self.stack.append(popup)
        if self.cache.request_frame:
            self.cache.request_frame()

    def ran(self, handle):
        """The popup `handle` was declared: once its body has run in this opening, one that asked
        for keys takes the keyboard. (Its body is where it asks, so this cannot be known at `push`.)"""
        popup = self.open.get(handle)
        if popup is None or not popup.fresh or popup.node not in self.cache.backgrounds:
            return
        popup.fresh = False
        event = self.cache.inputs.get(popup.node)
        if event is not None and event.asked & KEYS:
            self.take(popup.node)

    def press(self, hit):
        """A button went down over `hit`: the popups it is not in close."""
        self.shut.clear()
        for at, popup in enumerate(self.stack):
            if not self.inside(hit, popup.node):
                self.shut.update(found.handle for found in self.stack[at:])
                self.shed(at)
                break

    def escape(self, keys):
        """Take the frame's Escape presses out of `keys`: each closes the innermost popup."""
        for at in range(len(keys) - 1, -1, -1):
            if keys[at][0] == KEY_ESCAPE and self.stack:
                del keys[at]
                self.shed(len(self.stack) - 1)

    def close(self, node=None):
        """Close the popup `node` (the running view) is in, and those opened from it."""
        node = self.cache.current if node is None else node
        for at in range(len(self.stack) - 1, -1, -1):
            if self.inside(node, self.stack[at].node):
                self.shed(at)
                break

    def shed(self, at):
        """Close the popups from `at` on: the keyboard goes back, their declaring views run again."""
        cache = self.cache
        closed, self.stack[at:] = self.stack[at:], []
        for popup in closed:
            self.open.pop(popup.handle, None)
            self.closed[popup.handle] = popup.node
        first = closed[0]
        if self.keyboard is not None and self.inside(self.keyboard, first.node):
            self.take(first.back if first.back in cache.records else None)
        for popup in closed:
            # The declaring view's next run does not declare it: the node retires there.
            if popup.parent in cache.records:
                cache.invalidate_id(popup.parent)
        if cache.request_frame:
            cache.request_frame()

    def retire(self, node):
        """`node` is gone: the keyboard it held is released, a popup it was is closed."""
        if node == self.keyboard:
            self.keyboard, self.dropped = None, True
        if self.closed:
            self.closed = {handle: found for handle, found in self.closed.items() if found != node}
        for at, popup in enumerate(self.stack):
            if popup.node == node:
                # Its declaring view stopped calling it (or went itself): nothing is left to run again.
                del self.stack[at]
                self.open.pop(popup.handle, None)
                if self.dropped and self.keyboard is None and popup.back in self.cache.records:
                    self.take(popup.back)
                break
