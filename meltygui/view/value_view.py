"""The built in value views of `draw_any` (core/rendering/gui_any.py): one `@gui` view per plain type.

    changed, value = draw_float(value, name='gain', width=160)

- `draw_bool`: a chip; a press flips it.
- `draw_int`, `draw_float`: drag sideways to change the number, click to type one. Enter takes what was
  typed, Escape drops it, so does leaving the field with something that is no number.
- `draw_str`: click to type; every keystroke is returned, Escape brings back the text it started with.
- `draw_none` and `draw_value` (whatever has no view of its own): read only.

Each view fills the box it is given (`width`, `height`) and listens for its own events, so a value
nobody touches is drawn once and never runs again. An edit returns a value of the input's own type.

The pointer is a view's only where it has a gesture of its own (`claim`): the chip of a boolean,
the field of a number or a text that can be edited. Everywhere else, and over every value that is
read only, a left drag moves the window the value is in (docs/WINDOW_COLLISION_COLUMNS.md, "Gestures").

To add a view for another type, write it and put `@is_default_for(type)` above `@gui`. A view's
colours are `look(tint)`: the `Tint` palette of the legacy value views, made from the view's own
tint (the constants below). The sizes are the levers below. Typing is a single line session of the
editor (`Field`), made when a field is first clicked.
"""
import reprlib
import threading
from collections import namedtuple

import meltygui_imgui as imgui

import meltygui.core.windowing.window_constants as glfw
from meltygui.core.input import mouse_cursor
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.rendering.gui_any import is_default_for
from meltygui.core.rendering.gui_prototype import gui
from meltygui.core.runtime.toggles import Tint
from meltygui.hdr_color import pack_color

ROW = 22.                        # height of a value view, unless its caller gives another
PAD = 6.                         # space left and right of the text in a field
ROUND = 4.                       # corner radius of a field
RING = 1.5                       # width of a field's outline
SLOP = 3.                        # pixels the pointer may travel in a click
NOTCH = 4.                       # pixels of a sideways drag per step of a number
FINE = .01                       # a float's step, as a share of the value the drag started at (at least of 1)
DIGITS = '.6g'                   # how a float is shown while it is not being typed
SHOWN = 240                      # characters of a text that are drawn while it is not being typed
LONG = 4096                      # a text longer than this is read only
NONE = pack_color(.164, .389, .197, .8)                  # the word None
# The tint each view's colours are made from: those of the legacy views, and a slate for text and the rest.
BOOL = (0., .527, .817)
INT = (0., .45, .85)
FLOAT = (0., .62, .72)
SLATE = (.22, .34, .45)
Look = namedtuple('Look', 'field on outline text dim select caret')
LOOKS = {}                       # (tint, the code of `look`) -> its table
WARM = []                        # the thread that loads the editor's session code, once started
REPR = reprlib.Repr()
REPR.maxstring = REPR.maxother = 80


def look(tint):
    """The colour table of a view tinted `tint`. To restyle the views, change a row. A table is kept
    per tint and per version of this function, so an edit here shows at once; one to a `Tint`
    formula shows after `LOOKS.clear()`."""
    key = (tint, look.__code__)
    table = LOOKS.get(key)
    if table is None:
        # The field-only push: `Tint` reads the style manager's tint, no ImGui style is touched.
        manager = Core.melty.style_manager
        held = manager.push_tint_fields(*tint)
        try:
            table = LOOKS[key] = Look(
                field=pack_color(*Tint.checkbox_bg(), 1.),               # a field's fill
                on=pack_color(*Tint.checkbox_bg_selected(), 1.),         # the fill of a True chip
                outline=pack_color(*Tint.checkbox_outline(), 1.),
                text=pack_color(*Tint.checkbox_text_true(), 1.),         # a value, a key
                dim=pack_color(*Tint.checkbox_text(), .45),              # False, and what is read only
                select=pack_color(*Tint.text_selection(), .4),
                caret=pack_color(*Tint.cursor_tint(), 1.))
        finally:
            manager.pop_tint_fields(held)
    return table


