"""Geometric relationships between caller-owned frames and native surfaces.

A surface body is connected by two fixed gaps, not a second free window inside
its native frame. Both native containment and local layout use this relationship.
"""


class SurfaceBinding:
    def __init__(self, window, axis, near_gap, far_gap):
        self.window = window
        self.axis = axis
        self.near_gap = float(near_gap)
        self.far_gap = float(far_gap)

    def content_size(self, native_size):
        return native_size - self.near_gap - self.far_gap

    def minimum_size(self, content_minimum):
        return content_minimum + self.near_gap + self.far_gap

    def cells(self, native_edges, frame_edges):
        near, far = native_edges
        frame_near, frame_far = frame_edges
        return ([(near, frame_near, self.near_gap, self.near_gap),
                 (frame_far, far, self.far_gap, self.far_gap)])


class EdgeProjection:
    """Private native/display edge copies with their stable source identities.

    A context owns this collection for one local solve. Keeping the projection
    together also leaves Context's existing slot layout intact during hotswap.
    """
    def __init__(self, native, screen, binding):
        self.binding = binding
        self.native = tuple(dict(edge) for edge in native)
        self.screen = tuple(dict(edge) for edge in screen)
        self.sources = {id(local): source for local, source in
                        zip((*self.native, *self.screen), (*native, *screen))}

    def __iter__(self):
        return iter((*self.native, *self.screen))
