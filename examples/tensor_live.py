import numpy as np
import torch
import meltygui
from meltygui.view.code_view import draw_function_live

axis = torch.linspace(-1.5, 1.5, 40, device='cuda:0')
x, y, z = torch.meshgrid(axis, axis, axis, indexing='ij')
volume = torch.exp(-4 * ((torch.sqrt(x*x + y*y) - .85)**2 + z*z))


def wave(size=24, phase=0.0):
    coordinates = np.linspace(-3, 3, size)
    values = np.sin(coordinates[:, None] + coordinates[None, :] + phase)
    return values


@meltygui.glfw_window(name='Standalone CUDA tensor', width=850, height=700, app_id='meltygui-validation')
@meltygui.render_func(use_cache=False)
def tensor_window(input_value=None, draw_state=None):
    meltygui.draw_voxels(volume, name='CUDA torus', width=800, height=630)
    return False, input_value


@meltygui.glfw_window(name='Standalone live code', width=1000, height=720)
@meltygui.render_func(use_cache=False)
def live_window(input_value=None, draw_state=None):
    draw_function_live(wave, name='Wave lab')
    return False, input_value
