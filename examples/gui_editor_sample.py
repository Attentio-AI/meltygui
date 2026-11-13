"""Sample source for examples/gui_editor.py: every editor feature has something here.

Numbers, booleans and colours are inline widgets. `mix` and `Palette` carry
tints. `demo` runs for live values. The last function has lint findings.
"""
import math
import os
from dataclasses import dataclass


# A comment run folds by default.
# It keeps its first line visible.
# Click the chevron in the gutter to open it.
SCALE = 1.5
STEPS = 12
ENABLED = True
ACCENT = (0.2, 0.55, 0.9)
WARM = '#e0703a'


def styled(tint):
    """A decorator carrying a tint, as Melty views do."""
    return lambda function: function


@styled(tint=(0.2, 0.5, 0.9))
def mix(low, high, amount=0.25):
    """Blend two numbers."""
    if amount < 0.0:
        amount = 0.0
    elif amount > 1.0:
        amount = 1.0
    return low + (high - low) * amount


@dataclass
class Palette:
    tint = (0.9, 0.45, 0.15)
    name: str = 'sunset'
    size: int = 4

    def shades(self, base=0.4):
        result = []
        for index in range(self.size):
            shade = mix(base, 1.0, index / max(self.size - 1, 1))
            result.append(round(shade, 3))
        return result

    def describe(self):
        return f'{self.name}: {len(self.shades())} shades'


def demo(count=4):
    """Run this for live values: each assignment shows its value inline."""
    palette = Palette(size=count)
    shades = palette.shades()
    total = sum(shades)
    mean = total / len(shades)
    label = palette.describe()
    ready = mean > 0.5
    angle = math.degrees(math.atan2(mean, SCALE))
    return label, ready, angle


def area(radius):
    return math.pi * radius ** 2


def table(rows=3, columns=4):
    cells = []
    for row in range(rows):
        line = []
        for column in range(columns):
            line.append(area(row + column) * SCALE)
        cells.append(line)
    return cells


def join(parts, separator=', '):
    return separator.join(str(part) for part in parts)


def broken(path):
    data = np.zeros(STEPS)
    return Path(path).name, data, os.sep
