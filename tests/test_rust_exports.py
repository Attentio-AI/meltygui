"""The IDE reads names from `if TYPE_CHECKING:` imports; the runtime reads the lazy tables. Keep them equal."""
import ast
from pathlib import Path

import meltygui
import meltygui.rust

ROOT = Path(meltygui.__file__).parent


def typed_imports(path):
    """name -> (module, original name) for every import under `if TYPE_CHECKING:`."""
    found = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.If) and getattr(node.test, 'id', '') == 'TYPE_CHECKING':
            for statement in node.body:
                for alias in statement.names:
                    module = statement.module if isinstance(statement, ast.ImportFrom) else alias.name
                    found[alias.asname or alias.name] = (module, alias.name)
    return found


def test_rust_exports_are_visible_to_the_ide():
    typed = {name: module for name, (module, _) in typed_imports(ROOT / 'rust.py').items()}
    assert typed == meltygui.rust.EXPORTS


def test_lazy_exports_are_visible_to_the_ide():
    typed = typed_imports(ROOT / '__init__.py')
    assert {name: typed.get(name) for name in meltygui._LAZY_EXPORTS} == meltygui._LAZY_EXPORTS
    assert {'imgui', 'rust', 'toggles', 'window_api'} <= set(typed)
