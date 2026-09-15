"""Visibility contract shared by nested Melty and GLFW child windows."""


def requested_window_closed(closed, kwargs, *, first_request=False):
    if first_request and 'open_requested' in kwargs:
        closed = True
    if kwargs.get('closed') is not None:
        closed = bool(kwargs['closed'])
    if kwargs.get('open_requested'):
        closed = False
    return closed