def box(draw_list, table, width, height, fill=None):
    """A field's box, which every editable value starts with. Returns the y its text goes at."""
    draw_list.add_rect_filled(0., 0., width, height, fill or table.field, ROUND)
    draw_list.add_rect(.5, .5, width - .5, height - .5, table.outline, ROUND, 0, RING)
    return (height - imgui.get_text_line_height()) * .5


def claim(cache, width, height):
    """The running view takes the pointer over its first `width` by `height` pixels, and nowhere when
    `width` is 0: the rest of its box stays the window's, which a left drag there moves."""
    cache.imgui_input.reach(cache.current, (0., 0., width, height))


# --- typing ------------------------------------------------------------------
def warm():
    """Load the editor's session code off the render thread, once: the first click into a field
    should not wait for the import."""
    if not WARM:
        WARM.append(threading.Thread(target=lambda: __import__('meltygui.editor.editor'), name='field-warm', daemon=True))
        WARM[0].start()


class Field:
    """A value being typed: a single line session of the editor (editor/editor.py) and how it is
    drawn inside a value view. `node` is the view it was made in (`cache.current`): a view that went
    out of sight and came back is another node and starts without it. `first` is the text it started
    with, `gave` the text its view last returned, `shift` the pixels the text is scrolled left to
    keep the caret in the box."""
    __slots__ = ('session', 'desk', 'node', 'first', 'gave', 'shift')

    def __init__(self, text, node, whole=True):
        from meltygui.editor import keys
        from meltygui.editor.editor import Editor
        self.node = node
        self.session = Editor(text, None, False)
        self.session.doc.record = None                  # what is typed here is no undo step of the editor's
        self.desk = keys.Desk(True, True, None, imgui.set_clipboard_text, imgui.get_clipboard_text)
        self.first = self.gave = text
        self.shift = 0.
        if whole:
            self.session.doc.select(len(text), 0)       # all of it selected: typing replaces it

    text = property(lambda self: self.session.doc.text)

    def load(self, text):
        """The caller passed another text than the one typed: show that one."""
        doc = self.session.doc
        doc.replace(0, doc.size, text)
        self.gave = text

    def index(self, x):
        """The caret position nearest to `x` pixels into the text."""
        text = self.text
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if imgui.calc_text_size(text[:middle]).x <= x:
                low = middle
            else:
                high = middle - 1
        if low < len(text):
            before, after = imgui.calc_text_size(text[:low]).x, imgui.calc_text_size(text[:low + 1]).x
            low += x - before > after - x
        return low

    def pointer(self, event):
        """A left press or drag at `event.x` of the view: the caret goes there (two presses take the
        word, three everything), a drag selects."""
        from meltygui.editor import edit
        doc = self.session.doc
        index = self.index(event.x - PAD + self.shift)
        if event.kind == 'drag':
            doc.select(index, doc.anchor)
        elif event.kind == 'press' and event.clicks == 2:
            edit.select_word(doc, index)
        elif event.kind == 'press' and event.clicks == 3:
            edit.select_all(doc)
        elif event.kind == 'press':
            edit.move(doc, index, select=event.shift)

    def typed(self, events):
        """Run the frame's key presses and typed text. Returns 'take' for Enter, 'drop' for Escape, else None."""
        from meltygui.editor import keys
        doc, pending = self.session.doc, ''.join(events.get('text', ()))
        for key, mods in events.get('keys', ()):
            if keys.printable(key, mods):
                # The character itself arrives as text; keep both in press order.
                if pending:
                    doc.replace(*doc.selection, pending[0])
                    pending = pending[1:]
            elif key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER):
                return 'take'
            elif key == glfw.KEY_ESCAPE:
                return 'drop'
            else:
                keys.press(self.session, self.desk, key, mods)
        if pending:
            doc.replace(*doc.selection, pending.split('\n')[0])
        return None

    def paint(self, draw_list, table, width, height, y):
        """The text, its selection and the caret, scrolled so that the caret is in the box."""
        doc = self.session.doc
        text, (low, high) = self.text, doc.selection
        across = lambda index: imgui.calc_text_size(text[:index]).x
        caret, room = across(doc.caret), width - 2 * PAD
        self.shift = max(min(self.shift, caret), caret - room, 0.)
        left = PAD - self.shift
        draw_list.push_clip_rect(PAD - 1., 0., width - PAD + 2., height, True)
        if low < high:
            draw_list.add_rect_filled(left + across(low), 2., left + across(high), height - 2., table.select, 2.)
        draw_list.add_text(left, y, table.text, text)
        draw_list.add_rect_filled(left + caret, 3., left + caret + 1.5, height - 3., table.caret)
        draw_list.pop_clip_rect()


