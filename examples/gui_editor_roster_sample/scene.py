"""Sample for examples/gui_editor_roster.py: open on the right. It states no tint: every wash is another file's."""
import shapes
from palette import *
from shapes import Circle, square as box

SIZE = box(4)


def build(count):
    rings = [Circle(index) for index in range(count)]
    floor = Ground()
    print(f'{sky(2)} over {floor.dig()}')
    return rings, shapes.square(2), Ground.depth, rain(1)       # nothing imports `rain`: its one definition is a guess


def horizon(sky):
    return sky + 1      # a parameter: not the sky of palette.py
