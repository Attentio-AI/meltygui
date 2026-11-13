"""A standalone ImGui app, a module with its own window and frame loop, as a @gui view.

    draw_counter = gui_module('counter_app.py', width=420, height=160)
    changed, text = draw_counter('Not hello', name='left')      # in any @gui body

Two shapes of app are hosted: one with its own GLFW frame loop, and a meltygui
app, whose `@os_window` function is its frame.

The module is not imported: each view instance runs it, and the .py files
beside it, in namespaces of its own, so its globals are that instance's state.
A top level `name = <value>` is a parameter: a value passed to the call
replaces the global on every frame it is passed (and the module's own value is
not computed), an omitted one leaves the global to the module. No name is
special: `view(title='x')` sets the module's `title`. The call's positional
value goes to the first parameter in the file, and a change to that one is
what the call returns. A global named like one of the view's own values
(VIEW_VALUES: `width`, `name`, `depth`, ...) is not a parameter: the view's
value would silently replace it on every run.

How the module becomes lifecycle code (`split`):
- The app is suspended where it waits for events (WAIT_CALLS): the code up to
  its first wait is load, from one wait to the next is a draw, and when the
  view closes the wait ends its loop (`break`), so what follows the loop, and
  every `finally` / `with` around it, is terminate. Locals stay locals.
- Functions that reach a wait, and the module's own tail from the statement
  that starts it, are generators (`yield from`), so the loop can sit in
  `main()`, behind `if __name__ == '__main__':` or in a method.
- A meltygui app has no loop of its own: the hosting view calls its
  `@os_window` / `@glfw_window` functions directly, with its own size, and
  `meltygui.run` does nothing. Closing runs their `on_close`.
- A thread whose target holds the loop is not started; its generator runs on
  the render thread. Other threads are real.
- Windowing is imported from shims (`SHIMS`): GLFW, the ImGui context / frame /
  backend calls, GL clears, signal and atexit handlers, `sys.argv`. The app
  sees the view as its window: its size, pointer and focus. Where a windowing
  call is plainly one of those that do nothing hosted (swap_buffers, render,
  new_frame, glClear...), it is compiled away (`Quiet`) and costs nothing.
- What belongs to the host stays the host's: writes to io are dropped, fonts
  are stand-ins (text uses the host's font), style colors are recorded and
  pushed around each draw, a backend's frame methods do nothing.

When the view runs: on the host's invalidation (input, arguments); when the
app's own timed wait runs out; and, for an app that also blocks in a wait, on
the frame after one it polled in (`live`). A run in which the app's loop drew
nothing (it was idle) gives it a window refresh and runs it once more.

The module's statements before the loop starts run on one shared loader thread
the first time a file is hosted, and for as long as they take longer than
INLINE; a file known to load fast loads on the render thread and draws in its
first frame. `background=False` always loads on the render thread, for a
module that needs GL there. The launch function's own setup runs on the
render thread with the first draw.

Cost (tests/benchmark_gui_module.py): a run is the @gui view's own cost plus
this file's `view` and `App.draw`; keep both free of per-run allocation.
Compiled programs are shared by every instance of a file (`program`).

Gaps: edits to the file are not picked up by a running instance, a wait inside
a lambda, a comprehension or a sibling file is not supported, a window
function's decoration (tint, scroll, events) and `settings=` are dropped and
its injected state objects are not saved with the session, style variables
(rounding, padding) and the app's own fonts are not applied, and in-place
edits of a mutable first parameter are not reported as changed.
"""
import ast
import atexit
import builtins
import functools
import importlib
import inspect
import os
import queue
import signal
import sys
import threading
import time
import traceback
import types
from pathlib import Path

from meltygui.core.rendering.gui_prototype import gui

# Lever: the calls an app waits for events with; the first never blocks, the others do.
WAIT_CALLS = ('poll_events', 'wait_events', 'wait_events_timeout')
# Lever: the host owns these; a hosted module's calls to them do nothing.
# The view is the module's window: its begin / end and window placement are dropped too.
IMGUI_HOST_CALLS = ('create_context', 'destroy_context', 'set_current_context', 'end_frame', 'render',
                    'end', 'set_next_window_position', 'set_next_window_size')
# As a statement, begin's answer is unused: it is dropped with them when the app is compiled.
IMGUI_STATEMENTS = frozenset((*IMGUI_HOST_CALLS, 'begin'))
GL_HOST_CALLS = ('glClear', 'glClearColor', 'glViewport')
BACKEND_HOST_CALLS = ('process_inputs', 'render', 'refresh_font_texture', 'shutdown')
# What @gui itself hands a view's **kwargs: never routed to a module global of the same name.
VIEW_VALUES = frozenset(('depth', 'height', 'left', 'max_height', 'min_height', 'name', 'return_extras',
                         'scroll_offset', 'tint', 'top', 'unique', 'view_events', 'width', 'key'))

MAIN, DRIVE, WAIT, ARGS, APP = '__gui_main__', '__gui_drive__', '__gui_wait__', '__gui_args__', '__gui_app__'
DRAWN = '__gui_drawn__'     # set true in the app's namespace where it began a frame (imgui.new_frame)
# Lever: GLFW calls that do something for a hosted app. Any other one, called for nothing but its
# effect, is dropped when the module is compiled (`Quiet`); so are the IMGUI / GL host calls above.
GLFW_LIVE = frozenset((*WAIT_CALLS, 'window_should_close', 'set_window_should_close', 'post_empty_event'))
# The modules whose calls are known by name, and how a module may have bound them.
HOSTS = {'glfw': 'glfw', 'imgui': 'imgui', 'meltygui_imgui': 'imgui', 'OpenGL.GL': 'gl'}
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
          ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
