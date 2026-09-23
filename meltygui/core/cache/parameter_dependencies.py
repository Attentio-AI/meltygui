"""Weak subscriptions from injected state objects to their cached consumers."""
from weakref import WeakKeyDictionary, WeakSet, ref


class ParameterDependencies:
    def __init__(self):
        self.sources = WeakKeyDictionary()
        self.consumers = WeakKeyDictionary()

    def bind(self, consumer, values):
        previous = self.consumers.get(consumer, {})
        if (previous.keys() == values.keys()
                and all(previous[name]() is value for name, value in values.items())):
            return False
        # Keep parameter identities as well as the source set: swapping two
        # parameters between the same sources must invalidate the consumer.
        old_sources = {item() for item in previous.values() if item() is not None}
        new_sources = set(values.values())
        old_sources.discard(consumer)
        new_sources.discard(consumer)
        for source in old_sources - new_sources:
            subscribers = self.sources.get(source)
            if subscribers is not None:
                subscribers.discard(consumer)
        for source in new_sources - old_sources:
            self.sources.setdefault(source, WeakSet()).add(consumer)
        if values:
            self.consumers[consumer] = {name: ref(value) for name, value in values.items()}
        else:
            self.consumers.pop(consumer, None)
        return True

    def subscribers(self, source):
        try:
            return tuple(self.sources.get(source, ()))
        except TypeError:
            return ()

    def invalidate(self, cache, source, frame_delta=0, note=None):
        # A view reference also depends on state injected into that view.
        # Follow those edges once; mutually linked views must not recurse forever.
        pending = list(self.subscribers(source))
        visited = set()
        while pending:
            consumer = pending.pop()
            if consumer in visited:
                continue
            visited.add(consumer)
            if consumer._tile_id is not None:
                cache.invalidate_up(consumer._tile_id, force=True,
                                    frame_delta=frame_delta, note=note)
            pending.extend(self.subscribers(consumer))
