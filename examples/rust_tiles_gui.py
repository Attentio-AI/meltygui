"""Resizable rows and columns, directly in an OS window and in a Melty window."""
from meltygui import columns, gui, os_window, rows
import imgui


@gui
def table_cell(label='', draw_state=None):
    imgui.text(label)
    imgui.text(f'{draw_state.width:.0f} x {draw_state.height:.0f}')


@gui
def table_grid():
    # Each column has its own row dividers, to exercise nested layout constraints.
    # Change these tints to distinguish the three columns more strongly.
    column_tints = ((.16, .23, .29), (.18, .27, .24), (.28, .22, .18))
    with columns(('A', 'B', 'C'), mins=110, padding=4) as column_layout:
        for column, tint in zip(('A', 'B', 'C'), column_tints):
            with column_layout.cell(column):
                with rows((1, 2, 3), key=f'rows-{column}', mins=60, padding=6) as row_layout:
                    for row in (1, 2, 3):
                        with row_layout.cell(row):
                            table_cell(key=(column, row), label=f'{column}{row}', tint=tint)


@gui
def instructions(label=''):
    imgui.text(label)
    imgui.text('Left drag dividers. Right drag cells to resize.')
    imgui.text('Hold the second right click for top/left resize.')


@gui(min_width=350, min_height=270, tint=(.10, .13, .16, 1.))
def table(label=''):
    with rows(('heading', 'grid'), fixed=(76, None), mins=(76, 180), padding=6) as layout:
        with layout.cell('heading'):
            instructions(label=label)
        with layout.cell('grid'):
            table_grid()


@os_window(name="example main", width=1400, height=1000)
def example_app():
    # A filling cell connects the table's outer edges to the OS frame, so
    # resizing the outermost row/column resizes the native window as well.
    with rows(('content',), padding=0) as layout:
        with layout.cell('content'):
            table(label='OS window table', key='native-table')
    table(label='Nested window table', key='nested-table', melty_window=True,
          initial={'window_pos': (420, 240), 'width': 700, 'height': 460})
