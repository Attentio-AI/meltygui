"""One cancellable, deadline-based invalidation owned by a cached view."""
import threading
import time
from weakref import ref

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save


@no_save('_lock', '_timer', '_token', '_deadline', '_target')
class DeferredInvalidation(DictConversion):
    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self._timer = None
        self._token = None
        self._deadline = 0.0
        self._target = None

    def schedule(self, draw_state, cache, deadline):
        """Repeated draws update one deadline, without waking or polling frames."""
        with self._lock:
            self._deadline = deadline
            self._target = (ref(draw_state), ref(cache), draw_state._tile_id)
            if self._token is None:
                self._token = object()
                self._arm(self._token)

    def _arm(self, token):
        owner = ref(self)

        def fire():
            state = owner()
            if state is not None:
                state._fire(token)

        self._timer = threading.Timer(max(0.001, self._deadline - time.monotonic()), fire)
        self._timer.daemon = True
        self._timer.start()

    def cancel(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = self._token = self._target = None

    def _fire(self, token):
        with self._lock:
            if self._token is not token:
                return
            if time.monotonic() < self._deadline:
                self._arm(token)
                return
            draw_state, cache, _ = self._target
            draw_state, cache = draw_state(), cache()
            if draw_state is None or cache is None or draw_state.closed:
                self.cancel()
                return
        # Invalidate between frames, using the original window's cache rather
        # than whichever Surface happens to be current when the timer expires.
        from meltygui.core.melty import Melty
        owner = ref(self)

        def deliver():
            state = owner()
            if state is not None:
                state._deliver(token)

        Melty.post_to_render(deliver)

    def _deliver(self, token):
        with self._lock:
            if self._token is not token:
                return
            if time.monotonic() < self._deadline:
                self._arm(token)
                return
            draw_state, cache, key = self._target
            draw_state, cache = draw_state(), cache()
            self.cancel()
        if (draw_state is None or cache is None or draw_state.closed
                or cache.key_to_draw_state.get(key) is not draw_state):
            return
        from meltygui.core.cache.invalidation_tracker import Note
        from meltygui.core.windowing.glfw_utils import request_render
        cache.invalidate_up(key, force=True, max_depth=8,
                            note=Note(name="deferred symbol refresh", draw_state=draw_state))
        request_render()
