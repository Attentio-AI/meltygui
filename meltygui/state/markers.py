
from typing import Any

from src.lsd.gl_gui.view.core_views.core_presets import Meta


class Val:
    def __init__(self, option:Any):
        self.option = option


def _is_field_candidate(v) -> bool:
    # Treat non-callables that aren't descriptors as "fields"
    return not callable(v) and not isinstance(v, (staticmethod, classmethod, property))


class FieldMeta(type):
    @classmethod
    def __prepare__(mcls, name, bases):
        # any mapping works; order is guaranteed in modern Python
        return {}

    def __setattr__(cls, name, value):
        print(f"Class attribute {name} set to {value!r}")
        super().__setattr__(name, value)

    def __new__(mcls, name, bases, namespace):
        new_namespace = {}
        field_defaults = {}
        field_meta = {}

        if name == "Lora":
            pass

        for key, value in namespace.items():
            if key.startswith("__") and key.endswith("__"):
                new_namespace[key] = value
                continue

            if not _is_field_candidate(value):
                new_namespace[key] = value
                continue

            # marker line
            if isinstance(value, Meta):
                field_defaults[key] = value.value
                field_meta[key] = value
                new_namespace[key] = value.value
                new_namespace[f"{key}_meta"] = value
            else:
                # Plain value still becomes a field
                field_defaults[key] = value
                new_namespace[key] = value

        new_namespace["__field_defaults__"] = field_defaults
        new_namespace["__field_meta__"] = field_meta

        cls = super().__new__(mcls, name, bases, new_namespace)
        return cls

    def __call__(cls, *args, **kwargs):
        """
        Make a fresh instance, then backfill any missing attributes
        from __field_defaults__ (deepcopying mutables).
        """
        obj = super().__call__(*args, **kwargs)

        # Only fill attributes not explicitly set by __init__
        for k, v in getattr(cls, "__field_defaults__").items():
            if k not in obj.__dict__:
                setattr(obj, k, v)
        return obj


    def get_field_meta(cls, field_name):
        """Get Meta object for a given field, or default."""
        return getattr(cls, f"{field_name}_meta", Meta.get_default())




# disabled = Marker(disabled=True)
# colored_text = Marker(colored_text=True)
# class Car(metaclass=FieldMeta):
#     # class attribute
#     visibleinui
#     wheels: int = 4
#
#     # instance attribute
#     colored_text((1, 0, 0))
#     year: int = 2025
#
#     disabled
#     mileage: int = 0
#
# # c = Car()
# print(c.year)  # 2025
# print(c.mileage)  # 0
# print(Car.wheels)  # 4
#
# # Meta
# print(Car.year.meta)  # {'colored_text': True, 'args': (1, 0, 0)}
# print(Car.mileage.meta)  # {'disabled': True}
# print(Car.wheels.meta)  # {'visibleinui': True}
