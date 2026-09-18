"""CodeDict under pressure: several users of one definition, the editor's
code-host pipeline on the same span, external disk writes, other hotswaps,
inserted and deleted code, the flush to disk, value shapes and the launch
override file. The basics live in test_code_dict.py."""
import enum
import gc
import importlib
import json
import os
import sys
import textwrap
import time
import types

import pytest

from meltygui.code.file_converters import _recompile_class
from meltygui.code.file_converters import _recompile_module
from meltygui.code.file_converters import stamp_module_baseline
from meltygui.code.fileref import _EDITABLE_ROOTS
from meltygui.code.fileref import _EDITABLE_SOURCE_CACHE
from meltygui.code.fileref import add_editable_root
from meltygui.code.new_codecs import TypeCodec
from meltygui.code.new_converters import load_file
from meltygui.code.new_converters import save_file
from meltygui.core.melty import Melty
from meltygui.core.runtime import launch_override
from meltygui.editor.pending_save import PendingSave
from meltygui.model import code_dict_model
from meltygui.model.code_dict_model import CodeDict
from meltygui.model.code_dict_model import CodeList
from meltygui.model.code_dict_model import Codebase
from meltygui.model.code_dict_model import Hotswap
from meltygui.model.code_dict_model import LaunchOverride

SOURCE = textwrap.dedent('''\
    """Module docstring stays."""
    import enum

    LIMIT = 10
    NAMES = ["a", "b"]


    class Mode(enum.Enum):
        FAST = 1
        SLOW = 2


    # Settings: the comment above the class.
    class Settings:
        """Class docstring stays."""
        my_dict = {"0": False}
        my_list = [1, 2]
        # keep me
        speed = 1.5   # trailing stays
        name = 'single "quoted"'
        color = (0.1, 0.2, 0.3)
        mode = Mode.FAST
        nothing = None

        class SomeInnerClass:
            my_int_toggle = 1

            class Deeper:
                depth = 3

        def method(self, scale=2):
            return scale

        @staticmethod
        def helper(amount=7, *, flag=False):
            return amount, flag


    class Other:
        value = 100


    def work(a=3):
        step = 5
        return a * step
''')


@pytest.fixture
def project(tmp_path, monkeypatch):
    package = tmp_path / "cds_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("PACKAGE_FLAG = 1\n")
    (package / "settings.py").write_text(SOURCE)
    roots = list(_EDITABLE_ROOTS)
    add_editable_root(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(launch_override, "_state",
                        dict(path=tmp_path / "launch_overrides.json", overrides={}, dirty=False, hooked=True))
    if Melty.cache is None:
        monkeypatch.setattr(Melty, "cache", StubCache())
    module = importlib.import_module("cds_pkg.settings")
    yield module
    for name in [name for name in sys.modules if name.startswith("cds_pkg")]:
        del sys.modules[name]
    for key in [key for key in code_dict_model._cores if key[0].startswith("cds_pkg")]:
        del code_dict_model._cores[key]
    for address in [a for a in PendingSave.pending_saves if tmp_path in a.path.parents]:
        PendingSave.pending_saves.pop(address, None)
        PendingSave.originals.pop(address, None)
    sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, launch_override._OverrideFinder)]
    _EDITABLE_ROOTS[:] = roots
    _EDITABLE_SOURCE_CACHE.clear()


def pending_text(module):
    return PendingSave.current_file_text(module.__file__)


def disk_text(module):
    with open(module.__file__, encoding="utf-8") as stream:
        return stream.read()


def write_disk(module, text):
    """An external program rewrites the file (mtime guaranteed to move)."""
    before = os.stat(module.__file__).st_mtime_ns
    with open(module.__file__, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.utime(module.__file__, ns=(before + 5_000_000_000, before + 5_000_000_000))


def built_class(module, name="Settings"):
    """The class the pending text would define."""
    namespace = {}
    exec(compile(pending_text(module), module.__file__, "exec"), namespace)
    return namespace[name]


def changed_lines(before, after):
    """(removed, added) line lists between two texts, order kept."""
    import difflib
    removed, added = [], []
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0, lineterm=""):
        if line.startswith(("---", "+++", "@@")):
            continue
        (added if line.startswith("+") else removed).append(line[1:])
    return removed, added


class FakeDrawState:
    """What the codecs and save_file need from the editor's draw_state."""

    def __init__(self):
        self.name = "editor host"
        self._addr_cache = None
        self._parent = None
        self.parent_window = None


class StubCache:
    """Melty.cache for a test without a window: no-op invalidations, an empty
    draw_state table (the same stand-in test_hotswap_preserves_runtime_state uses)."""
    key_to_draw_state = {}

    def __getattr__(self, name):
        if name.startswith("invalidate"):
            return lambda *args, **kwargs: None
        raise AttributeError(name)


# ── several users of one definition ───────────────────────────────────────────

def test_many_handles_every_sink_combination_see_every_write(project):
    sink_sets = [(), Codebase, Hotswap, LaunchOverride, (Codebase, Hotswap),
                 (LaunchOverride, Hotswap), (Codebase, LaunchOverride, Hotswap)]
    handles = [CodeDict(project.Settings, write_to=sinks) for sinks in sink_sets]
    assert len({id(handle) for handle in handles}) == len(handles)
    for index, handle in enumerate(handles):
        handle["SomeInnerClass"]["my_int_toggle"] = 10 + index
        assert [other["SomeInnerClass"]["my_int_toggle"] for other in handles] == [10 + index] * len(handles)


