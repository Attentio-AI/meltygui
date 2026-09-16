from typing import Dict, Set, Tuple

from meltygui.core.rendering.window_decoration import window


@window
class AttributeChurnMonitor:
    """Per-frame counter of @live attribute writes.

    Reset at frame end; pair with Toggles.attrib_churn_log to dump
    a top-N summary each frame the counter is non-empty.
    """


    attributes_changed: Set[Tuple[str, str]] = set()
    attribute_change_count: Dict[Tuple[str, str], int] = {}

    @classmethod
    def record(cls, cls_name: str, attr_name: str) -> None:
        key = (cls_name, attr_name)
        cls.attributes_changed.add(key)
        cls.attribute_change_count[key] = cls.attribute_change_count.get(key, 0) + 1

    @classmethod
    def on_frame_end(cls) -> None:
        if not cls.attribute_change_count:
            return

        from meltygui.core.runtime.toggles import Toggles
        if Toggles.attrib_churn_log:
            from meltygui.core.melty import Melty
            top = sorted(cls.attribute_change_count.items(), key=lambda kv: -kv[1])[:10]
            line = ", ".join(f"{c}.{a}={n}" for (c, a), n in top)
            print(f"[churn f{Melty.frame_count}] {line}")


        cls.attributes_changed.clear()
        cls.attribute_change_count.clear()
