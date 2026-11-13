"""Sample for examples/gui_editor_roster.py: colours. On disk only: the toolbar pushes another sky tint."""


def styled(tint):
    return lambda function: function


@styled(tint=(0.2, 0.5, 0.9))
def sky(height):
    return height * 2


# [tint=(0.9, 0.45, 0.15)]
class Ground:
    depth = 3  # [tint=(0.3, 0.8, 0.3)]

    def dig(self):
        return self.depth