# --- views -------------------------------------------------------------------
@is_default_for(type(None))
@gui(listen=(), height=ROW)
def draw_none(input_value: type(None) = None, height=0.):
    imgui.get_window_draw_list().add_text(PAD, (height - imgui.get_text_line_height()) * .5, NONE, 'None')


@is_default_for(object)
@gui(listen=(), height=ROW)
def draw_value(input_value: object = None, width=0., height=0.):
    """A value without a view of its own: a bounded `repr`, read only."""
    draw_list = imgui.get_window_draw_list()
    draw_list.push_clip_rect(0., 0., width, height, True)
    draw_list.add_text(PAD, (height - imgui.get_text_line_height()) * .5, look(SLATE).dim, REPR.repr(input_value))
    draw_list.pop_clip_rect()


@is_default_for(bool)
@gui(listen=('pointer',), cursor=mouse_cursor.HAND, height=ROW)
def draw_bool(input_value: bool, editable=True, width=0., height=0., cache=None, view_events=None):
    """A chip as wide as its label; a press on it flips the value."""
    table, draw_list = look(BOOL), imgui.get_window_draw_list()
    on, off = f" True", f" False"
    # Lever: the chip's width. Both labels are measured so that a flip does not move its edge.
    wide = min(max(imgui.calc_text_size(on).x, imgui.calc_text_size(off).x) + 2 * PAD, width)
    claim(cache, wide * editable, height)
    flipped = editable and any(event.kind == 'press' and not event.button for event in view_events.get('pointer', ()))
    value = input_value != flipped
    y = box(draw_list, table, wide, height, table.on if value else table.field)
    draw_list.add_text(PAD, y, table.text if value else table.dim, on if value else off)
    return flipped, value


def number(value, tint, shown, step, editable, width, height, cache, draw_state, field, grab, events):
    """The body `draw_int` and `draw_float` share: returns the value after this frame's input, of the
    type it came in. `shown(value)` is its text, `step(start)` what one notch of a drag adds to a
    value that started at `start`. `field` is the typing session, `grab` the press that may become a
    drag: (x, the value there, dragged)."""
    kind, was, held = type(value), field, grab
    claim(cache, width * editable, height)
    node = cache.current
    if field is not None and field.node != node:
        field = None

    def take():
        """The number typed, or the value as it was."""
        try:
            return kind(field.text.strip())
        except ValueError:
            return value

    for gained in events.get('focus', ()):
        if not gained and field is not None:
            value, field = take(), None
    for event in events.get('pointer', ()) if editable else ():
        if event.button:
            continue
        if field is not None:
            field.pointer(event)
        elif event.kind == 'press':
            grab = (event.x, value, False)
        elif event.kind == 'drag' and grab is not None and (grab[2] or abs(event.x - grab[0]) >= SLOP):
            grab = (grab[0], grab[1], True)
            value = kind(grab[1] + int((event.x - grab[0]) / NOTCH) * step(grab[1]))
        elif event.kind == 'release' and grab is not None:
            if not grab[2]:
                field = Field(repr(value), node)    # a click: type the number
            grab = None
    if field is not None and ('keys' in events or 'text' in events):
        done = field.typed(events)
        if done is not None:
            value, field = take() if done == 'take' else value, None
    if field is not was:
        draw_state.field = field
    if grab is not held:
        draw_state.grab = grab
    table, draw_list = look(tint), imgui.get_window_draw_list()
    y = box(draw_list, table, width, height)
    if field is not None:
        field.paint(draw_list, table, width, height, y)
    else:
        draw_list.push_clip_rect(0., 0., width - PAD, height, True)
        draw_list.add_text(PAD, y, table.text, shown(value))
        draw_list.pop_clip_rect()
    return value


