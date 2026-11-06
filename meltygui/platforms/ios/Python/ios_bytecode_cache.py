"""Persist checked-hash import caches in the native host's writable prefix.

CPython's normal import path compiles on demand and writes atomically. Keep that
path, but hash the exact bytes compiled so same-size, same-timestamp edits cannot
reuse stale code. No application imports or filesystem scans are needed.
"""
import importlib.machinery
import importlib.util


def install():
    loader = importlib.machinery.SourceFileLoader
    if getattr(loader, '_ios_checked_cache', False):
        return
    compile_source = loader.source_to_code
    cache_bytecode = loader._cache_bytecode

    def source_to_code(self, data, path, *, _optimize=-1):
        # Hash the compilation input, not a later reread of a possibly edited file.
        self._ios_source_hash = (importlib.util.source_hash(data)
                                 if isinstance(data, (bytes, bytearray, memoryview)) else None)
        return compile_source(self, data, path, _optimize=_optimize)

    def write_cache(self, source_path, bytecode_path, data):
        digest = getattr(self, '_ios_source_hash', None)
        if digest is not None:
            # PEP 552: magic + checked-hash flags + 8-byte source hash + code.
            data = data[:4] + b'\x03\x00\x00\x00' + digest + data[16:]
        return cache_bytecode(self, source_path, bytecode_path, data)

    loader.source_to_code = source_to_code
    loader._cache_bytecode = write_cache
    loader._ios_checked_cache = True
