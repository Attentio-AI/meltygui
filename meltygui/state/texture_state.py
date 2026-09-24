"""Prepared image presentation retained while the texture view's body is cached."""
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save


@no_save('source_id', 'texture_id', 'bright_texture_id', 'width', 'height',
         'zoom_state', 'color', 'flip_y', 'dim_outside', 'show_info', 'line_height')
class TextureViewState(DictConversion):
    def __init__(self):
        super().__init__()
        self.source_id = 0
        self.texture_id = 0
        self.bright_texture_id = 0
        self.width = 0
        self.height = 0
        self.zoom_state = None
        self.color = 0
        self.flip_y = False
        self.dim_outside = None
        self.show_info = True
        self.line_height = 0.0
