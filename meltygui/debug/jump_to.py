import threading

from src.lsd.gl_gui.view.core_conversion import address
from src.lsd.gl_gui.view.core_conversion.address import Address
from src.lsd.gl_gui.view.core_views.core_render import render_func


@render_func(auto_resize=True)
def draw_jump_to(input_value: Address, unique):
    file_name = input_value.path.name if input_value.path is not None else "Unknown file"
    folder_icon = ""
    from src.lsd.gl_gui.view.core_views.new_core_view import button
    if button(f"{folder_icon} {file_name}##jump_to{unique}", height=30, draw=True, value=0.4, saturation=1.5)[0]:
        from src.lsd.gl_gui.utils.jump_to_code import open_in_intellij

        line_number = input_value.start + 1 if input_value.start is not None else None
        threading.Thread(
            target=open_in_intellij,
            args=(str(input_value.path),),
            kwargs={"line_number": line_number},
            daemon=True,
        ).start()


