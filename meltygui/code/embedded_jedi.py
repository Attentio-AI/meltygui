"""Jedi introspection inside a running app, without process-global path swaps.

Jedi's usual same-process helpers temporarily replace sys.path. Its analysis
worker then races imports on the render thread and application workers. Source
lookups can pass their search path directly; compiled modules must use the
embedded application's actual environment.
"""
import importlib
import sys
import warnings

from jedi.api.environment import InterpreterEnvironment
from jedi.inference.compiled import access
from jedi.inference.compiled.subprocess import InferenceStateSameProcess, functions


class EmbeddedProcess(InferenceStateSameProcess):
    def get_module_info(self, sys_path=None, full_name=None, **kwargs):
        if kwargs.get('path') is None and sys_path is not None:
            kwargs['path'] = sys_path
            # Jedi's namespace-package result normally gets this from a global
            # lookup. Passing an explicit root path must retain that name.
            full_name = full_name or kwargs['string']
        try:
            return functions._find_module(full_name=full_name, **kwargs)
        except ImportError:
            return None, None

    def load_module(self, dotted_name, sys_path):
        # Project-specific Python sources are resolved statically above. Native
        # introspection uses this interpreter's modules, never another venv's
        # binaries or a temporary replacement of the application's sys.path.
        try:
            module = importlib.import_module(dotted_name)
        except Exception:
            warnings.warn(f'Module {dotted_name} is not importable in the embedded app.',
                          stacklevel=2)
            return None
        return access.create_access_path(self._inference_state_weakref(), module)


class EmbeddedEnvironment(InterpreterEnvironment):
    def get_inference_state_subprocess(self, inference_state):
        return EmbeddedProcess(inference_state)

    def get_sys_path(self):
        return list(sys.path)