# What a shimmed event wait returns: the rewritten call site suspends on it.
WAITING = object()
UNSET = object()

class Program:
    """A compiled app file, shared by every instance of it.

    code: the rewritten module; parameters: the names it assigns at top level, in order
    (first: the one the call's value goes to); blocks: it also waits without polling, so
    its polls ask for the next frame; beside: {import name: path of the file next to the
    app, or None}, filled as instances import; seconds: how long its last load took (None:
    never loaded), which decides where the next one runs (INLINE).
    """
    __slots__ = ('code', 'parameters', 'first', 'blocks', 'beside', 'seconds')

    def __init__(self, code, parameters, blocks):
        self.code, self.parameters, self.blocks = code, tuple(parameters), blocks
        self.first, self.beside, self.seconds = (parameters[0] if parameters else None), {}, None


class SplitError(ValueError):
    """The module does not wait for events where this wrapper can suspend it."""


def own(node):
    """The nodes of one scope: nested functions, classes and lambdas are not entered."""
    todo = [node]
    while todo:
        for child in ast.iter_child_nodes(todo.pop()):
            yield child
            if not isinstance(child, SCOPES):
                todo.append(child)


def called(node):
    """The name a call goes by: `f()` and `x.f()` are both 'f'."""
    if isinstance(node, ast.Call):
        target = node.func
        return target.id if isinstance(target, ast.Name) else getattr(target, 'attr', None)


def calls(node):
    return {called(child) for child in own(node)} - {None}


def bound(statements):
    """Names the statements bind at their own scope (what a wrapped tail must declare global)."""
    names = set()
    for statement in statements:
        for node in (statement, *(() if isinstance(statement, SCOPES) else own(statement))):
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                names.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                if any(alias.name == '*' for alias in node.names):
                    raise SplitError(f'line {node.lineno}: a star import must come before the frame loop starts')
                names.update(alias.asname or alias.name.partition('.')[0] for alias in node.names)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
    return names


def hosts(tree):
    """`host(call)` for a module: ('glfw' | 'imgui' | 'gl', function name) when the call is
    certainly one into a host module, else None. Certain means reached through a name that an
    import bound (`glfw.f()`, `import glfw as g`, `from glfw import f`) and nothing else rebinds."""
    modules, functions, stored = {}, {}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in HOSTS and (alias.asname or '.' not in alias.name):
                    modules[alias.asname or alias.name] = HOSTS[alias.name]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            for alias in node.names:
                if f'{node.module}.{alias.name}' in HOSTS:      # from OpenGL import GL, from meltygui import imgui
                    modules[alias.asname or alias.name] = HOSTS[f'{node.module}.{alias.name}']
                elif node.module == 'meltygui' and alias.name == 'imgui':
                    modules[alias.asname or alias.name] = 'imgui'
                elif node.module in HOSTS and alias.name != '*':
                    functions[alias.asname or alias.name] = (HOSTS[node.module], alias.name)
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            stored.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stored.add(node.name)
        elif isinstance(node, ast.arg):
            stored.add(node.arg)

    def host(node):
        if not isinstance(node, ast.Call):
            return None
        target = node.func
        if isinstance(target, ast.Name) and target.id not in stored:
            return functions.get(target.id)
        if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id not in stored and target.value.id in modules):
            return modules[target.value.id], target.attr
    return host


def plain(node):
    """An argument that only reads values: dropping the call it is passed to loses nothing."""
    if isinstance(node, (ast.Name, ast.Constant)):
        return True
    if isinstance(node, (ast.Attribute, ast.UnaryOp, ast.Starred, ast.keyword)):
        return plain(node.operand if isinstance(node, ast.UnaryOp) else node.value)
    if isinstance(node, ast.BinOp):
        return plain(node.left) and plain(node.right)
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(plain(item) for item in node.elts)
    return False


class Quiet(ast.NodeTransformer):
    """Compile away what a hosted app's windowing calls would do nothing with.

    Their shims are still there for a call this does not recognise (`hosts`);
    this only makes the common frame loop cost its own draw code and no more:
    - a host call made for its effect alone (swap_buffers, render, glClear...) is dropped,
    - `imgui.new_frame()` is the store that marks the frame as drawn,
    - `glfw.window_should_close(w)` reads the app's closing flag.
    """

    def __init__(self, host):
        self.host, self.marks = host, []     # marks: per enclosing function, whether it sets DRAWN

    def visit_Expr(self, node):
        found = self.host(node.value)
        call = node.value
        if found is None or not all(plain(part) for part in (*call.args, *call.keywords)):
            return self.generic_visit(node)
        kind, name = found
        if kind == 'imgui' and name == 'new_frame':
            if self.marks:
                self.marks[-1] = True
            return ast.copy_location(ast.Assign([ast.Name(DRAWN, ast.Store())], ast.Constant(True)), node)
        if (kind == 'imgui' and name in IMGUI_STATEMENTS or kind == 'gl' and name in GL_HOST_CALLS
                or kind == 'glfw' and name not in GLFW_LIVE and not name.endswith('_callback')):
            return ast.copy_location(ast.Pass(), node)
        return self.generic_visit(node)

    def visit_Call(self, node):
        self.generic_visit(node)
        if self.host(node) == ('glfw', 'window_should_close') and all(plain(part) for part in node.args):
            return ast.copy_location(ast.Attribute(ast.Name(APP, ast.Load()), 'closing', ast.Load()), node)
        return node

    def visit_FunctionDef(self, node):
        self.marks.append(False)
        self.generic_visit(node)
        if self.marks.pop():
            first = node.body[0]
            documented = isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            node.body.insert(int(documented), ast.copy_location(ast.Global([DRAWN]), first))
        return node

    def visit_ClassDef(self, node):
        return node     # a class body is not a frame: left to the shims


