"""Throwaway Rust collision laboratory. Run with .venv/bin/python this_file.py."""
from meltygui import gui, os_window, columns, rows
import imgui
from meltygui.core.conversion.dict_conversion import DictConversion


class LabState(DictConversion):
    def __init__(self):
        super().__init__()
        self.show_window=True
        self.show_nested=True
        self.clicks=0
        self.amount=.5
        self.text='Drag the dividers'


@gui
def controls(input_value: object, draw_state=None):
    state=input_value
    imgui.text('Rust collision laboratory')
    imgui.text('Left drag a divider. Right drag a cell.')
    imgui.text('Hold the second right click for top/left.')
    changed,state.show_window=imgui.checkbox('Floating window',state.show_window)
    edited,state.show_nested=imgui.checkbox('Nested window',state.show_nested)
    changed |= edited
    if imgui.button('Count clicks',120,28):state.clicks+=1
    imgui.text(f'Clicks: {state.clicks}')
    _,state.amount=imgui.slider_float('Amount',state.amount,0.,1.)
    _,state.text=imgui.input_text('Text',state.text,256)
    imgui.text(f'Allocation: {draw_state.width:.0f} x {draw_state.height:.0f}')
    return changed,state


@gui
def panel(input_value: object, label='Cached panel', tint=(.12,.19,.24), draw_state=None):
    imgui.text(label)
    imgui.text(f'{draw_state.width:.0f} x {draw_state.height:.0f}')
    imgui.text('Empty space passes native movement through.')


@gui
def split_panel(input_value: object):
    with rows(('top','bottom'),mins=(90,90),padding=6) as layout:
        with layout.cell('top'):panel(None,key='top',label='Nested top row')
        with layout.cell('bottom'):panel(None,key='bottom',label='Nested bottom row')


@gui(min_width=140,min_height=100)
def floating(input_value: object):
    with columns(('left','right'),mins=70,padding=4) as layout:
        with layout.cell('left'):panel(None,key='left',label='Floating left')
        with layout.cell('right'):panel(None,key='right',label='Floating right')
    if input_value.show_nested:
        panel(None,key='nested',label='Parent-relative child',melty_window=True,
              initial={'window_pos':(90,130),'width':200,'height':100},min_width=100,min_height=60)


@os_window(name='Rust collision lab',width=1040,height=700,app_id='meltygui-collision-lab')
def app(state: LabState=None):
    with columns(('controls','rows','panel'),mins=(220,140,140),padding=6) as layout:
        with layout.cell('controls'):controls(state)
        with layout.cell('rows'):split_panel(None)
        with layout.cell('panel'):panel(None,label='Right panel')
    if state.show_window:
        floating(state,melty_window=True,initial={'window_pos':(390,270),'width':370,'height':260})