def test_a_handle_without_sinks_changes_nothing_but_the_dicts(project):
    viewer = CodeDict(project.Settings)
    viewer["speed"] = 8.0
    assert viewer["speed"] == 8.0
    assert project.Settings.speed == 1.5
    assert not [a for a in PendingSave.pending_saves if a.path.name == "settings.py"]
    assert launch_override._state["overrides"] == {}


def test_each_write_reaches_only_the_writing_handles_sinks(project):
    user = CodeDict(project.Settings, write_to=(LaunchOverride, Hotswap))
    developer = CodeDict(project.Settings, write_to=Codebase)
    user["speed"] = 2.25
    assert pending_text(project) == SOURCE                  # the user's slider never edits code
    assert project.Settings.speed == 2.25
    developer["nothing"] = 4
    assert project.Settings.nothing is None                 # the developer's edit never hotswaps
    assert "nothing = 4" in pending_text(project)
    assert launch_override.get_override("cds_pkg.settings", ["Settings", "nothing"]) is None
    assert launch_override.get_override("cds_pkg.settings", ["Settings", "speed"])["value"] == "2.25"


def test_child_handles_are_stable_objects_per_handle(project):
    first = CodeDict(project.Settings, write_to=Hotswap)
    second = CodeDict(project.Settings, write_to=Hotswap)
    inner, held_dict, held_list = first["SomeInnerClass"], first["my_dict"], first["my_list"]
    assert second["SomeInnerClass"] is not inner            # a child belongs to ONE handle (its sinks)
    second["SomeInnerClass"]["my_int_toggle"] = 5
    second["my_dict"]["0"] = True
    second["my_list"].append(3)
    second["my_dict"] = {"0": False, "1": True}             # whole-value replace
    second["my_list"] = [9]
    assert first["SomeInnerClass"] is inner and inner["my_int_toggle"] == 5
    assert first["my_dict"] is held_dict and held_dict == {"0": False, "1": True}
    assert first["my_list"] is held_list and held_list == [9]
    assert first.refresh() is False                          # nothing left to catch up on
    assert first["SomeInnerClass"] is inner and first["my_dict"] is held_dict


def test_nested_handle_roots_link_with_paths_through_the_outer_class(project):
    deeper = CodeDict(project.Settings.SomeInnerClass.Deeper, write_to=(Codebase, Hotswap))
    inner = CodeDict(project.Settings.SomeInnerClass)
    outer = CodeDict(project.Settings)
    through_module = CodeDict(project)["Settings"]
    deeper["depth"] = 4
    assert inner["Deeper"]["depth"] == outer["SomeInnerClass"]["Deeper"]["depth"] == 4
    assert through_module["SomeInnerClass"]["Deeper"]["depth"] == 4
    assert project.Settings.SomeInnerClass.Deeper.depth == 4
    assert changed_lines(SOURCE, pending_text(project)) == (["            depth = 3"], ["            depth = 4"])
    assert len([a for a in PendingSave.pending_saves if a.path.name == "settings.py"]) == 1


def test_dropped_handles_are_forgotten(project):
    keeper = CodeDict(project.Settings, write_to=Hotswap)
    core = keeper._core
    for _ in range(50):
        CodeDict(project.Settings)["SomeInnerClass"]
    gc.collect()
    keeper["speed"] = 2.25
    keeper["SomeInnerClass"]["my_int_toggle"] = 2
    assert len(core.handles_at(())) == 1
    assert len(core.handles_at(("SomeInnerClass",))) == 1


