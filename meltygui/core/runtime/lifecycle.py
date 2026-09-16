"""Session-lifetime checks for long-running threads.

A studio "restart" is IN-PROCESS: model_server purges every `src.*` entry from
sys.modules and re-imports, so a daemon poller started by the previous session
keeps running against its OLD module globals — and pins that whole session
(old Melty → draw_states → code hosts → parsed cst dicts) for the life of the
process, while still doing its polling. Found via the gc boot profile: ~1M
retained objects per restart after the atexit/gc.callbacks roots were fixed.

`module_is_live(globals())` is the exit test for such loops: it is True while
the caller's module dict is the one currently registered under its name.
A hotswap re-execs INTO the same dict, so the check survives hotswap; a
restart binds a new module object, so every old-session loop sees False on
its next iteration and returns.
"""
import sys


def module_is_live(module_globals):
    mod = sys.modules.get(module_globals.get("__name__"))
    return mod is not None and getattr(mod, "__dict__", None) is module_globals
