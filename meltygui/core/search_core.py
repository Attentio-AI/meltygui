"""Search core functions and supporting definitions."""
import types


def search_activate_target(node):
    """The draw_state Ctrl+Enter should 'click' for the current search match
    `node` (meltygui.search_current_node). When the match is one of a collection's
    keys, that's the child at the current key; for a leaf content match it's the
    node itself."""
    if node is None:
        return None
    key = getattr(node, '_search_current_key', None)
    if key is not None:
        child = node._children.get(key)
        if child is not None:
            return child
    # Views that draw their matches as raw draw-list items (no child draw_states
    # - e.g. the Fast Dock) publish the current match's row rect instead; hand
    # back a geometry shim so the caller's center-of-rect click lands on the
    # row rather than the view's center.
    rect = getattr(node, '_search_current_rect', None)
    if rect is not None:
        return types.SimpleNamespace(abs_left=rect[0], abs_top=rect[1],
                                     width=rect[2], height=rect[3],
                                     _tile_id=node._tile_id)
    return node
