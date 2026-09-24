"""The import graph meltygui's boot relies on (core/runtime/app.py).

An app imports ``meltygui``, core_render and its views before its first
``@glfw_window``; that decoration starts the import thread and the display.
The overlap only exists while those imports stay light: the render libraries
load on the thread, and the code-editing stack (libcst) loads with the first
code edit. These tests pin that shape so a stray top-level import does not
quietly put 100 ms back on the main thread.
"""
import subprocess
import sys

RENDER_LIBRARIES = ('numpy', 'OpenGL.GL', 'PIL', 'pygments')
CODE_STACK = ('libcst', 'meltygui.code.libcst_conversion', 'meltygui.code.chain_converters',
              'meltygui.code.new_codecs', 'meltygui.code.new_converters', 'meltygui.editor.text_editor',
              'meltygui.view.code_view')
# The GL side of the runtime: imported by the boot thread, never by a view.
GL_MODULES = ('meltygui.core.cache.tile_cache', 'meltygui.core.windowing.surface',
              'meltygui.core.graphics.gl_state', 'meltygui.model.texture_model')


def imported_after(*statements):
    """The modules a fresh interpreter has loaded after running `statements`."""
    code = '\n'.join(statements) + '\nimport sys\nprint("\\n".join(sorted(sys.modules)))'
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True)
    return set(result.stdout.split())


def test_package_and_render_graph_stay_light():
    loaded = imported_after('import meltygui',
                            'from meltygui.core.core_render import render_func',
                            'from meltygui.core.rendering.render_dispatch import draw_any',
                            'import meltygui.view.collection_view',
                            'import meltygui.view.control_view',
                            'import meltygui.view.text_view',
                            'import meltygui.view.header_view',
                            'import meltygui.view.layout_view',
                            'import meltygui.core.layout.tile_manager_core')
    assert 'meltygui.core.melty' in loaded
    assert not loaded & set(RENDER_LIBRARIES), loaded & set(RENDER_LIBRARIES)
    assert not loaded & set(CODE_STACK), loaded & set(CODE_STACK)
    assert not loaded & set(GL_MODULES), loaded & set(GL_MODULES)


def test_renderer_policies_load_without_the_code_stack():
    """mode.py names every built-in view; its code-mode members build their
    policies on first use, so the first frame's window modes cost no libcst."""
    loaded = imported_after('import meltygui.core.rendering.mode as mode',
                            'assert mode.Mode.WINDOW.get_config_for(the_type=dict).kwargs["closable"]')
    assert 'libcst' not in loaded
    loaded = imported_after('import meltygui.core.rendering.mode as mode',
                            'assert mode.Mode.CODE_UI.get_config_for(the_type=type).recursive')
    assert 'libcst' in loaded


def test_default_renderers_registered_by_name_resolve():
    """Views registered for the code stack's types by name (draw_text for
    CodeLine, draw_collection for the parse dicts) dispatch once it loads."""
    from meltygui.core.melty import Melty
    import meltygui.view.text_view
    import meltygui.view.collection_view
    from meltygui.code.libcst_conversion import CodeLine, GeneralParse
    assert Melty.default_funcs_by_name['CodeLine'] is meltygui.view.text_view.draw_text
    assert Melty.get_default_view_function(real_type=CodeLine, value=CodeLine('x')) is meltygui.view.text_view.draw_text
    assert Melty.get_default_view_function(real_type=GeneralParse, value=GeneralParse()) is not None


def test_codec_registry_is_light_and_fills_when_codecs_load():
    from meltygui.code.codec_registry import type_to_codec, codec_for_type
    import types
    from meltygui.core.core_render import _codec_for_type
    before = len(type_to_codec)
    import meltygui.code.new_codecs  # noqa: F401
    assert len(type_to_codec) > before or before > 0
    assert codec_for_type(types.FunctionType) is _codec_for_type(types.FunctionType) is not None


def test_import_thread_covers_the_first_frame(tmp_path):
    """Everything _run_imports loads is importable without a display, with
    and without the warm-start hint for the code stack."""
    from meltygui.core.runtime import app
    from meltygui.core.styling import warm_start
    app._state['cache'] = tmp_path
    app._run_imports()
    assert app._state['import_error'] is None, app._state['import_error']
    assert 'OpenGL.GL' in sys.modules and 'meltygui.core.rendering.mode' in sys.modules
    warm_start.remember_code_stack(tmp_path, True)
    assert warm_start.code_stack_hint(tmp_path)
    app._run_imports()
    assert app._state['import_error'] is None, app._state['import_error']
    assert 'meltygui.code.libcst_conversion' in sys.modules
    warm_start.remember_code_stack(tmp_path, False)
    assert not warm_start.code_stack_hint(tmp_path)
    assert 'OpenGL.GL' in sys.modules and 'meltygui.core.rendering.mode' in sys.modules
