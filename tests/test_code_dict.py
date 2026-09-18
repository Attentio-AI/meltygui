"""CodeDict: a definition as a mutable dict whose writes reach the source
(through PendingSave), the live object and the launch overrides."""
import importlib
import sys
import textwrap

import pytest

from meltygui.code.fileref import _EDITABLE_ROOTS
from meltygui.code.fileref import _EDITABLE_SOURCE_CACHE
from meltygui.code.fileref import add_editable_root
from meltygui.core.runtime import launch_override
from meltygui.editor.pending_save import PendingSave
from meltygui.model import code_dict_model
from meltygui.model.code_dict_model import CodeDict
from meltygui.model.code_dict_model import CodeList
from meltygui.model.code_dict_model import Codebase
from meltygui.model.code_dict_model import Hotswap
from meltygui.model.code_dict_model import LaunchOverride

SOURCE = textwrap.dedent('''\
    LIMIT = 10


    class Settings:
        my_dict = {"0": False}
        my_list = [1, 2]
        # keep me
        speed = 1.5

        class SomeInnerClass:
            my_int_toggle = 1


    def work(a=3):
        step = 5
        return a * step
''')


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A throwaway editable package `cd_pkg` with one module `settings`."""
    package = tmp_path / "cd_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "settings.py").write_text(SOURCE)
    roots = list(_EDITABLE_ROOTS)
    add_editable_root(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(launch_override, "_state",
                        dict(path=tmp_path / "launch_overrides.json", overrides={}, dirty=False, hooked=True))
    module = importlib.import_module("cd_pkg.settings")
    yield module
    for name in [name for name in sys.modules if name.startswith("cd_pkg")]:
        del sys.modules[name]
    for key in [key for key in code_dict_model._cores if key[0].startswith("cd_pkg")]:
        del code_dict_model._cores[key]
    for address in [a for a in PendingSave.pending_saves if tmp_path in a.path.parents]:
        PendingSave.pending_saves.pop(address, None)
        PendingSave.originals.pop(address, None)
    _EDITABLE_ROOTS[:] = roots
    _EDITABLE_SOURCE_CACHE.clear()


def pending_text(module):
    return PendingSave.current_file_text(module.__file__)


def test_reads_mirror_the_class(project):
    settings = CodeDict(project.Settings)
    assert settings["my_dict"] == {"0": False}
    assert settings["my_list"] == [1, 2]
    assert settings["speed"] == 1.5
    assert settings["SomeInnerClass"]["my_int_toggle"] == 1
    assert "__origin__" not in settings
    assert settings["SomeInnerClass"] is settings["SomeInnerClass"]


def test_codebase_write_queues_a_pending_save_and_leaves_disk_and_runtime(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    settings["my_dict"]["0"] = True
    settings["SomeInnerClass"]["my_int_toggle"] = 2
    expected = SOURCE.replace('{"0": False}', '{"0": True}').replace("my_int_toggle = 1", "my_int_toggle = 2")
    assert pending_text(project) == expected
    assert open(project.__file__).read() == SOURCE
    assert project.Settings.SomeInnerClass.my_int_toggle == 1
    assert settings["SomeInnerClass"]["my_int_toggle"] == 2


def test_hotswap_write_changes_the_live_object_only(project):
    live_dict = project.Settings.my_dict
    settings = CodeDict(project.Settings, write_to=Hotswap)
    settings["my_dict"]["0"] = True
    settings["SomeInnerClass"]["my_int_toggle"] = 2
    settings["my_list"].append(3)
    assert project.Settings.my_dict is live_dict and live_dict == {"0": True}
    assert project.Settings.SomeInnerClass.my_int_toggle == 2
    assert project.Settings.my_list == [1, 2, 3]
    assert not PendingSave.pending_saves or pending_text(project) == SOURCE


def test_handles_with_different_sinks_stay_linked(project):
    code_only = CodeDict(project.Settings, write_to=Codebase)
    live_too = CodeDict(project.Settings, write_to=(Codebase, Hotswap))
    assert code_only is not live_too
    live_too["SomeInnerClass"]["my_int_toggle"] = 2
    assert code_only["SomeInnerClass"]["my_int_toggle"] == 2
    code_only["speed"] = 3.0
    assert live_too["speed"] == 3.0
    assert project.Settings.speed == 1.5                     # code_only never hotswaps
    assert project.Settings.SomeInnerClass.my_int_toggle == 2
    text = pending_text(project)
    assert "my_int_toggle = 2" in text and "speed = 3.0" in text


def test_a_nested_class_shares_its_top_level_core(project):
    inner = CodeDict(project.Settings.SomeInnerClass, write_to=Codebase)
    outer = CodeDict(project.Settings)
    inner["my_int_toggle"] = 7
    assert outer["SomeInnerClass"]["my_int_toggle"] == 7
    assert len([a for a in PendingSave.pending_saves if a.path.name == "settings.py"]) == 1


def test_another_writer_on_the_span_is_not_reverted(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    core = settings._core
    other = SOURCE[SOURCE.index("class Settings"):SOURCE.index("def work")].rstrip("\n")
    PendingSave.queue_save(address=core.address, codec=core.codec,
                           data=other.replace("speed = 1.5", "speed = 9.0"), wake=False)
    settings["SomeInnerClass"]["my_int_toggle"] = 2
    text = pending_text(project)
    assert "speed = 9.0" in text and "my_int_toggle = 2" in text
    assert settings["speed"] == 1.5     # effective value: the live class still says 1.5


def test_function_defaults_and_locals(project):
    work = CodeDict(project.work, write_to=(Codebase, Hotswap))
    assert work["parameters"]["a"] == 3 and work["locals"]["step"] == 5
    work["parameters"]["a"] = 4
    work["locals"]["step"] = 6
    assert project.work() == 24
    text = pending_text(project)
    assert "def work(a=4):" in text and "step = 6" in text


def test_module_and_package_recursion(project):
    package = CodeDict(sys.modules["cd_pkg"], write_to=(Codebase, Hotswap))
    assert set(package) == {"settings", "__init__"}
    settings = package["settings"]
    assert settings["LIMIT"] == 10
    settings["Settings"]["SomeInnerClass"]["my_int_toggle"] = 2
    assert project.Settings.SomeInnerClass.my_int_toggle == 2
    assert CodeDict(project.Settings)["SomeInnerClass"]["my_int_toggle"] == 2
    assert "my_int_toggle = 2" in pending_text(project)
    settings["LIMIT"] = 11
    assert project.LIMIT == 11


def test_launch_override_records_the_expression_and_applies_at_launch(project):
    settings = CodeDict(project.Settings, write_to=(LaunchOverride, Hotswap))
    settings["SomeInnerClass"]["my_int_toggle"] = 2
    settings["my_dict"]["0"] = True
    entry = launch_override.get_override("cd_pkg.settings", ["Settings", "SomeInnerClass", "my_int_toggle"])
    assert entry == {"path": ["Settings", "SomeInnerClass", "my_int_toggle"], "value": "2", "default": "1"}
    assert not PendingSave.pending_saves or pending_text(project) == SOURCE
    launch_override.flush()

    # The next launch: a fresh import picks the override up through the hook.
    saved = launch_override._state["path"]
    del sys.modules["cd_pkg.settings"]
    launch_override._state.update(path=None, overrides={}, hooked=False)
    launch_override._state["path"] = None
    real = launch_override.overrides_path
    launch_override.overrides_path = lambda app_id: saved
    try:
        launch_override.install("any-app")
        fresh = importlib.import_module("cd_pkg.settings")
    finally:
        launch_override.overrides_path = real
        sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, launch_override._OverrideFinder)]
    assert fresh.Settings.SomeInnerClass.my_int_toggle == 2
    assert fresh.Settings.my_dict == {"0": True}

    # Writing the code's own value back removes the override.
    again = CodeDict(fresh.Settings, write_to=LaunchOverride)
    again["SomeInnerClass"]["my_int_toggle"] = 1
    assert launch_override.get_override("cd_pkg.settings", ["Settings", "SomeInnerClass", "my_int_toggle"]) is None


def test_a_value_without_a_source_form_reaches_no_sink(project):
    settings = CodeDict(project.Settings, write_to=(Hotswap, Codebase))
    with pytest.raises(TypeError):
        settings["speed"] = object()
    assert project.Settings.speed == 1.5 and settings["speed"] == 1.5


def test_list_and_delete(project):
    settings = CodeDict(project.Settings, write_to=(Codebase, Hotswap))
    assert isinstance(settings["my_list"], CodeList)
    held = settings["my_list"]
    held.append(3)
    assert settings["my_list"] is held and held == [1, 2, 3]
    del settings["speed"]
    assert "speed" not in settings and not hasattr(project.Settings, "speed")
    text = pending_text(project)
    assert "my_list = [1, 2, 3]" in text and "speed" not in text


def test_refresh_reports_a_runtime_change(project):
    settings = CodeDict(project.Settings)
    assert settings.refresh() is False
    project.Settings.speed = 2.5
    assert settings.refresh() is True
    assert settings["speed"] == 2.5


def test_a_class_without_editable_source_still_mirrors_and_hotswaps(project, monkeypatch):
    """An installed app's settings class: no source to parse, the live class
    is the dict, and LaunchOverride + Hotswap still work."""
    from meltygui.code.new_codecs import TypeCodec
    monkeypatch.setattr(TypeCodec, "resolve_address", staticmethod(lambda *args, **kwargs: None))
    settings = CodeDict(project.Settings, write_to=(LaunchOverride, Hotswap))
    assert settings["SomeInnerClass"]["my_int_toggle"] == 1 and settings["my_dict"] == {"0": False}
    settings["SomeInnerClass"]["my_int_toggle"] = 2
    assert project.Settings.SomeInnerClass.my_int_toggle == 2
    assert launch_override.get_override(
        "cd_pkg.settings", ["Settings", "SomeInnerClass", "my_int_toggle"])["value"] == "2"


def test_app_settings_leaves_a_code_dict_to_its_own_sinks(project, tmp_path):
    from meltygui.core.runtime.app_settings import AppSettings
    file = tmp_path / "settings.json"
    file.write_text('{"Window": {"speed": 9.0}}')
    values = CodeDict(project.Settings, write_to=(LaunchOverride, Hotswap))
    settings = AppSettings(values, "Window", file)
    assert values["speed"] == 1.5                          # the file is not merged into it
    values["speed"] = 2.25
    settings.save()
    assert file.read_text() == '{"Window": {"speed": 9.0}}'  # nor written from it
    assert "2.25" in launch_override._state["path"].read_text()


def test_a_view_reassigning_the_child_it_mutated_is_a_no_op(project):
    """draw_collection mutates a child in place, then its parent assigns the
    same object back (the render contract)."""
    settings = CodeDict(project.Settings, write_to=(LaunchOverride, Hotswap))
    inner, held_list = settings["SomeInnerClass"], settings["my_list"]
    inner["my_int_toggle"] = 2
    settings["SomeInnerClass"] = inner
    settings["my_list"] = held_list
    assert settings["SomeInnerClass"] is inner and settings["my_list"] is held_list
    assert list(launch_override._state["overrides"]["cd_pkg.settings"]) == [
        {"path": ["Settings", "SomeInnerClass", "my_int_toggle"], "default": "1", "value": "2"}]
    assert project.Settings.SomeInnerClass.my_int_toggle == 2


def test_an_override_the_code_itself_now_holds_is_dropped(project):
    settings = CodeDict(project.Settings, write_to=(LaunchOverride, Hotswap, Codebase))
    settings["SomeInnerClass"]["my_int_toggle"] = 2
    assert pending_text(project) == SOURCE.replace("my_int_toggle = 1", "my_int_toggle = 2")
    assert launch_override._state["overrides"] == {}         # the source says 2: nothing to override
