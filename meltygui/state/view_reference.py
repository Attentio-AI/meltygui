"""Runtime annotation for a DrawState borrowed from a particular view."""
from dataclasses import dataclass
import inspect


def view_identifier(view):
    view = inspect.unwrap(view)
    return f"{view.__module__}.{view.__qualname__}"


@dataclass(frozen=True)
class DrawStateSource:
    view: object

    def __post_init__(self):
        if not inspect.isfunction(inspect.unwrap(self.view)):
            raise TypeError("DrawState[...] expects a view function")

    def __repr__(self):
        return f"DrawState[{view_identifier(self.view)}]"