class Drive(ast.NodeTransformer):
    """In one scope, suspend at the event waits and run the calls that reach one as `yield from`.

    A wait suspends with what it asks of the host: `yield 0.0` for a poll, the
    timeout for a timed wait, infinity for a plain one. A wait `host` is not
    sure of goes through its shim, which answers WAITING and leaves the time on the app.
    """

    def __init__(self, names, direct, host):
        # names: calls that may wait; direct: those that are this module's own generator functions.
        self.names, self.direct, self.host, self.loops = names, direct, host, 0

    def asked(self, call):
        """The `yield <seconds>` of a certain wait call, or None."""
        found = self.host(call)
        if found is None or found[0] != 'glfw' or found[1] not in WAIT_CALLS:
            return None
        timed = found[1] == 'wait_events_timeout' and (call.args or call.keywords)
        seconds = (call.args[0] if call.args else call.keywords[0].value) if timed else ast.Constant(
            0.0 if found[1] == 'poll_events' else float('inf'))
        return ast.Yield(seconds)

    def loop(self, node):
        self.loops += 1
        self.generic_visit(node)
        self.loops -= 1
        return node

    visit_While = visit_For = loop

    def visit_Expr(self, node):
        call = node.value
        if self.loops == 1 and called(call) in WAIT_CALLS:
            # The frame loop's own wait: `if (yield <seconds>): break`, or for an uncertain one
            # `if <wait>() is <waiting> and (yield): break`. One yield a frame, no generator of
            # its own; the true value the view's close sends leaves the loop.
            stop = self.asked(call)
            if stop is None:
                waiting = ast.Compare(call, [ast.Is()], [ast.Name(WAIT, ast.Load())])
                stop = ast.BoolOp(ast.And(), [waiting, ast.Yield()])
            return ast.copy_location(ast.If(stop, [ast.Break()], []), node)
        return self.generic_visit(node)

    def visit_Call(self, node):
        self.generic_visit(node)
        name = called(node)
        if name in WAIT_CALLS:
            asked = self.asked(node)
            if asked is not None:
                return ast.copy_location(asked, node)
        if isinstance(node.func, ast.Name) and name in self.direct:
            return ast.copy_location(ast.YieldFrom(node), node)
        if name not in self.names:
            return node
        driven = ast.Call(ast.Name(DRIVE, ast.Load()), [node], [])
        return ast.copy_location(ast.YieldFrom(driven), node)

    def keep(self, node):
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = visit_Lambda = keep
    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = keep


def waits(tree):
    return [node for node in ast.walk(tree) if called(node) in WAIT_CALLS]


def split(source, filename='<gui_module>'):
    """Module source -> Program: suspended at its event waits, its top level names made parameters."""
    tree = ast.parse(source, filename)
    host = hosts(tree)
    tree = Quiet(host).visit(tree)
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    found = waits(tree)
    for node in found:
        scope = node
        while scope in parents and not isinstance(scope, SCOPES):
            scope = parents[scope]
        if isinstance(scope, SCOPES) and not isinstance(scope, ast.FunctionDef):
            raise SplitError(f'{filename} line {node.lineno}: an event wait must be in a plain function or at module level')

    # Every function that reaches a wait, by name.
    colored, size = set(WAIT_CALLS), -1
    while len(colored) != size:
        size = len(colored)
        colored.update(function.name for function in functions if calls(function) & colored)
    names = colored | {'join'}
    # Called by its bare name, a module level one is known to be a generator: no check at run time.
    direct = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)} & colored - set(WAIT_CALLS)
    for function in functions:
        if function.name in colored:
            drive = Drive(names, direct, host)
            function.body = [drive.visit(statement) for statement in function.body]

    def starts(statement):
        return not isinstance(statement, SCOPES) and any(called(node) in names for node in own(statement))

    start = next((index for index, statement in enumerate(tree.body) if found and starts(statement)), len(tree.body))
    head, tail = tree.body[:start], tree.body[start:]

    parameters = []
    for statement in head:
        if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)):
            name = statement.targets[0].id
            if name.startswith('_') or name in parameters or name in VIEW_VALUES:
                continue
            parameters.append(name)
            # name = <args>[name] if name in <args> else <value>: load code sees the caller's value.
            take = ast.parse(f'{ARGS}[{name!r}] if {name!r} in {ARGS} else 0', mode='eval').body
            take.orelse = statement.value
            statement.value = ast.copy_location(take, statement.value)

    if tail:
        declared = sorted(bound(tail))
        drive = Drive(names, direct, host)
        body = ([ast.Global(declared)] if declared else []) + [drive.visit(statement) for statement in tail]
        main = ast.FunctionDef(name=MAIN, args=ast.parse('lambda: 0', mode='eval').body.args, body=body,
                               decorator_list=[], type_params=[])
        tree.body = head + [ast.copy_location(main, tail[0])]
    blocks = any(called(node) != WAIT_CALLS[0] for node in found)
    code = compile(ast.fix_missing_locations(tree), filename, 'exec')
    return Program(code, parameters, blocks)


