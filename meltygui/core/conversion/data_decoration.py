from dataclasses import dataclass, field, MISSING, make_dataclass
import types
from typing import Any

# Types considered safe to use as constants (not recreated per instance)
IMMUTABLES = (int, float, str, bool, type(None), bytes, tuple, frozenset)

def data(_cls=None, **dc_kwargs):
    """
    A decorator that works like @dataclass, but ensures that
    all defaults are re-evaluated for every new instance.

    Behavior:
      - Immutable defaults (int, str, bool, None, tuple, etc.) are kept as-is.
      - Mutable defaults ([], {}, set()) are turned into default_factory calls.
      - Callable defaults (like list, dict, or a user function) are used as factories.
      - Function call results (e.g. id = generate_id()) are re-executed for each instance.
    """

    def wrap(cls):
        annotations = getattr(cls, "__annotations__", {})
        new_fields = []

        for name, typ in annotations.items():
            default = getattr(cls, name, MISSING)

            if default is MISSING:
                # Field without a default
                new_fields.append((name, typ))
            else:
                if isinstance(default, IMMUTABLES):
                    # Safe to use as a constant default
                    new_fields.append((name, typ, default))

                elif isinstance(default, (list, dict, set)):
                    # Dangerous mutable literal: replace with factory
                    new_fields.append(
                        (name, typ, field(default_factory=default.__class__))
                    )

                elif isinstance(default, types.FunctionType):
                    # A function type was provided: use it as default_factory
                    new_fields.append((name, typ, field(default_factory=default)))

                else:
                    # A computed value (like generate_id())
                    # We capture the original function by closure
                    def factory(val=default):
                        # If value looks callable (like generate_id()), re-call
                        if callable(val):
                            return val()
                        # Else try to copy/recreate it
                        return type(val)(val)

                    new_fields.append((name, typ, field(default_factory=factory)))

        # Rebuild dataclass with new defaults
        new_cls = make_dataclass(
            cls.__name__,
            new_fields,
            bases=cls.__bases__,
            namespace=dict(cls.__dict__),
            **dc_kwargs
        )
        return new_cls

    return wrap if _cls is None else wrap(_cls)