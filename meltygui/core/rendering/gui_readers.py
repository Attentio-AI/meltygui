"""Readers: the `@gui` views that read a model's values, so the model can run them again.

    class Model:
        def __init__(self):
            self.readers = Readers()

        @property
        def value(self):
            self.readers.note()         # the view whose body reads this
            return self._value

        def work(self):                 # any thread
            self._value = fresh
            self.readers.wake()

A retained view runs again only when it is asked to, so a model that swaps in
a result on a worker thread has to say who read the old one: without this a
cached root keeps passing the value it read on its last run. `note` costs a
context lookup; `wake` is `RetainedGui.wake`, the one cache call a worker
may make. A host that runs its root on every frame (`use_cache=False`, the
legacy host) has no reading view: `wake` then only asks for a frame.
"""
import weakref

from meltygui.core.rendering.gui_prototype import _window


class Readers:
    __slots__ = ('found',)

    def __init__(self):
        self.found = {}            # (id of the cache, node) -> weak reference to the cache

    def note(self):
        """Called where a view's body reads the model: that view is woken by `wake`."""
        window = _window.get()
        if window is not None and window.cache.stack:
            cache = window.cache
            key = (id(cache), cache.stack[-1])
            if key not in self.found:
                self.found[key] = weakref.ref(cache)

    def wake(self):
        """Thread safe: the model has new values. Every reading view runs again; views that are gone are forgotten."""
        from meltygui.core.windowing.glfw_utils import request_render
        for key, held in list(self.found.items()):
            cache = held()
            if cache is None or key[1] not in cache.records:
                self.found.pop(key, None)
            else:
                cache.wake(key[1])
        request_render()