# path: (mtime, value). One compile per file, shared by every instance; a later instance sees an edit.
_programs, _beside = {}, {}


# Lever: seconds a file's compiled program is used before its mtime is looked at again, so a
# frame that starts hundreds of instances stats the file once.
RECHECK = 0.25
_checked = {}


def program(path):
    """The Program of the app file at `path`."""
    now = time.monotonic()
    entry = _programs.get(path)
    if entry is not None and now - _checked[path] < RECHECK:
        return entry[1]
    stamp = os.stat(path).st_mtime_ns
    if entry is None or entry[0] != stamp:
        entry = _programs[path] = (stamp, split(Path(path).read_text(), path))
    _checked[path] = now
    return entry[1]


def beside(path):
    """The code of a file next to an app: part of the app, but it may not wait for events."""
    now = time.monotonic()
    entry = _beside.get(path)
    if entry is not None and now - _checked[path] < RECHECK:
        return entry[1]
    stamp = os.stat(path).st_mtime_ns
    _checked[path] = now
    if entry is None or entry[0] != stamp:
        source = Path(path).read_text()
        if waits(ast.parse(source, path)):
            raise SplitError(f'{path}: an event wait must be in the app\'s main file')
        entry = _beside[path] = (stamp, compile(source, path, 'exec'))
    return entry[1]


def drive(value):
    """What a rewritten call returns: an event wait suspends here, a generator that waits runs in place."""
    if value is WAITING:
        return (yield)
    if type(value) is types.GeneratorType:
        return (yield from value)
    return value


def quiet(*args, **kwargs):
    return None


class Null:
    """A window, a monitor: every attribute, item and call gives it back."""

    def __getattr__(self, name):
        return self

    def __call__(self, *args, **kwargs):
        return self

    __getitem__ = __getattr__


NULL = Null()


class Shim(types.ModuleType):
    """A real module with some names replaced; star imports still see the real names.

    A name is looked up in the real module once and kept (`imgui.text` in a draw
    loop costs what it costs unhosted). `Passing` is for modules whose values change.
    """

    def __init__(self, real, **replaced):
        super().__init__(real.__name__)
        self.__dict__.update(replaced, _real=real)

    def __getattr__(self, name):
        if name == '__all__':
            return [name for name in dir(self._real) if not name.startswith('_')]
        value = self.__dict__[name] = getattr(self._real, name)
        return value


class Passing(Shim):
    def __getattr__(self, name):
        return getattr(self._real, name)


@functools.cache
def gl_shim():
    import OpenGL
    import OpenGL.GL
    return Shim(OpenGL, GL=Shim(OpenGL.GL, **dict.fromkeys(GL_HOST_CALLS, quiet)))


@functools.cache
def signal_shim():
    # The host keeps its handlers; a hosted module ends through its frame loop.
    return Shim(signal, signal=lambda number, handler: signal.SIG_DFL)


class Guest:
    """A host object as the app sees it: reads pass through, writes are dropped."""

    def __init__(self, real, **replaced):
        self.__dict__.update(replaced, _real=real)

    def __getattr__(self, name):
        return getattr(self._real(), name)

    def __setattr__(self, name, value):
        pass


class Io(Guest):
    """ImGui's io for a hosted app: the display is its view, input state is a copy."""
    display_size = property(lambda self: self._app.extent())
    mouse_down = property(lambda self: list(self._real().mouse_down))


class Font:
    """Stands in for a font the app added: text drawn with it uses the host's font."""


class Fonts:
    """The app's font atlas: nothing is baked, the host's atlas is not touched."""
    texture_id = 0

    def add_font_from_file_ttf(self, *args, **kwargs):
        return Font()

    add_font_default = add_font_from_file_ttf

    def get_tex_data_as_rgba32(self):
        return 1, 1, bytes(4)

    get_tex_data_as_alpha8 = get_tex_data_as_rgba32

    def __getattr__(self, name):
        return quiet


class Colors(dict):
    """Style colors the app set: pushed around each of its draws, read back from the host otherwise."""

    def __init__(self, real):
        super().__init__()
        self.real = real

    def __missing__(self, index):
        return self.real().colors[index]


class Frame:
    """Draw data of a hosted app: the host draws its frame."""
    commands_lists, valid = (), False

    def scale_clip_rects(self, *scale):
        pass


class Backend:
    """A platform backend (imgui.integrations): the app may subclass it, the host does its work."""
    io = None       # the app's guest io, set on the per app subclass (Integrations)

    def __init__(self, window=None, *args, **kwargs):
        self.window, self._font_texture = window, None

    def __init_subclass__(cls):
        for name in BACKEND_HOST_CALLS:
            if name in vars(cls):
                setattr(cls, name, quiet)

    def __getattr__(self, name):
        return quiet


class Integrations:
    """imgui.integrations: any module path, any renderer class name is the app's Backend."""

    def __init__(self, io):
        self.io, self.backend = io, None

    def __getattr__(self, name):
        if not name[:1].isupper():
            return self
        if self.backend is None:
            self.__dict__['backend'] = type('Backend', (Backend,), {'io': self.io})
        return self.backend


