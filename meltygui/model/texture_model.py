"""Integer-like texture values with lazy, context-owned GPU storage."""
from functools import total_ordering
import operator

from meltygui.core.graphics.gl_state import GLState, current_context, is_gl_thread


@total_ordering
class TextureId:
    """A texture ID proxy. Resolve it only where an OpenGL ID is needed.

    Subclasses provide ``target`` and ``_upload(state)``. Constructing a value
    does no GL work; int/index conversion uploads it in the current context.
    The numeric ID may change after an edit, so this mutable proxy is unhashable.
    """
    __hash__ = None

    def __init__(self, target):
        self.target = int(target)
        self._states = {}

    def _state_for_context(self, context):
        if context not in self._states:
            state = GLState()
            state._context = context
            self._states[context] = state
        return self._states[context]

    def _state(self):
        context = current_context()
        if context is None or not is_gl_thread():
            raise RuntimeError('A texture ID needs the current rendering context')
        return self._state_for_context(context)

    def _upload(self, state):
        raise NotImplementedError

    def __int__(self):
        return self._upload(self._state()).texture_id

    def __index__(self):
        return int(self)

    def __bool__(self):
        return bool(int(self))

    def __eq__(self, other):
        try:
            other_id = operator.index(other)
        except TypeError:
            return NotImplemented
        return int(self) == other_id

    def __lt__(self, other):
        try:
            other_id = operator.index(other)
        except TypeError:
            return NotImplemented
        return int(self) < other_id

    @property
    def texture_id(self):
        return int(self)

    @property
    def _as_parameter_(self):
        """ctypes/PyOpenGL accept the proxy directly as an integer argument."""
        return int(self)

    def adopt(self, state, old_key, key='texture'):
        """Move an existing allocation into this value without re-uploading it."""
        record = state._resources.pop(old_key, None)
        if record is not None:
            target = self._state_for_context(state._context)
            target.drop(key)
            target._resources[key] = record

    def release(self):
        for state in self._states.values():
            state.release()
        self._states.clear()

    def __repr__(self):
        return f'{type(self).__name__}(target={self.target:#x})'
