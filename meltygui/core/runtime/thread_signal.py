import threading


class PauseSignal:
    def __init__(self):
        self._event = threading.Event()
        # Initially clear (not paused) so threads can process
        self._event.clear()

    def pause(self):
        """Signal threads to pause processing"""
        self._event.set()

    def resume(self):
        """Signal threads to resume processing"""
        self._event.clear()

    def wait_if_paused(self):
        """
        If paused, wait until resumed
        Otherwise return immediately
        """
        self._event.wait()
        # If we reach here and it's still set, wait again
        while self._event.is_set():
            self._event.wait(0.1)  # Small timeout to recheck condition

    def is_paused(self):
        """Check if processing should be paused"""
        return self._event.is_set()