class Shared(Shim):
    """A Shim of which every app has an instance: real names are kept on the class, once for all."""

    def __getattr__(self, name):
        if name == '__all__':
            return [name for name in dir(self._real) if not name.startswith('_')]
        value = getattr(self._real, name)
        # staticmethod: a plain function kept on the class must not bind to the shim.
        setattr(type(self), name, staticmethod(value) if isinstance(value, types.FunctionType) else value)
        return value


class Imgui(Shared):
    """ImGui for a hosted app: the context, frame and window belong to the host (IMGUI_HOST_CALLS)."""
    begin = staticmethod(lambda *args, **kwargs: (True, True))
    get_draw_data = Frame

    _pack = None    # hdr_color.style_color, bound with the first hosted app

    def __init__(self, app):
        if Imgui._pack is None:
            from meltygui import hdr_color
            Imgui._pack = staticmethod(hdr_color.style_color)
            Imgui.get_color_u32_rgba = staticmethod(hdr_color.pack_color)
        super().__init__(sys.modules['meltygui_imgui'], _app=app)

    # Its io, style and backends are built when the app first asks: most frames never do.
    def get_io(self):
        io = self.__dict__.get('_io')
        if io is None:
            io = self.__dict__['_io'] = Io(self._real.get_io, fonts=Fonts(), _app=self._app)
        return io

    def get_style(self):
        style = self.__dict__.get('_style')
        if style is None:
            colors = self._app.colors = Colors(self._real.get_style)
            style = self.__dict__['_style'] = Guest(self._real.get_style, colors=colors)
        return style

    @property
    def integrations(self):
        made = self.__dict__.get('_integrations')
        if made is None:
            made = self.__dict__['_integrations'] = Integrations(self.get_io())
        return made

    def new_frame(self):
        self._app.namespace[DRAWN] = True

    def begin_child(self, *args, **kwargs):
        # Workaround: the capture resets a child window's draw list without its base clip rect and
        # font texture (meltygui-imgui retained_capture.h, detach_child), so a view that runs twice
        # in one frame asserts in end_child. Remove these pushes once the binding restores them.
        imgui = self._real
        visible = imgui.begin_child(*args, **kwargs)
        self._app.children += 1
        draw = imgui.get_window_draw_list()
        (x, y), (width, height) = imgui.get_window_position(), imgui.get_window_size()
        draw.push_clip_rect(x, y, x + width, y + height, True)
        draw.push_texture_id(imgui.get_io().fonts.texture_id)
        return visible

    def end_child(self):
        self._app.children -= 1
        self._real.end_child()

    # The host packs colours its own way (hdr_color.py); an app's ordinary sRGB floats are
    # translated where they enter ImGui. Lever: a colour call missing here reads too opaque.
    def push_style_color(self, index, r, g, b, a=1.0):
        self._real.push_style_color(index, *self._pack(r, g, b, a))

    def text_colored(self, text, r, g, b, a=1.0):
        self._real.text_colored(text, *self._pack(r, g, b, a))

    def push_font(self, font):
        real = not isinstance(font, Font)
        self.__dict__.setdefault('_pushed', []).append(real)
        if real:
            self._real.push_font(font)

    def pop_font(self):
        if self.__dict__['_pushed'].pop():
            self._real.pop_font()


for _name in IMGUI_HOST_CALLS:
    setattr(Imgui, _name, staticmethod(quiet))


class Glfw(types.ModuleType):
    """GLFW with the view as its one window: constants are real, the rest succeeds and does nothing."""
    _real = UNSET       # the real module, for its constants: imported with the first hosted app that uses GLFW
    init = staticmethod(lambda: True)
    get_window_content_scale = staticmethod(lambda window: (1.0, 1.0))

    def __init__(self, app):
        super().__init__('glfw')
        if Glfw._real is UNSET:
            try:
                Glfw._real = importlib.import_module('glfw')
            except ImportError:
                Glfw._real = None
        self.__dict__.update(_app=app, _started=time.perf_counter())

    def poll_events(self):
        self._app.wait = 0.0
        return WAITING

    def wait_events(self):
        if self._app.wait is None:
            self._app.wait = float('inf')
        return WAITING

    def wait_events_timeout(self, seconds):
        app = self._app
        app.wait = seconds if app.wait is None else min(app.wait, seconds)
        return WAITING

    def post_empty_event(self):
        wake = self._app.wake
        if wake is not None:
            wake()

    def window_should_close(self, window):
        return self._app.closing

    def set_window_should_close(self, window, value):
        self._app.closing = bool(value)

    def get_time(self):
        return time.perf_counter() - self._started

    def get_window_size(self, window):
        return self._app.extent()

    get_framebuffer_size = get_window_size

    def get_cursor_pos(self, window=None):
        import meltygui_imgui as imgui
        return tuple(imgui.get_mouse_pos())

    def get_window_attrib(self, window, which):
        # Lever: the view counts as hovered and focused while the pointer is on it, so an app
        # that takes the keyboard when focused holds a live text field only then.
        if which in (getattr(self._real, 'HOVERED', None), getattr(self._real, 'FOCUSED', None)):
            (x, y), (width, height) = self.get_cursor_pos(), self._app.extent()
            return 0 <= x < width and 0 <= y < height
        return True

    def __getattr__(self, name):
        if name.startswith('set_') and name.endswith('_callback'):
            refresh = self._app.refresh

            def value(window, function):
                if name == 'set_window_refresh_callback' and function is not None:
                    refresh.append(function)
                return quiet
            self.__dict__[name] = value
            return value
        value = getattr(self._real, name, None)
        value = NULL if value is None or callable(value) else value
        setattr(Glfw, name, value)      # the same for every app: looked up once
        return value


