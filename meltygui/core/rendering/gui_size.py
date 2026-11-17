"""What a `@gui` view's `width` or `height` can be besides a number of pixels.

    draw_view(value, width=FILL, height=WRAP)    # the same as width=1e8, height=None

The plain values need no import and mean the same; the names are for a call that
reads better with them. Resolved in `RetainedGui.gui` (retained_gui_prototype.py).
"""
# Any very large number: from the view's left / top edge to its parent's right / bottom one.
# Also what a view is given when neither the call nor its definition gives a size.
# Lever: `FILL_FROM`, the smallest number that fills. No view is that large (the largest is 8192
# px), so a size computed that high by mistake fills as well.
FILL = 1e8
FILL_FROM = 1e6
# As large as the view's content: what moved ImGui's cursor (widgets, dummies, child views).
WRAP = None

# A wrapped height where nothing bounds it (a scroll view's content, a parent that wraps too):
# the body is given this much to lay out in, the tallest a view can be.
UNBOUNDED = 8192.


class Canvas:
    """What a view was asked to be, for a body that needs to know: `canvas.width` and
    `canvas.height` are the values passed (the call's, else the definition's): a number of
    pixels, FILL or WRAP. The body's own `width` and `height` are always numbers: the space it
    has to draw in. Read only; ask for it with a `canvas` parameter."""
    __slots__ = ('width', 'height')

    def __init__(self, width, height):
        object.__setattr__(self, 'width', width)
        object.__setattr__(self, 'height', height)

    def __setattr__(self, name, value):
        raise AttributeError('canvas is read only: a size is passed on the call or the decorator')

    def __repr__(self):
        return f'Canvas(width={self.width!r}, height={self.height!r})'


_canvases = {}


def canvas(width, height):
    """One `Canvas` per request, so that an unchanged request is the same object (a view runs
    again when an argument is another object). Lever: `KEPT`, how many are remembered."""
    found = _canvases.get((width, height))
    if found is None:
        if len(_canvases) >= KEPT:
            _canvases.clear()
        found = _canvases[(width, height)] = Canvas(width, height)
    return found


KEPT = 512
