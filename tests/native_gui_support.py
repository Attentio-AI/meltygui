"""Headless harness for exercising the native call ABI without an OS window."""
from contextlib import contextmanager


@contextmanager
def native_frame(native, owner=None, width=300, **arguments):
    native.begin_frame(owner=owner, width=width, **arguments)
    try:
        yield native
    finally:
        native.end_frame()