# code object: (the view values it declares, its parameters that may be injected state). Every
# instance of a file has the same window functions: their signature is read once.
_plans = {}


def window_frame(function):
    """How the hosting view calls a window function: `frame(draw_state, cache, values)`.

    The function gets what it declares of `width`, `height`, `draw_state` and
    `cache` from the hosting view, and a state object of its own for each
    parameter annotated with a DictConversion class (not saved with the session).
    The size is read from `values`, the view's own kwargs: a draw_state attribute
    costs a native call (about 0.2 us each).
    """
    plan = _plans.get(function.__code__)
    if plan is None:
        declared = inspect.signature(function).parameters
        wants = tuple(name for name in ('width', 'height', 'draw_state', 'cache') if name in declared)
        states = tuple(name for name, parameter in declared.items()
                       if parameter.default is None and parameter.annotation is not parameter.empty)
        plan = _plans[function.__code__] = (wants, states)
    wants, states = plan
    fixed = {}
    if states:      # annotated with a class of this instance's own namespace
        from meltygui.core.conversion.dict_conversion import DictConversion
        from meltygui.core.rendering.injected_state import parameter_annotations
        annotations = parameter_annotations(function)
        fixed = {name: annotations[name]() for name in states
                 if inspect.isclass(annotations.get(name)) and issubclass(annotations[name], DictConversion)}
    if not fixed and not wants:
        return lambda draw_state, cache, values: function()
    if not fixed and wants == ('width', 'height'):
        return lambda draw_state, cache, values: function(width=values['width'], height=values['height'])

    def frame(draw_state, cache, values):
        arguments = dict(fixed)
        for name in wants:
            arguments[name] = draw_state if name == 'draw_state' else cache if name == 'cache' else values[name]
        return function(**arguments)
    return frame


class Melty(Shared):
    """meltygui for a hosted meltygui app: its window functions are the hosting view's frame, the loop is the host's."""
    run = boot = staticmethod(quiet)

    def __init__(self, app):
        import meltygui
        super().__init__(meltygui, _app=app)

    def os_window(self, function=None, **decoration):
        if function is None:
            return lambda function: self.os_window(function, **decoration)
        app = self._app
        if decoration.get('on_close'):
            app.exits.append(decoration['on_close'])
        app.roots.append(window_frame(function))
        return function

    glfw_window = os_window

    def gui(self, function=None, *, glfw_window=False, **decoration):
        return self.os_window(function, **decoration) if glfw_window else gui(function, **decoration)


def joined(loop):
    while loop.gi_frame is not None:
        yield


def thread_shim(app):
    frames = app.frames

    class Thread(threading.Thread):
        """A thread that would run the frame loop hands it to the render thread instead."""
        loop = None

        def start(self):
            run = next((run for run in (self.run, self._target) if inspect.isgeneratorfunction(run)), None)
            if run is None:
                return super().start()
            self.loop = run() if run is not self._target else run(*self._args, **self._kwargs)
            frames.append(self.loop)

        def join(self, timeout=None):
            return super().join(timeout) if self.loop is None else joined(self.loop)

        def is_alive(self):
            return super().is_alive() if self.loop is None else self.loop.gi_frame is not None

    return Shim(threading, Thread=Thread)


def register(app):
    def register(function, *args, **kwargs):
        app.exits.append(functools.partial(function, *args, **kwargs))
        return function
    return Shim(atexit, register=register)


# Lever: what a hosted module, and the files beside it, get when they import these names:
# name: (make(app), None) for a shim each app has its own of, (None, shim()) for a shared one.
SHIMS = {'imgui': (Imgui, None), 'meltygui_imgui': (Imgui, None), 'meltygui': (Melty, None),
         'glfw': (Glfw, None), 'threading': (thread_shim, None), 'atexit': (register, None),
         'sys': (lambda app: Passing(sys, argv=[app.program.code.co_filename]), None),
         'OpenGL': (None, gl_shim), 'signal': (None, signal_shim)}


