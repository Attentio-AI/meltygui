import time
from src.lsd.gl_gui import window_api as glfw
from typing import Callable, Union, Tuple
from enum import Enum

from src.lsd.gl_gui.utils.glfw_utils import request_render


class EaseType(Enum):
    LINEAR = 'linear'
    EASE_IN = 'ease_in'
    EASE_OUT = 'ease_out'
    EASE_IN_OUT = 'ease_in_out'


AnimValue = Union[float, Tuple[float, ...]]


class Animator:
    def __init__(self):
        self.start_value: AnimValue = 0
        self.end_value: AnimValue = 0
        self.duration = 0
        self.start_time = 0
        self.is_animating = False
        self.on_update: Callable[[AnimValue], None] = lambda x: None
        self.ease_type = EaseType.LINEAR

    def _ease(self, t: float) -> float:
        if self.ease_type == EaseType.LINEAR:
            return t
        elif self.ease_type == EaseType.EASE_IN:
            return t * t
        elif self.ease_type == EaseType.EASE_OUT:
            return 1 - (1 - t) * (1 - t)
        elif self.ease_type == EaseType.EASE_IN_OUT:
            if t < 0.5:
                return 2 * t * t
            return 1 - (-2 * t + 2) ** 2 / 2
        return t

    def _interpolate(self, start: AnimValue, end: AnimValue, progress: float) -> AnimValue:
        """Interpolate between start and end values, handling both floats and tuples."""
        if isinstance(start, tuple) and isinstance(end, tuple):
            # Interpolate each component of the tuple
            return tuple(
                start[i] + (end[i] - start[i]) * progress
                for i in range(len(start))
            )
        else:
            # Handle as float
            return start + (end - start) * progress

    def stop(self):
        """Stop the current animation."""
        self.is_animating = False

    def animate(self, start: AnimValue, end: AnimValue, duration: float,
                on_update: Callable[[AnimValue], None], ease_type: EaseType = EaseType.EASE_IN_OUT):
        """
        Start animation with support for both float and tuple values.

        Args:
            start: Starting value (float or tuple of floats)
            end: Ending value (float or tuple of floats)
            duration: Animation duration in seconds
            on_update: Callback function that accepts either float or tuple
            ease_type: Type of easing function to use
        """
        if isinstance(start, tuple) != isinstance(end, tuple):
            raise ValueError("Start and end values must be the same type (both float or both tuple)")
        if isinstance(start, tuple) and len(start) != len(end):
            raise ValueError("Start and end tuples must have the same length")

        self.start_value = start
        self.end_value = end
        self.duration = duration
        self.start_time = time.time()
        self.is_animating = True
        self.on_update = on_update
        self.ease_type = ease_type

    def update(self):
        if not self.is_animating:
            return

        elapsed = time.time() - self.start_time
        if elapsed >= self.duration:
            self.is_animating = False
            self.on_update(self.end_value)
            return

        progress = self._ease(elapsed / self.duration)
        current = self._interpolate(self.start_value, self.end_value, progress)

        self.on_update(current)
        request_render()  # Request next frame