@is_default_for(int)
@gui(listen=('pointer', 'keys', 'text', 'focus'), cursor=mouse_cursor.RESIZE_EW, no_save=('field', 'grab'), height=ROW)
def draw_int(input_value: int, editable=True, field=None, grab=None, width=0., height=0., draw_state=None, cache=None,
             view_events=None):
    """A whole number: drag sideways to change it, click to type it."""
    warm()
    value = number(input_value, INT, str, lambda start: 1, editable, width, height, cache, draw_state, field, grab,
                   view_events)
    return value != input_value, value


@is_default_for(float)
@gui(listen=('pointer', 'keys', 'text', 'focus'), cursor=mouse_cursor.RESIZE_EW, no_save=('field', 'grab'), height=ROW)
def draw_float(input_value: float, editable=True, field=None, grab=None, width=0., height=0., draw_state=None, cache=None,
               view_events=None):
    """A number: drag sideways to change it by hundredths of what it was, click to type it."""
    warm()
    value = number(input_value, FLOAT, lambda value: format(value, DIGITS), lambda start: max(abs(start), 1.) * FINE,
                   editable, width, height, cache, draw_state, field, grab, view_events)
    return value != input_value, value


@is_default_for(str)
@gui(listen=('pointer', 'keys', 'text', 'focus'), cursor=mouse_cursor.TEXT, no_save=('field',), height=ROW)
def draw_str(input_value: str, editable=True, field=None, width=0., height=0., draw_state=None, cache=None,
             view_events=None):
    """A line of text: click to type. A text of several lines, or a very long one, is shown read only."""
    warm()
    value, was = input_value, field
    lines = value.count('\n') + 1 if len(value) <= LONG else 0
    claim(cache, width * (editable and lines == 1), height)
    if field is not None and field.node != cache.current:
        field = None
    elif field is not None and value != field.gave:
        field.load(value)
    for gained in view_events.get('focus', ()):
        if not gained:
            field = None
    for event in view_events.get('pointer', ()) if editable and lines == 1 else ():
        if event.button:
            continue
        if field is None and event.kind == 'press':
            field = Field(value, cache.current, whole=False)
        if field is not None:
            field.pointer(event)
    if field is not None and ('keys' in view_events or 'text' in view_events):
        done = field.typed(view_events)
        # (Escape brings the text back that the field started with.)
        value = type(input_value)(field.first if done == 'drop' else field.text)
        field.gave = value
        if done is not None:
            field = None
    if field is not was:
        draw_state.field = field
    table, draw_list = look(SLATE), imgui.get_window_draw_list()
    y = box(draw_list, table, width, height)
    if field is not None:
        field.paint(draw_list, table, width, height, y)
    else:
        shown = value[:SHOWN] if lines == 1 else f'{value[:SHOWN].split(chr(10))[0]}   ({lines} lines)' if lines \
            else f'{value[:SHOWN].split(chr(10))[0]}   ({len(value)} characters)'
        draw_list.push_clip_rect(0., 0., width - PAD, height, True)
        draw_list.add_text(PAD, y, table.text if lines == 1 else table.dim, shown)
        draw_list.pop_clip_rect()
    return value != input_value, value
