"""Sample for examples/gui_editor_roster.py: open on the left. Change a tint here: the right editor follows."""
from palette import Ground, sky


class Circle:  # [tint=(0.6, 0.3, 0.9)]
    def __init__(self, radius):
        self.radius = radius

    def area(self):
        return 3.14159 * self.radius ** 2 + sky(Ground.depth)


def square(side):  # [tint=(0.9, 0.8, 0.2)]
    return side * side


def rain(amount):  # [tint=(0.2, 0.8, 0.8)]
    return amount
