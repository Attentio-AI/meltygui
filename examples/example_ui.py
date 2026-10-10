from meltygui import os_window
import imgui


@os_window(name="example main", width=1400, height=1000)
def example_app():
    
    imgui.text("hello")