"""Context menus of `@gui` views.

    def delete(item, items):
        items.pop(item)

    @context_menu({'Delete': delete, 'More': {'Say hello': lambda: print('hello')}})
    @gui(height=24)
    def draw_row(item, items=None):
        ...

`@context_menu(options)` goes above `@gui` (or `@os_window`). A right click on the view shows its
options, then, under a line each, those of every view around it, innermost first. It acts on the
release, so a right drag still resizes. An option's function is called with the parameters of its
view that it names (`item`, `items`, `draw_state`, `cache`), or with nothing. An option that is a
dict of options is a submenu, to any depth; its functions are called the same way.

Where things are, and how to change them:
- `MENUS`: view name -> its options. The decorator writes that row and one in `AFTER`
  (core/rendering/gui_prototype.py) and hands the view back as it was: a view without a menu runs
  the code it always did, and with no menu in the app a frame pays one truth test
  (`ImGuiInput.process`, core/rendering/gui_imgui_input.py). Rows are by name, so a source edit of
  the options is the row the live views read.
- `click`: the router calls it for a right click. It gathers the sections and has the innermost
  view that has one run again.
- `attached`, `show`: after that view's body (`AFTER`) the menu is declared as its popup. The menu
  is so a child of the view it was opened on: placed from its corner, closed with it, and on a
  view inside a popup it is below that popup, which stays open. A press outside, Escape and the
  keyboard are the popup rules of core/input/focus.py.
- The menu itself, and how an option's arguments are filled: view/context_menu_view.py.

An immediate view (`use_cache=False`) has no place in the graph and so no menu, with one exception:
the root of a window. Its options come last, and the surface declares the menu when no cached view
under the pointer has one (`ImGuiInput.root`, gui_window_prototype.py).
"""
from meltygui.core.input.event import RIGHT
from meltygui.core.rendering.gui_prototype import AFTER, _renderer_name

# view name (`_renderer_name`) -> {label: function}
MENUS = {}


class Menu:
    """The open context menu of one surface (`ImGuiInput.menu`)."""
    __slots__ = ('node', 'sections', 'left', 'top', 'width', 'height', 'fresh')

    def __init__(self, node, sections, left, top, width, height):
        self.node = node                    # the view it is a popup of; 0: the surface, for an immediate root
        self.sections = sections            # [(view, options)], innermost first; 0 is the immediate root
        self.left, self.top = left, top     # where it opened, in that view's coordinates
        self.width, self.height = width, height
        self.fresh = True                   # until it was declared once: the popup's open event


def check(options):
    """Raise unless `options` is what a menu is made of: {label: function}, or in place of a
    function a dict of the same kind, a submenu."""
    if not isinstance(options, dict) or not options:
        raise TypeError('@context_menu takes a dict of {label: function}, a submenu a dict of the same in place of a function')
    for label, action in options.items():
        if isinstance(action, dict):
            check(action)
        elif not isinstance(label, str) or not callable(action):
            raise TypeError(f'@context_menu options are {{label: function}}, not {label!r}: {action!r}')


def context_menu(options):
    """Give the view below a right click menu. `options` is {label: function}, in the order shown;
    a dict of options in place of a function is a submenu."""
    check(options)

    def register(view):
        if not getattr(view, '__gui__', False):
            raise TypeError('@context_menu goes above @gui')
        name = _renderer_name(view.__gui_definition__[0])
        MENUS[name] = options
        AFTER[name] = attached(name)
        return view
    return register


def attached(name):
    """The `AFTER` step of the view `name`: when the open menu is on the view that just ran, declare
    it. An immediate view runs inside another view's execution, or none: it is not that view."""
    def after(cache):
        menu = cache.imgui_input.menu
        if menu is None or not cache.stack:
            return
        node = cache.stack[-1]
        if menu.node == node and _renderer_name(cache.records[node][2]) == name:
            show(cache, node)
    return after


def nearer(cache, node, asker):
    """Whether a view with a menu is under the pointer inside `asker`, a view that asked about the
    right button. The menu is then the nearer of the two and takes the click (`ImGuiInput.deliver`)."""
    records, parent = cache.records, cache.graph.parent
    while node and node != asker:
        record = records.get(node)
        if record is not None and _renderer_name(record[2]) in MENUS:
            return True
        node = parent(node)
    return False


def click(cache, hit, position):
    """A right click at `position` (surface coordinates) over the view `hit`: gather the options of
    the views around it, innermost first, and have the innermost one that has any show them."""
    from meltygui.view.context_menu_view import draw_context_menu, extent
    router = cache.imgui_input
    records, parent, inputs = cache.records, cache.graph.parent, cache.inputs
    menu_body = draw_context_menu.__gui_definition__[0]
    sections = []
    node = hit
    while node:
        record = records.get(node)
        if record is not None:
            if record[2] is menu_body:
                return                      # a click in an open menu is that menu's
            options = MENUS.get(_renderer_name(record[2]))
            if options:
                sections.append((node, options))
            elif not sections:
                event = inputs.get(node)
                if event is not None and event.asked & RIGHT:
                    return                  # a view that uses the right click itself is nearer
        node = parent(node)
    options = MENUS.get(router.root)
    if options:
        sections.append((0, options))
    if not sections:
        return
    target = sections[0][0]
    if target:
        left, top = router.local(target, position)
    else:
        left, top = position[0] - cache.origin[0], position[1] - cache.origin[1]
    router.menu = Menu(target, sections, left, top, *extent(sections))
    # A menu opens on the release, so the press that closed the last one may open the next.
    cache.focus.shut.clear()
    if target:
        cache.invalidate_id(target)         # its run declares the popup (`show`)
    elif cache.request_frame:
        cache.request_frame()


def show(cache, node):
    """Declare the open menu as a popup of `node`, the view it is attached to, from that view's run
    (`attached`). The surface calls it with 0 for the menu of an immediate root."""
    router = cache.imgui_input
    menu = router.menu
    if menu is None or menu.node != node:
        return
    from meltygui.view.context_menu_view import draw_context_menu
    _, _, state = draw_context_menu(menu.sections, name='context menu', popup=menu.fresh, return_extras=True,
                                    left=menu.left, top=menu.top, width=menu.width, height=menu.height)
    menu.fresh = False
    if state is None and router.menu is menu:
        router.menu = None                  # it closed: a pick, Escape or a press outside