def load(name, globals=None, locals=None, fromlist=(), level=0):
    """`__import__` of every hosted namespace: the importing module's globals name its app."""
    app = globals.get(APP) if globals else None
    if app is None or level:
        return builtins.__import__(name, globals, locals, fromlist, level)
    top, _, rest = name.partition('.')
    shim = SHIMS.get(top)
    if shim is not None:
        make, shared = shim
        if name not in sys.modules or top == 'imgui':     # the real module, for what the shim passes through
            real = 'meltygui_imgui' + name[5:] if top == 'imgui' else name
            if real not in sys.modules and top not in ('glfw', 'sys'):
                importlib.import_module(real)
        if make is None:
            module = shared()
        else:
            module = app.made.get(make)
            if module is None:
                module = app.made[make] = make(app)
        for part in rest.split('.') if fromlist and rest else ():
            module = getattr(module, part)
        if fromlist and isinstance(module, types.ModuleType) and not isinstance(module, Shim):
            # A real submodule: the real import also loads the submodules `from` names.
            return builtins.__import__(module.__name__, globals, locals, fromlist, 0)
        return module
    if not rest:
        # A file next to the app is part of it: one copy per instance, on the same shims.
        module = app.modules.get(top)
        if module is not None:
            return module
        known = app.program.beside
        if not fromlist and known.get(top, UNSET) is None:
            module = sys.modules.get(name)      # an ordinary module that is loaded: no import machinery
            if module is not None:
                return module
        path = known.get(top, UNSET)
        if path is UNSET:
            path = os.path.join(os.path.dirname(app.program.code.co_filename), f'{top}.py')
            path = known[top] = path if os.path.isfile(path) else None
        if path is not None:
            code = beside(path)
            module = app.modules[top] = types.ModuleType(top)
            module.__dict__.update({'__file__': path, '__builtins__': SCOPE, APP: app})
            exec(code, module.__dict__)
            return module
    return builtins.__import__(name, globals, locals, fromlist, level)


# The builtins of every hosted namespace: one table for all apps (a copy each was most of an instance).
SCOPE = {**vars(builtins), '__import__': load, DRIVE: drive, WAIT: WAITING}


# Lever: a file whose last load took no longer than this (seconds) loads on the render thread, in
# the frame that first draws it: handing a small module to the loader costs more than running it.
# The first instance of a file, and any slower one, loads in the background (`background=True`).
INLINE = 0.0002
_loads, _loader = queue.SimpleQueue(), []


def load_later(run):
    """Run `run` on the one loader thread: instances load in turn, the render thread only queues."""
    if not _loader or not _loader[0].is_alive():
        def serve():
            while True:
                _loads.get()()
        _loader[:] = [threading.Thread(target=serve, name='gui_module load', daemon=True)]
        _loader[0].start()
    _loads.put(run)


class App:
    """One running instance of a module: its namespaces and what draws its frame."""

    def __init__(self, program):
        self.program, self.parameters, self.first = program, program.parameters, program.first
        # What one view execution calls with (draw_state, cache, values): `draw`, or the window function
        # itself when that is all there is (`start`).
        self.run = self.draw
        # frames: its own frame loops (generators); roots: a meltygui app's window functions (window_frame).
        self.frames, self.roots, self.exits, self.refresh, self.modules = [], [], [], [], {}
        self.colors, self.made = {}, {}     # made: its own shims, by maker (SHIMS)
        # wake: thread safe, from the app's threads and timers; again: the next frame, from its own draw.
        # wait: seconds asked by an event wait that went through its shim, until `step` takes it.
        # state: the hosting view's draw_state, where the app's window size is read (extent).
        self.namespace = self.error = self.wake = self.again = self.wait = self.timer = self.state = None
        # ready: loaded without error and with something to draw. follow: its polls ask for the next frame.
        self.loaded = self.ready = self.closing = self.follow = False
        self.children = 0       # child windows the app has open

    def extent(self):
        """The view's size, as the app's window size."""
        state = self.state
        return (1, 1) if state is None else (max(int(state.width), 1), max(int(state.height), 1))

    def fail(self):
        self.error, self.ready = traceback.format_exc(), False

    def start(self, args, wake=None, again=None, background=False):
        """Run the module up to where its frame loop starts. With `background`, on the loader
        thread unless the file is known to load fast (INLINE); `wake` then reports the end."""
        program = self.program
        self.namespace = {'__name__': '__main__', '__file__': program.code.co_filename,
                          ARGS: args, APP: self, DRAWN: False, '__builtins__': SCOPE}
        self.wake, self.again = wake, again
        if background and (program.seconds is None or program.seconds > INLINE):
            load_later(self.load_module)
        else:
            self.load_module(None)

    def load_module(self, wake=UNSET):
        """The module's own code, timed; `wake` defaults to the app's (a background load reports its end)."""
        program, namespace, started = self.program, self.namespace, time.perf_counter()
        try:
            exec(program.code, namespace)
            if MAIN in namespace:
                self.frames.insert(0, namespace[MAIN]())
            elif not self.frames and not self.roots:
                raise SplitError(f'{program.code.co_filename}: nothing to draw (no frame loop calling '
                                 f'{" / ".join(WAIT_CALLS)} and no @os_window)')
            if not self.frames and len(self.roots) == 1:
                self.run = self.roots[0]
            self.ready = True
        except BaseException:
            self.fail()
        program.seconds = time.perf_counter() - started
        self.loaded = True
        if wake is UNSET:
            wake = self.wake
        if wake is not None:
            wake()

    def step(self, stop=False):
        """Each frame loop runs to its next event wait; `stop` makes that wait end its loop.
        Returns the shortest wait they asked for, in seconds (None: none of them said)."""
        frames, wait = self.frames, None
        for loop in tuple(frames):
            try:
                if loop.gi_suspended:
                    asked = loop.send(stop)
                elif stop:              # never started: nothing of it to end
                    loop.close()
                    frames.remove(loop)
                    continue
                else:
                    asked = next(loop)
            except (StopIteration, SystemExit, KeyboardInterrupt):
                frames.remove(loop)
            except Exception:
                frames.remove(loop)
                self.fail()
            else:
                if asked is not None and (wait is None or asked < wait):
                    wait = asked
        if self.wait is not None:       # a wait that went through its shim left its time here
            wait, self.wait = (self.wait if wait is None else min(wait, self.wait)), None
        if not frames and not self.roots:
            self.ready = False          # the app ended its own loop: nothing more to draw
        return wait

    def draw(self, draw_state, cache, values=None):
        """One view execution: window functions run; a frame loop runs to its next event wait."""
        for frame in self.roots:
            frame(draw_state, cache, values)
        if self.frames:
            colors, namespace, pushed = self.colors, self.namespace, 0
            if colors:
                import meltygui_imgui as imgui
                from meltygui.hdr_color import style_color
                for index, value in colors.items():
                    imgui.push_style_color(index, *style_color(*value))
                pushed = len(colors)    # counted now: the app's setup may set colours during this run
            try:
                wait = self.step()
                # Setup, or an idle iteration, drew nothing and the view needs a frame: the app
                # is told its window wants a redraw, as GLFW would, and runs once more.
                if not namespace[DRAWN] and self.ready:
                    for refresh in self.refresh:
                        refresh(None)
                    wait = self.step()
                namespace[DRAWN] = False
            finally:
                if self.children:
                    # The app raised inside a child window: close it, so the error shows instead of the host asserting.
                    import meltygui_imgui as imgui
                    while self.children:
                        self.children -= 1
                        imgui.end_child()
                if pushed:
                    imgui.pop_style_color(pushed)
            if wait is not None and (wait > 0 or self.follow) or self.timer is not None:
                self.sleep(wait)
            return wait

    def sleep(self, wait):
        """Ask the host for the frame the app's wait of `wait` seconds wants. Lever: `live` (gui_module)."""
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.wake is None or wait is None or not self.frames:
            return
        if wait <= 0:
            if self.follow:
                self.again()
        elif wait != float('inf'):
            self.timer = threading.Timer(wait, self.wake)
            self.timer.daemon = True
            self.timer.start()

    def close(self):
        """Terminate: end the loops, run the module's cleanup, release its namespaces."""
        if self.namespace is None or not self.loaded:
            return
        if self.timer is not None:
            self.timer.cancel()
        self.closing = True
        # A loop waiting on another (join) ends on the pass after it.
        for _ in range(len(self.frames) + 1):
            self.step(stop=True)
        for loop in self.frames:
            loop.close()
        for leave in reversed(self.exits):
            try:
                leave()
            except Exception:
                self.fail()
        for module in self.modules.values():
            module.__dict__.clear()
        self.namespace.clear()
        self.__init__(self.program)