def test_two_classes_in_one_file_queue_two_spans_that_compose(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    other = CodeDict(project.Other, write_to=Codebase)
    work = CodeDict(project.work, write_to=Codebase)
    settings["speed"] = 2.25
    other["value"] = 200
    work["parameters"]["a"] = 30
    assert changed_lines(SOURCE, pending_text(project)) == (
        ["    speed = 1.5   # trailing stays", "    value = 100", "def work(a=3):"],
        ["    speed = 2.25   # trailing stays", "    value = 200", "def work(a=30):"])
    assert len([a for a in PendingSave.pending_saves if a.path.name == "settings.py"]) == 3


# ── the editor's code-host pipeline on the same span ──────────────────────────

def editor_open(cls):
    """What code_file_io does when the editor opens a class: resolve, load."""
    draw_state = FakeDrawState()
    address = TypeCodec.resolve_address(cls, draw_state)
    return draw_state, address, load_file(address, codec=TypeCodec)


def test_editor_and_code_dict_share_one_pending_entry(project):
    draw_state, address, text = editor_open(project.Settings)
    settings = CodeDict(project.Settings, write_to=Codebase)
    assert settings._core.address == address                # the same (path, start, end) key
    settings["speed"] = 2.25
    save_file(address, str(text).replace("my_int_toggle = 1", "my_int_toggle = 6"), codec=TypeCodec,
              parent_ds=draw_state)
    assert len([a for a in PendingSave.pending_saves if a.path.name == "settings.py"]) == 1


def test_editor_reload_sees_the_code_dict_edit(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    settings["speed"] = 2.25
    _, _, text = editor_open(project.Settings)               # opens AFTER the CodeDict write
    assert "speed = 2.25" in str(text)


def test_code_dict_write_after_an_editor_save_keeps_the_editors_edit(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    draw_state, address, text = editor_open(project.Settings)
    edited = str(text).replace("my_int_toggle = 1", "my_int_toggle = 6").replace(
"    # keep me\n", "    # keep me\n    added_by_editor = 'x'\n")
    save_file(address, edited, codec=TypeCodec, parent_ds=draw_state)
    settings["speed"] = 2.25                                  # its parse predates the editor's save
    final = pending_text(project)
    assert "my_int_toggle = 6" in final and "added_by_editor = 'x'" in final and "speed = 2.25" in final
    assert settings["added_by_editor"] == "x"                # and the handle caught up in place


def test_editor_save_after_a_code_dict_write_wins_on_the_same_key(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    settings["speed"] = 2.25
    draw_state, address, text = editor_open(project.Settings)
    save_file(address, str(text).replace("speed = 2.25", "speed = 3.25"), codec=TypeCodec, parent_ds=draw_state)
    assert "speed = 3.25" in pending_text(project)
    assert settings.refresh() is True
    assert settings._core.source_value(("speed",)) == 3.25
    assert settings["speed"] == 1.5                          # effective: nobody hotswapped


def test_the_editors_run_hotswaps_what_code_dict_queued(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    viewer = CodeDict(project.Settings)
    live_class = project.Settings
    settings["speed"] = 2.25
    settings["SomeInnerClass"]["my_int_toggle"] = 4
    PendingSave.recompile_all()                              # Ctrl+Enter in the editor
    assert project.Settings is live_class
    assert live_class.speed == 2.25 and live_class.SomeInnerClass.my_int_toggle == 4
    viewer.refresh()
    assert viewer["speed"] == 2.25
    assert disk_text(project) == SOURCE                      # still nothing on disk


# ── the flush ─────────────────────────────────────────────────────────────────

def test_flush_writes_exactly_the_edits_and_a_fresh_import_agrees(project):
    CodeDict(project.Settings, write_to=Codebase)["speed"] = 2.25
    CodeDict(project.Other, write_to=Codebase)["value"] = 200
    CodeDict(project.work, write_to=Codebase)["locals"]["step"] = 6
    expected = pending_text(project)
    PendingSave.apply_all_saves()
    assert disk_text(project) == expected
    assert changed_lines(SOURCE, expected) == (
        ["    speed = 1.5   # trailing stays", "    value = 100", "    step = 5"],
        ["    speed = 2.25   # trailing stays", "    value = 200", "    step = 6"])
    del sys.modules["cds_pkg.settings"]
    fresh = importlib.import_module("cds_pkg.settings")
    assert fresh.Settings.speed == 2.25 and fresh.Other.value == 200 and fresh.work() == 18


def test_writes_keep_working_after_a_flush_that_moved_lines(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    other = CodeDict(project.Other, write_to=Codebase)
    settings["inserted_a"] = 1
    settings["inserted_b"] = 2                               # Other now sits two lines lower
    PendingSave.apply_all_saves()
    other["value"] = 300
    settings["speed"] = 2.75
    PendingSave.apply_all_saves()
    text = disk_text(project)
    compile(text, project.__file__, "exec")
    assert "value = 300" in text and "speed = 2.75" in text and "inserted_b = 2" in text
    assert text.count("class Other:") == 1 and text.count("value = ") == 1


# ── external disk writes ──────────────────────────────────────────────────────

def test_external_edit_shows_after_refresh(project):
    settings = CodeDict(project.Settings)
    write_disk(project, SOURCE.replace("    nothing = None\n", "    nothing = None\n    external = 42\n"))
    assert "external" not in settings                        # reads never poll
    assert settings.refresh() is True
    assert settings["external"] == 42
    assert settings.refresh() is False


def test_write_after_an_external_edit_keeps_the_external_edit(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    write_disk(project, SOURCE.replace("my_int_toggle = 1", "my_int_toggle = 77"))
    settings["speed"] = 2.25
    final = pending_text(project)
    assert "my_int_toggle = 77" in final and "speed = 2.25" in final


def test_external_lines_inserted_above_the_definition(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    work = CodeDict(project.work, write_to=Codebase)
    shifted = SOURCE.replace("LIMIT = 10\n", "LIMIT = 10\nEXTRA_ONE = 1\nEXTRA_TWO = 2\nEXTRA_THREE = 3\n")
    write_disk(project, shifted)
    settings["speed"] = 2.25
    work["parameters"]["a"] = 4
    final = pending_text(project)
    compile(final, project.__file__, "exec")
    assert changed_lines(shifted, final) == (
        ["    speed = 1.5   # trailing stays", "def work(a=3):"],
        ["    speed = 2.25   # trailing stays", "def work(a=4):"])


# A block of new code an external program (git pull, another editor, an agent)
# puts in front of the definitions: every span below it moves by its length.
BIG_BLOCK = "\n".join(
    [f"class Generated{index}:\n    \"\"\"Generated filler.\"\"\"\n    value = {index}\n\n    def get(self):\n"
     f"        return self.value\n\n" for index in range(12)]
    + ["def generated_function(a=1, b=2):\n    total = a + b\n    return total\n\n\n"])


def with_block_above(text):
    return text.replace("# Settings: the comment above the class.\n",
                        BIG_BLOCK + "# Settings: the comment above the class.\n")


def write_everywhere(project):
    """One write in every kind of place, through fresh or held handles."""
    CodeDict(project.Settings, write_to=(Codebase, Hotswap))["speed"] = 2.25
    CodeDict(project.Settings.SomeInnerClass.Deeper, write_to=Codebase)["depth"] = 4
    CodeDict(project.Settings.helper, write_to=Codebase)["parameters"]["amount"] = 8
    CodeDict(project.Other, write_to=Codebase)["value"] = 200
    CodeDict(project.work, write_to=Codebase)["parameters"]["a"] = 4


EVERYWHERE = (["    speed = 1.5   # trailing stays", "            depth = 3",
               "    def helper(amount=7, *, flag=False):", "    value = 100", "def work(a=3):"],
              ["    speed = 2.25   # trailing stays", "            depth = 4",
               "    def helper(amount=8, *, flag=False):", "    value = 200", "def work(a=4):"])


def test_a_big_block_of_code_placed_before_the_definitions(project):
    held = CodeDict(project.Settings, write_to=Codebase)      # parsed at the OLD position
    shifted = with_block_above(SOURCE)
    assert shifted.count("\n") - SOURCE.count("\n") > 80
    write_disk(project, shifted)
    write_everywhere(project)
    held["my_dict"]["0"] = True
    final = pending_text(project)
    compile(final, project.__file__, "exec")
    removed, added = changed_lines(shifted, final)
    assert removed == ['    my_dict = {"0": False}'] + EVERYWHERE[0]
    assert added == ['    my_dict = {"0": True}'] + EVERYWHERE[1]
    PendingSave.apply_all_saves()
    assert disk_text(project) == final                       # and the flush lands on the new lines


def test_code_removed_above_the_definitions(project):
    write_disk(project, with_block_above(SOURCE))
    del sys.modules["cds_pkg.settings"]
    module = importlib.import_module("cds_pkg.settings")      # the app started with the block there
    held = CodeDict(module.Settings, write_to=Codebase)
    write_disk(module, SOURCE)                               # ... and it is removed under us
    write_everywhere(module)
    held["my_dict"]["0"] = True
    final = pending_text(module)
    compile(final, module.__file__, "exec")
    assert changed_lines(SOURCE, final)[1] == ['    my_dict = {"0": True}'] + EVERYWHERE[1]
    PendingSave.apply_all_saves()
    assert disk_text(module) == final


def test_a_shift_while_edits_are_already_pending(project):
    write_everywhere(project)                                # queued against the old line numbers
    shifted = with_block_above(SOURCE)
    write_disk(project, shifted)
    CodeDict(project.Settings, write_to=Codebase)["nothing"] = 1
    CodeDict(project.Other, write_to=Codebase)["extra"] = 2
    CodeDict(project.work, write_to=Codebase)["locals"]["step"] = 6
    final = pending_text(project)
    compile(final, project.__file__, "exec")
    removed, added = changed_lines(shifted, final)
    assert removed == EVERYWHERE[0][:1] + ["    nothing = None"] + EVERYWHERE[0][1:] + ["    step = 5"]
    assert [line for line in added if line.strip()] == (
        EVERYWHERE[1][:1] + ["    nothing = 1"] + EVERYWHERE[1][1:4] + ["    extra = 2", "def work(a=4):", "    step = 6"])
    assert len([a for a in PendingSave.pending_saves if a.path.name == "settings.py"]) == 3
    PendingSave.apply_all_saves()
    assert disk_text(project) == final
    namespace = {}
    exec(compile(final, project.__file__, "exec"), namespace)
    assert namespace["Generated11"]().get() == 11 and namespace["work"]() == 24


def test_repeated_shifts_between_writes(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    text = SOURCE
    for round_number in range(4):
        text = text.replace("LIMIT = 10\n", f"LIMIT = 10\nROUND_{round_number} = {round_number}\n" * 3, 1)
        write_disk(project, text)
        settings["SomeInnerClass"]["my_int_toggle"] = 10 + round_number
        settings[f"added_{round_number}"] = round_number
    final = pending_text(project)
    built = built_class(project)
    assert built.SomeInnerClass.my_int_toggle == 13
    assert [getattr(built, f"added_{n}") for n in range(4)] == [0, 1, 2, 3]
    assert final.count("ROUND_3 = 3") == 3 and final.count("class Settings:") == 1
    PendingSave.apply_all_saves()
    assert disk_text(project) == final


def test_an_external_edit_inside_the_span_merges_with_the_pending_edit(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    settings["speed"] = 2.25
    write_disk(project, SOURCE.replace("my_int_toggle = 1", "my_int_toggle = 77").replace(
        "    nothing = None\n", "    nothing = None\n    external = 42\n"))
    settings["color"] = (1, 2, 3)
    built = built_class(project)
    assert built.speed == 2.25 and built.color == (1, 2, 3)   # ours, before and after the external write
    assert built.SomeInnerClass.my_int_toggle == 77 and built.external == 42
    assert settings["external"] == 42


def test_an_external_edit_of_the_same_line_loses_to_the_pending_key(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    settings["speed"] = 2.25
    settings["nothing"] = 5
    write_disk(project, SOURCE.replace("speed = 1.5   # trailing stays", "speed = 9.75   # they changed it too")
               .replace("my_int_toggle = 1", "my_int_toggle = 77"))
    settings["my_dict"]["0"] = True                          # the text merge conflicts on the speed line
    built = built_class(project)
    assert built.speed == 2.25 and built.nothing == 5 and built.my_dict == {"0": True}
    assert built.SomeInnerClass.my_int_toggle == 77           # their non-conflicting edit stays
    assert "# they changed it too" in pending_text(project)


def test_external_removal_of_a_key_a_handle_still_shows(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    write_disk(project, SOURCE.replace("    nothing = None\n", ""))
    settings["speed"] = 2.25
    final = pending_text(project)
    assert "nothing" not in final.split("class Other")[0]
    assert "speed = 2.25" in final


def test_external_edit_that_breaks_the_file_does_not_corrupt_anything(project):
    settings = CodeDict(project.Settings, write_to=(Codebase, Hotswap))
    broken = SOURCE.replace("class Settings:", "class Settings(:")
    write_disk(project, broken)
    with pytest.raises(Exception):
        settings["speed"] = 2.25
    assert disk_text(project) == broken                      # never written over
    assert project.Settings.speed == 1.5                     # no sink ran
    write_disk(project, SOURCE)                              # the user fixes the file
    settings["speed"] = 2.25
    assert "speed = 2.25" in pending_text(project) and project.Settings.speed == 2.25


# ── other hotswaps ────────────────────────────────────────────────────────────

def test_class_recompile_keeps_identity_and_handles_follow(project):
    settings = CodeDict(project.Settings, write_to=(Codebase, Hotswap))
    live_class, inner = project.Settings, settings["SomeInnerClass"]
    class_source = SOURCE[SOURCE.index("class Settings:"):SOURCE.index("class Other:")]
    _recompile_class(live_class, class_source.replace("speed = 1.5", "speed = 6.5"), project.__file__)
    assert project.Settings is live_class and live_class.speed == 6.5
    assert settings.refresh() is True
    assert settings["speed"] == 6.5 and settings["SomeInnerClass"] is inner
    settings["SomeInnerClass"]["my_int_toggle"] = 2           # same core, still writable
    assert live_class.SomeInnerClass.my_int_toggle == 2
    assert code_dict_model._core_for(live_class)[0] is settings._core


def test_module_recompile_keeps_a_launch_override_value_alive(project):
    stamp_module_baseline(project, SOURCE)
    user = CodeDict(project.Settings, write_to=(LaunchOverride, Hotswap))
    user["speed"] = 2.25
    _recompile_module(project, SOURCE.replace("value = 100", "value = 101"), project.__file__)
    assert project.Other.value == 101                        # the source edit applied
    assert project.Settings.speed == 2.25                     # the user's live value survived
    assert user.refresh() is False or user["speed"] == 2.0


def test_runtime_mutation_by_other_code_shows_after_refresh(project):
    settings = CodeDict(project.Settings)
    held = settings["my_dict"]
    project.Settings.my_dict["added"] = 1
    project.Settings.SomeInnerClass.my_int_toggle = 9
    project.Settings.my_list.append(3)
    assert settings.refresh() is True
    assert settings["my_dict"] is held and held == {"0": False, "added": 1}
    assert settings["SomeInnerClass"]["my_int_toggle"] == 9
    assert settings["my_list"] == [1, 2, 3]


def test_a_rebound_top_level_name_gets_a_fresh_core(project):
    old = CodeDict(project.Settings)
    project.Settings = type("Settings", (), {"__module__": project.__name__, "speed": 0.5})
    new = CodeDict(project.Settings)
    assert new._core is not old._core
    assert new["speed"] == 0.5


# ── mutations that insert code ────────────────────────────────────────────────

def test_insert_new_attributes_of_every_shape(project):
    settings = CodeDict(project.Settings, write_to=(Codebase, Hotswap))
    settings["new_int"] = 5
    settings["new_text"] = "it's \"both\""
    settings["new_tuple"] = (1, (2, 3))
    settings["new_dict"] = {"a": {"b": [1, 2]}}
    settings["new_enum"] = project.Mode.SLOW
    settings["SomeInnerClass"]["inner_new"] = False
    settings["SomeInnerClass"]["Deeper"]["deeper_new"] = -2.5
    final = pending_text(project)
    namespace = {}
    exec(compile(final, project.__file__, "exec"), namespace)
    built = namespace["Settings"]
    assert built.new_int == 5 and built.new_text == "it's \"both\"" and built.new_tuple == (1, (2, 3))
    assert built.new_dict == {"a": {"b": [1, 2]}} and built.new_enum is namespace["Mode"].SLOW
    assert built.SomeInnerClass.inner_new is False and built.SomeInnerClass.Deeper.deeper_new == -2.5
    assert built.speed == 1.5 and built().method() == 2       # the rest of the class is intact
    assert project.Settings.new_dict == {"a": {"b": [1, 2]}}
    assert isinstance(settings["new_dict"], CodeDict) and isinstance(settings["new_dict"]["a"]["b"], CodeList)
    removed, added = changed_lines(SOURCE, final)
    assert removed == [] and len([line for line in added if line.strip()]) == 7   # seven statements, nothing else touched


def test_an_inserted_dict_is_itself_writable(project):
    settings = CodeDict(project.Settings, write_to=(Codebase, Hotswap))
    settings["new_dict"] = {"a": {"b": [1, 2]}}
    settings["new_dict"]["a"]["b"].append(3)
    settings["new_dict"]["a"]["c"] = True
    settings["new_dict"]["z"] = None
    assert project.Settings.new_dict == {"a": {"b": [1, 2, 3], "c": True}, "z": None}
    assert built_class(project).new_dict == {"a": {"b": [1, 2, 3], "c": True}, "z": None}


def test_insert_into_existing_dict_and_list_values(project):
    settings = CodeDict(project.Settings, write_to=(Codebase, Hotswap))
    live_dict, live_list = project.Settings.my_dict, project.Settings.my_list
    settings["my_dict"]["1"] = True
    settings["my_dict"].update({"2": None}, three=3)
    settings["my_dict"].setdefault("0", "unused")
    settings["my_dict"].setdefault("4", 4)
    settings["my_dict"] |= {"5": 5}
    settings["my_list"].insert(0, 0)
    settings["my_list"].extend([3, 4])
    settings["my_list"] += [5]
    settings["my_list"][1] = 100
    assert live_dict is project.Settings.my_dict and live_list is project.Settings.my_list
    assert live_dict == {"0": False, "1": True, "2": None, "three": 3, "4": 4, "5": 5}
    assert live_list == [0, 100, 2, 3, 4, 5]
    built = built_class(project)
    assert built.my_dict == live_dict and built.my_list == live_list
    assert "my_list = [0, 100, 2, 3, 4, 5]" in pending_text(project)


def test_insert_at_module_level(project):
    module = CodeDict(project, write_to=(Codebase, Hotswap))
    module["NEW_GLOBAL"] = [1, 2]
    module["NAMES"].append("c")
    assert project.NEW_GLOBAL == [1, 2] and project.NAMES == ["a", "b", "c"]
    final = pending_text(project)
    namespace = {}
    exec(compile(final, project.__file__, "exec"), namespace)
    assert namespace["NEW_GLOBAL"] == [1, 2] and namespace["NAMES"] == ["a", "b", "c"]
    assert namespace["Settings"].speed == 1.5


# ── mutations that delete code ────────────────────────────────────────────────

def test_delete_attributes_and_their_comments(project):
    settings = CodeDict(project.Settings, write_to=(Codebase, Hotswap))
    viewer = CodeDict(project.Settings)
    del settings["speed"]
    assert settings.pop("nothing") is None
    assert settings.pop("absent", "fallback") == "fallback"
    with pytest.raises(KeyError):
        del settings["absent"]
    with pytest.raises(KeyError):
        settings.pop("absent")
    assert "speed" not in viewer and "nothing" not in viewer
    assert not hasattr(project.Settings, "speed") and not hasattr(project.Settings, "nothing")
    final = pending_text(project)
    removed, added = changed_lines(SOURCE, final)
    assert added == []
    assert "    speed = 1.5   # trailing stays" in removed and "    nothing = None" in removed
    compile(final, project.__file__, "exec")


def test_delete_inside_dict_and_list_values(project):
    settings = CodeDict(project.Settings, write_to=(Codebase, Hotswap))
    settings["my_dict"]["1"] = True
    del settings["my_dict"]["0"]
    settings["my_list"].remove(1)
    assert settings["my_list"].pop() == 2
    assert project.Settings.my_dict == {"1": True} and project.Settings.my_list == []
    built = built_class(project)
    assert built.my_dict == {"1": True} and built.my_list == []
    settings["my_dict"].clear()
    assert project.Settings.my_dict == {} and "my_dict = {}" in pending_text(project)


def test_delete_a_nested_class_and_a_method(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    del settings["SomeInnerClass"]
    del settings["method"]
    final = pending_text(project)
    namespace = {}
    exec(compile(final, project.__file__, "exec"), namespace)
    built = namespace["Settings"]
    assert not hasattr(built, "SomeInnerClass") and not hasattr(built, "method")
    assert built.helper() == (7, False) and built.speed == 1.5
    assert "SomeInnerClass" not in settings


def test_delete_then_reinsert_the_same_key(project):
    settings = CodeDict(project.Settings, write_to=(Codebase, Hotswap))
    del settings["speed"]
    settings["speed"] = 9.25
    assert project.Settings.speed == 9.25
    final = pending_text(project)
    assert final.count("speed = ") == 1 and "speed = 9.25" in final
    compile(final, project.__file__, "exec")


def test_a_delete_that_would_leave_invalid_source_is_refused_everywhere(project):
    """core_syntax has no `pass` to put in an emptied class body: the write
    is refused before any sink ran, and the handles roll back."""
    other = CodeDict(project.Other, write_to=(Hotswap, Codebase, LaunchOverride))
    viewer = CodeDict(project.Other)
    with pytest.raises(Exception):
        del other["value"]
    assert project.Other.value == 100
    assert other["value"] == 100 and viewer["value"] == 100
    assert pending_text(project) == SOURCE
    other["value"] = 101                                     # and the core still works
    assert "value = 101" in pending_text(project) and project.Other.value == 101


# ── functions ─────────────────────────────────────────────────────────────────

def test_methods_and_staticmethods(project):
    method = CodeDict(project.Settings.method, write_to=(Codebase, Hotswap))
    helper = CodeDict(project.Settings.helper, write_to=(Codebase, Hotswap))
    through_class = CodeDict(project.Settings)
    method["parameters"]["scale"] = 5
    helper["parameters"]["amount"] = 8
    helper["parameters"]["flag"] = True
    assert project.Settings().method() == 5
    assert project.Settings.helper() == (8, True)
    assert through_class["method"]["parameters"]["scale"] == 5
    final = pending_text(project)
    assert "def method(self, scale=5):" in final and "def helper(amount=8, *, flag=True):" in final


def test_function_write_keeps_the_function_object(project):
    live = project.work
    CodeDict(project.work, write_to=Hotswap)["parameters"]["a"] = 10
    assert project.work is live and live() == 50


# ── value shapes and text fidelity ────────────────────────────────────────────

def test_untouched_source_is_byte_identical(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    settings["speed"] = 1.5                                  # same value
    settings["my_dict"]["0"] = False
    assert pending_text(project) == SOURCE


def test_value_styles_follow_the_source(project):
    settings = CodeDict(project.Settings, write_to=Codebase)
    settings["name"] = "plain"
    settings["color"] = (0.5, 0.25, 1.0)
    settings["mode"] = project.Mode.SLOW
    settings["nothing"] = "now a string"
    settings["speed"] = 3
    removed, added = changed_lines(SOURCE, pending_text(project))
    assert removed == ["    speed = 1.5   # trailing stays", "    name = 'single \"quoted\"'",
                       "    color = (0.1, 0.2, 0.3)", "    mode = Mode.FAST", "    nothing = None"]
    assert added[0] == "    speed = 3   # trailing stays"    # the trailing comment survives
    assert added[1] == "    name = 'plain'"                   # the string keeps its quote style
    assert added[3] == "    mode = Mode.SLOW"
    built = built_class(project)
    assert built.color == (0.5, 0.25, 1.0) and built.nothing == "now a string"


def test_enum_values_read_as_live_members(project):
    settings = CodeDict(project.Settings)
    assert settings["mode"] is project.Mode.FAST


@pytest.mark.parametrize("value", [object(), lambda: 1, {1, object()}, [1, object()], {"k": object()}])
def test_unwritable_values_touch_nothing(project, value):
    settings = CodeDict(project.Settings, write_to=(Hotswap, LaunchOverride, Codebase))
    with pytest.raises(TypeError):
        settings["speed"] = value
    assert project.Settings.speed == 1.5 and settings["speed"] == 1.5
    assert pending_text(project) == SOURCE and launch_override._state["overrides"] == {}


def test_assigning_a_handle_copies_its_content(project):
    settings = CodeDict(project.Settings, write_to=(Codebase, Hotswap))
    settings["copy_of_dict"] = settings["my_dict"]
    settings["copy_of_dict"]["0"] = True
    assert project.Settings.my_dict == {"0": False}          # not aliased
    assert project.Settings.copy_of_dict == {"0": True}
    assert type(project.Settings.copy_of_dict) is dict


def test_dict_protocol(project):
    settings = CodeDict(project.Settings)
    assert settings["my_dict"] == {"0": False} and {"0": False} == settings["my_dict"]
    assert list(settings["SomeInnerClass"]) == ["my_int_toggle", "Deeper"]
    assert "speed" in settings and settings.get("absent", 1) == 1
    assert len(settings["my_dict"]) == 1
    assert json.dumps(settings["my_dict"]) == '{"0": false}'
    assert [key for key in settings if not key.startswith(("#", '"'))][:3] == ["my_dict", "my_list", "speed"]
    with pytest.raises(TypeError):
        hash(settings)


# ── what cannot be mirrored ───────────────────────────────────────────────────

def test_library_source_is_read_only(project):
    decoder = CodeDict(json.JSONDecoder, write_to=Codebase)
    with pytest.raises(TypeError):
        decoder["anything"] = 1


def test_a_class_defined_inside_a_function_is_refused(project):
    def factory():
        class Local:
            x = 1
        return Local
    with pytest.raises(TypeError):
        CodeDict(factory())


def test_a_package_listing_is_not_assignable(project):
    package = CodeDict(sys.modules["cds_pkg"], write_to=Codebase)
    with pytest.raises(TypeError):
        package["settings"] = 1
    assert package["__init__"]["PACKAGE_FLAG"] == 1
    package["__init__"]["PACKAGE_FLAG"] = 2
    assert "PACKAGE_FLAG = 2" in PendingSave.current_file_text(sys.modules["cds_pkg"].__file__)


def test_a_package_does_not_import_until_read(project, tmp_path):
    (tmp_path / "cds_pkg" / "lazy_one.py").write_text("VALUE = 1\n")
    package = CodeDict(sys.modules["cds_pkg"])
    assert "lazy_one" in dict.keys(package) and "cds_pkg.lazy_one" not in sys.modules
    assert package["lazy_one"]["VALUE"] == 1
    assert "cds_pkg.lazy_one" in sys.modules


# ── the launch override file ──────────────────────────────────────────────────

def relaunch(tmp_path, monkeypatch, module_name="cds_pkg.settings"):
    """Forget the module and run the boot-time install against the saved file."""
    saved = launch_override._state["path"]
    sys.modules.pop(module_name, None)
    launch_override._state.update(path=None, overrides={}, dirty=False, hooked=False)
    monkeypatch.setattr(launch_override, "overrides_path", lambda app_id: saved)
    monkeypatch.setattr(launch_override.atexit, "register", lambda function: None)
    launch_override.install("test-app")
    return importlib.import_module(module_name)


def test_overrides_of_every_shape_survive_a_relaunch(project, tmp_path, monkeypatch):
    user = CodeDict(project.Settings, write_to=LaunchOverride)
    user["speed"] = 2.25
    user["mode"] = project.Mode.SLOW
    user["color"] = (1.0, 0.0, 0.0)
    user["my_dict"]["0"] = True
    user["my_list"] = [7, 8]
    user["SomeInnerClass"]["Deeper"]["depth"] = 30
    CodeDict(project, write_to=LaunchOverride)["LIMIT"] = 99
    assert project.Settings.speed == 1.5                     # no Hotswap sink: this launch is untouched
    launch_override.flush()
    assert disk_text(project) == SOURCE
    fresh = relaunch(tmp_path, monkeypatch)
    assert fresh.Settings.speed == 2.25 and fresh.Settings.mode is fresh.Mode.SLOW
    assert fresh.Settings.color == (1.0, 0.0, 0.0) and fresh.Settings.my_dict == {"0": True}
    assert fresh.Settings.my_list == [7, 8] and fresh.Settings.SomeInnerClass.Deeper.depth == 30
    assert fresh.LIMIT == 99
    assert CodeDict(fresh.Settings)["speed"] == 2.25          # the effective value next launch


def test_a_bad_override_never_stops_the_launch(project, tmp_path, monkeypatch, capsys):
    path = launch_override._state["path"]
    path.write_text(json.dumps({"version": 1, "overrides": {"cds_pkg.settings": [
        {"path": ["Settings", "Gone", "x"], "value": "1"},
        {"path": ["Settings", "speed"], "value": "not python ((("},
        {"path": ["Settings", "speed"]},
        "not an entry",
        {"path": ["Settings", "name"], "value": "'good'"}]}}))
    fresh = relaunch(tmp_path, monkeypatch)
    assert fresh.Settings.name == "good" and fresh.Settings.speed == 1.5
    assert capsys.readouterr().err.count("launch override") == 2


def test_an_unreadable_override_file_is_moved_aside(project, tmp_path, monkeypatch):
    path = launch_override._state["path"]
    path.write_text("{ not json")
    fresh = relaunch(tmp_path, monkeypatch)
    assert fresh.Settings.speed == 1.5
    assert not path.exists() and list(tmp_path.glob("launch_overrides.json.broken-*"))


def test_install_patches_a_module_that_is_already_imported(project, tmp_path, monkeypatch):
    live_dict = project.Settings.my_dict
    path = launch_override._state["path"]
    path.write_text(json.dumps({"version": 1, "overrides": {"cds_pkg.settings": [
        {"path": ["Settings", "my_dict"], "value": "{'0': True, 'n': 1}"}]}}))
    launch_override._state.update(path=None, overrides={}, hooked=True)
    monkeypatch.setattr(launch_override, "overrides_path", lambda app_id: path)
    launch_override.install("test-app")
    assert project.Settings.my_dict is live_dict and live_dict == {"0": True, "n": 1}


def test_overrides_are_written_only_when_dirty_and_reset_by_the_default(project):
    path = launch_override._state["path"]
    user = CodeDict(project.Settings, write_to=LaunchOverride)
    launch_override.flush()
    assert not path.exists()
    user["speed"] = 2.25
    user["speed"] = 2.75                                      # one entry, the first default kept
    launch_override.flush()
    assert json.loads(path.read_text())["overrides"] == {
        "cds_pkg.settings": [{"path": ["Settings", "speed"], "default": "1.50", "value": "2.75"}]}
    user["speed"] = 1.5                                      # back to the code's value
    del user["name"]                                         # clearing something never overridden
    launch_override.flush()
    assert json.loads(path.read_text())["overrides"] == {}


def test_launch_override_refuses_function_internals(project):
    work = CodeDict(project.work, write_to=(LaunchOverride, Hotswap))
    with pytest.raises(TypeError):
        work["parameters"]["a"] = 4
    assert project.work() == 15


def test_the_import_hook_leaves_other_modules_alone(project, tmp_path, monkeypatch):
    (tmp_path / "cds_pkg" / "plain.py").write_text("VALUE = 1\n")
    CodeDict(project.Settings, write_to=LaunchOverride)["speed"] = 2.25
    launch_override.flush()
    relaunch(tmp_path, monkeypatch)
    plain = importlib.import_module("cds_pkg.plain")
    assert plain.VALUE == 1
    assert not isinstance(plain.__spec__.loader, launch_override._OverrideLoader)
