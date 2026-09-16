"""Bounded, always-on resize diagnostics; records metadata, never view values.

Each process writes .melty/resize-<pid>.log (JSON lines, two 4 MiB backups).
No draw-state references are retained and logging failures cannot break a drag.
"""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time
import traceback


def record(stage, window, event=None, error=False, **details):
    try:
        from meltygui.melty import Melty
        logger = logging.getLogger(f"meltygui.resize.{os.getpid()}")
        if not logger.handlers:
            from meltygui.paths import cache_root
            path = cache_root()
            path.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path / f"resize-{os.getpid()}.log",
                                          maxBytes=4 * 1024 * 1024, backupCount=2)
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        entry = dict(time=time.time(), frame=Melty.frame_count, stage=stage,
                     window=str(getattr(window, 'id', None)), identity=id(window),
                     name=str(getattr(window, 'name', ''))[:200])
        for key in ('window_pos', 'width', 'height', 'abs_left', 'abs_top',
                    'min_width', 'min_height', 'closed', 'expanded', 'use_cache',
                    'freeze_resize', 'size_change', '_frame_pinned',
                    '_initial_window_size', '_initial_window_pos_resize',
                    '_resize_from_top_left', '_resize_target_edge_x0', '_resize_target_row_y0'):
            entry[key] = getattr(window, key, None)
        for key, axis in (('_resize_target_edge', 'x'), ('_resize_target_row', 'y')):
            edge = getattr(window, key, None)
            entry[key] = None if edge is None else (id(edge), edge.get(axis))
        if event is not None:
            entry['event'] = {key: getattr(event, key, None)
                              for key in ('x', 'y', 'dx', 'dy', 'total_dx', 'total_dy')}
        if error:
            entry['traceback'] = traceback.format_exc()
        import meltygui.os_frame as os_frame
        entry['geometry_mode'] = os_frame._STATE['mode']
        entry['geometry_generation'] = os_frame._STATE['generation']
        entry['os_expected'] = list(os_frame._STATE['expected'])
        entry['os_unapplied'] = list(os_frame._STATE['unapplied'])
        entry.update(details)
        logger.info(json.dumps(entry, default=lambda value: f'<{type(value).__name__}>'))
    except Exception:
        # Diagnostics must never turn an otherwise valid frame into a failure.
        pass


def edges(window, axis):
    """Small identity/geometry snapshot; never serialize a draw_state or its data."""
    registry = getattr(window, '_edge_views' if axis == 'x' else '_row_views', {})
    return [{"owner": str(key), "closed": getattr(view, 'closed', False),
             "edges": [(id(edge), edge.get(axis)) for edge in edge_list]}
            for key, (view, edge_list) in registry.items()]