def differs(before, after):
    if before is after:
        return False
    try:
        return bool(before != after)
    except (TypeError, ValueError):
        return True


# (path, options): view. The same file with the same options is one view function, so a module
# that hosts itself, or is hosted from many places, binds once.
_views = {}


def gui_module(path, *, background=True, live=None, **decoration):
    """A @gui view that runs the standalone ImGui app at `path`.

    Call it like any view: `view(value, name=..., <parameter>=...)`: each keyword
    goes to the module global of that name, `value` to the file's first one. Every
    call site (name / key) is its own running app. `live`: None follows the app
    (see the module text), True redraws every frame, False only on the host's
    invalidation and the app's timed waits. `decoration` is @gui's.
    """
    if type(path) is not str:
        path = str(path)
    try:
        known = (path, background, live, *decoration.items())
        view = _views.get(known)
    except TypeError:       # an unhashable option: this view is its own
        known = view = None
    if view is not None:
        return view
    label = os.path.basename(path)
    decoration.setdefault('state_id', f'gui_module:{path}')
    no_save = (*decoration.pop('no_save', ()), 'app')
    inject = {**decoration.pop('inject', {}), 'app': lambda: App(program(path))}

    def start(app, values, input_value, cache):
        """First call, or the first one after the view was retired: a fresh run."""
        chosen = app.program
        args = {name: values[name] for name in chosen.parameters if name in values}
        if input_value is not None and chosen.first is not None:
            args[chosen.first] = input_value
        wake = again = None
        if cache is not None and cache.stack:     # an immediate view (use_cache=False) has no node
            node = cache.current
            cache.on_retire(node, app.close)
            wake, again = functools.partial(cache.wake, node), functools.partial(cache.wake_next, node)
        app.follow = chosen.blocks if live is None else bool(live)
        app.start(args, wake, again, background and wake is not None)

    # The per run path: keep it to dictionary stores and one call into the app.
    @gui(inject=inject, no_save=no_save, **decoration)
    def view(input_value=None, draw_state=None, cache=None, app=None, **values):
        namespace = app.namespace
        if namespace is None:
            start(app, values, input_value, cache)
            namespace = app.namespace
        if app.ready:
            # **values also carries the view's own named values (VIEW_VALUES), which are not parameters.
            for name in app.parameters:
                if name in values:
                    namespace[name] = values[name]
            first = app.first
            if input_value is not None and first is not None:
                namespace[first] = input_value
            before = namespace.get(first)
            app.state = draw_state
            try:
                app.run(draw_state, cache, values)
            except Exception:
                app.fail()
            if app.error is None:
                after = namespace.get(first)
                if after is not before and differs(before, after):
                    return True, after
                return
        import meltygui_imgui as imgui
        if not app.loaded:
            imgui.text(f'Loading {label}')
        elif app.error is not None:
            imgui.text(f'{label} failed')
            imgui.text(app.error)

    if known is not None:
        _views[known] = view
    return view
