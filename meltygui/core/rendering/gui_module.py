"""A standalone ImGui app, a module with its own window and frame loop, as a @gui view.

    draw_counter = gui_module('counter_app.py', width=420, height=160)
    changed, text = draw_counter('Not hello', name='left')      # in any @gui body

The module is not imported: each view instance runs it in a namespace of its
own, so its globals are that instance's state. A top level `name = <literal>`
is a parameter: a value passed to the call replaces the global on every frame
it is passed, an omitted one leaves the global to the module. No name is
special: `view(title='x')` sets the module's `title`. The call's positional
value goes to the first parameter in the file, and a change to that one is
what the call returns. A parameter named like one of the
view's own values (`width`, `height`, `name`, `tint`) receives that value.

How the module becomes lifecycle code (`split`):
- The frame loop is the one `while` that calls FRAME_CALLS. It gets a `yield`
  at the top of each iteration, so the code around it is a generator: up to the
  loop is load, one iteration is a draw, what follows the loop (and every
  `finally` / `with` around it) is terminate. Locals stay locals.
- Functions that reach the loop, and the module's own tail from the statement
  that starts it, are generators too (`yield from`), so the loop can sit in
  `main()`, behind `if __name__ == '__main__':` or in a method.
- A thread whose target holds the loop is not started; its generator runs on
  the render thread. Other threads are real.
- Windowing is not removed but imported from shims (`App.shims`): GLFW, the
  ImGui context / frame / backend calls, GL clears, signal and atexit handlers.

The module's statements before the loop starts run on a background thread
(`background=False`: on the render thread, for a module that needs GL there);
the launch function's own setup runs on the render thread with the first draw.

Gaps: edits to the file are not picked up by a running instance, `sys.argv` is
not a parameter source, and in-place edits of a mutable first parameter are not
reported as changed.
"""
import ast
import atexit
import builtins
import functools
import inspect
import signal
import threading
import time
import traceback
import types
from collections import namedtuple
from pathlib import Path

from meltygui.core.rendering.gui_prototype import gui

# Lever: the calls that mark a `while` as the app's frame loop.
FRAME_CALLS = frozenset(('new_frame', 'swap_buffers', 'poll_events'))
# Lever: the host owns these; a hosted module's calls to them do nothing.
# The view is the module's window: its begin / end and window placement are dropped too.
IMGUI_HOST_CALLS = ('create_context', 'destroy_context', 'set_current_context', 'new_frame', 'end_frame',
                    'render', 'get_draw_data', 'end', 'set_next_window_position', 'set_next_window_size')
GL_HOST_CALLS = ('glClear', 'glClearColor', 'glViewport')

MAIN, DRIVE, ARGS = '__gui_main__', '__gui_drive__', '__gui_args__'
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
          ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

# code: the rewritten module; parameters: {name: default} of its top level literals.
Program = namedtuple('Program', 'code parameters')


class SplitError(ValueError):
    """The module has no single frame loop this wrapper can drive."""


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


class Drive(ast.NodeTransformer):
    """In one scope, run the calls that may hold the frame loop as `yield from`."""

    def __init__(self, names):
        self.names = names

    def visit_Call(self, node):
        self.generic_visit(node)
        if called(node) not in self.names:
            return node
        driven = ast.Call(ast.Name(DRIVE, ast.Load()), [node], [])
        return ast.copy_location(ast.YieldFrom(driven), node)

    def keep(self, node):
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = visit_Lambda = keep
    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = keep


def split(source, filename='<gui_module>'):
    """Module source -> Program: its frame loop made resumable, its literals made parameters."""
    tree = ast.parse(source, filename)
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def reach(names):
        """The names, plus every function that calls one of them."""
        names, size = set(names), -1
        while len(names) != size:
            size = len(names)
            names.update(function.name for function in functions if calls(function) & names)
        return names

    def inside(node, kinds):
        while node in parents:
            node = parents[node]
            if isinstance(node, kinds):
                return node

    frames = reach(FRAME_CALLS)
    loops = [node for node in ast.walk(tree) if isinstance(node, ast.While)
             and any(calls(statement) & frames or called(getattr(statement, 'value', None)) in frames
                     for statement in node.body)]
    loops = [loop for loop in loops if not any(inside(loop, ast.While) is other for other in loops)]
    if len(loops) != 1:
        found = ', '.join(f'line {loop.lineno}' for loop in loops) or 'none'
        raise SplitError(f'{filename}: expected one frame loop (a while calling {sorted(FRAME_CALLS)}), found {found}')
    loop = loops[0]
    owner = inside(loop, SCOPES)
    if owner is not None and not isinstance(owner, ast.FunctionDef):
        raise SplitError(f'{filename} line {loop.lineno}: the frame loop must be in a plain function or at module level')

    # The resume point: one iteration per step, and a true value sent in ends the loop.
    stop = ast.parse('if (yield): break').body[0]
    loop.body.insert(0, ast.copy_location(stop, loop))

    colored = reach({owner.name}) if owner else set()
    drive = Drive(colored | {'join'})
    for function in functions:
        if function.name in colored:
            function.body = [drive.visit(statement) for statement in function.body]

    def starts(statement):
        if isinstance(statement, SCOPES):
            return False
        return statement is loop or any(node is loop or called(node) in drive.names for node in own(statement))

    start = next((index for index, statement in enumerate(tree.body) if starts(statement)), len(tree.body))
    head, tail = tree.body[:start], tree.body[start:]

    parameters = {}
    for statement in head:
        if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)):
            name = statement.targets[0].id
            if name.startswith('_') or name in parameters:
                continue
            try:
                parameters[name] = ast.literal_eval(statement.value)
            except ValueError:
                continue
            # name = <args>.get('name', <literal>): load code sees the caller's value.
            take = ast.parse(f'{ARGS}.get({name!r}, 0)', mode='eval').body
            take.args[1] = statement.value
            statement.value = ast.copy_location(take, statement.value)

    if tail:
        names = sorted(bound(tail))
        body = ([ast.Global(names)] if names else []) + [drive.visit(statement) for statement in tail]
        main = ast.FunctionDef(name=MAIN, args=ast.parse('lambda: 0', mode='eval').body.args, body=body,
                               decorator_list=[], type_params=[])
        tree.body = head + [ast.copy_location(main, tail[0])]
    return Program(compile(ast.fix_missing_locations(tree), filename, 'exec'), parameters)


def drive(value):
    """What a rewritten call returns: a generator holding the frame loop runs in place."""
    if inspect.isgenerator(value):
        return (yield from value)
    return value


class Null:
    """A window, a backend: every attribute and every call gives it back."""

    def __getattr__(self, name):
        return self

    def __call__(self, *args, **kwargs):
        return self


class Shim(types.ModuleType):
    """A real module with some names replaced; star imports still see the real names."""

    def __init__(self, real, **replaced):
        super().__init__(real.__name__)
        self.__dict__.update(replaced, _real=real)

    def __getattr__(self, name):
        if name == '__all__':
            return [name for name in dir(self._real) if not name.startswith('_')]
        return getattr(self._real, name)


def quiet(*args, **kwargs):
    return None


@functools.cache
def imgui_shim():
    import meltygui_imgui
    return Shim(meltygui_imgui, integrations=Null(), begin=lambda *args, **kwargs: (True, True),
                **dict.fromkeys(IMGUI_HOST_CALLS, quiet))


@functools.cache
def gl_shim():
    import OpenGL
    import OpenGL.GL
    return Shim(OpenGL, GL=Shim(OpenGL.GL, **dict.fromkeys(GL_HOST_CALLS, quiet)))


@functools.cache
def signal_shim():
    # The host keeps its handlers; a hosted module ends through its frame loop.
    return Shim(signal, signal=lambda number, handler: signal.SIG_DFL)


class Glfw(types.ModuleType):
    """GLFW without windows: constants are real, functions succeed and do nothing."""

    def __init__(self, size):
        super().__init__('glfw')
        try:
            import glfw
        except ImportError:
            glfw = None
        started = time.perf_counter()
        self.__dict__.update(
            _real=glfw, init=lambda: True, window_should_close=lambda window: False,
            get_time=lambda: time.perf_counter() - started,
            get_window_size=lambda window: tuple(size), get_framebuffer_size=lambda window: tuple(size))

    def __getattr__(self, name):
        value = getattr(self._real, name, None)
        return Null() if value is None or callable(value) else value


def wait(loop):
    while loop.gi_frame is not None:
        yield


def thread_shim(frames):
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
            return super().join(timeout) if self.loop is None else wait(self.loop)

        def is_alive(self):
            return super().is_alive() if self.loop is None else self.loop.gi_frame is not None

    return Shim(threading, Thread=Thread)


class App:
    """One running instance of a module: its namespace and its frame loops."""

    def __init__(self, program):
        self.program = program
        self.frames, self.exits, self.size = [], [], [0, 0]
        self.namespace = self.error = None
        self.loaded = False

    def shims(self):
        """Lever: what a hosted module gets when it imports these names."""
        def register(function, *args, **kwargs):
            self_exits.append(functools.partial(function, *args, **kwargs))
            return function

        self_exits, glfw, threads = self.exits, Glfw(self.size), thread_shim(self.frames)
        return {'imgui': imgui_shim, 'OpenGL': gl_shim, 'signal': signal_shim, 'glfw': lambda: glfw,
                'threading': lambda: threads, 'atexit': lambda: Shim(atexit, register=register)}

    def start(self, args, wake=None):
        """Run the module up to where its frame loop starts; with `wake`, on a background thread."""
        shims = self.shims()

        def load(name, globals=None, locals=None, fromlist=(), level=0):
            top, _, rest = name.partition('.')
            if level or top not in shims:
                return builtins.__import__(name, globals, locals, fromlist, level)
            module = shims[top]()
            for part in rest.split('.') if fromlist and rest else ():
                module = getattr(module, part)
            return module

        namespace = self.namespace = {
            '__name__': '__main__', '__file__': self.program.code.co_filename, ARGS: dict(args),
            '__builtins__': {**vars(builtins), '__import__': load, DRIVE: drive}}

        def run():
            try:
                exec(self.program.code, namespace)
                if MAIN in namespace:
                    self.frames.insert(0, namespace[MAIN]())
            except BaseException:
                self.error = traceback.format_exc()
            self.loaded = True
            if wake is not None:
                wake()

        if wake is None:
            run()
        else:
            # Not kept: a reference back to this App would delay its close to a collection.
            threading.Thread(target=run, name='gui_module load', daemon=True).start()

    def step(self, stop=False):
        """Each frame loop runs one iteration; `stop` ends them and runs what follows them."""
        for loop in list(self.frames):
            try:
                if inspect.getgeneratorstate(loop) == inspect.GEN_CREATED:
                    if stop:
                        loop.close()
                        raise StopIteration
                    next(loop)
                loop.send(stop)
            except (StopIteration, SystemExit, KeyboardInterrupt):
                self.frames.remove(loop)
            except Exception:
                self.frames.remove(loop)
                self.error = traceback.format_exc()

    def close(self):
        """Terminate: end the loops, run the module's cleanup, release its namespace."""
        if self.namespace is None or not self.loaded:
            return
        # A loop waiting on another (join) ends on the pass after it.
        for _ in range(len(self.frames) + 1):
            self.step(stop=True)
        for loop in self.frames:
            loop.close()
        for leave in reversed(self.exits):
            try:
                leave()
            except Exception:
                self.error = traceback.format_exc()
        self.frames.clear()
        self.exits.clear()
        self.namespace.clear()
        self.namespace, self.loaded = None, False

    # For a host without a cache to retire the view; the cache's on_retire is the real path.
    __del__ = close


def differs(before, after):
    if before is after:
        return False
    try:
        return bool(before != after)
    except (TypeError, ValueError):
        return True


def gui_module(path, *, background=True, live=False, **decoration):
    """A @gui view that runs the standalone ImGui app at `path`.

    Call it like any view: `view(value, name=..., <parameter>=...)`: each keyword
    goes to the module global of that name, `value` to the file's first one. Every
    call site (name / key) is its own running app. `live=True` redraws it every
    frame, as its own loop did; without it the view runs when it is invalidated.
    `decoration` is @gui's.
    """
    path = str(path)
    label = Path(path).name

    @functools.cache
    def program():
        return split(Path(path).read_text(), path)

    decoration.setdefault('state_id', f'gui_module:{path}')
    no_save = (*decoration.pop('no_save', ()), 'app')
    inject = {**decoration.pop('inject', {}), 'app': lambda: App(program())}

    @gui(inject=inject, no_save=no_save, **decoration)
    def view(input_value=None, draw_state=None, cache=None, app=None, **values):
        import meltygui_imgui as imgui
        # **values also carries the view's own named values (width, height, name, tint):
        # a module global of the same name receives it, like any other parameter.
        values = {name: values[name] for name in app.program.parameters.keys() & values.keys()}
        first = next(iter(app.program.parameters), None)
        if input_value is not None and first is not None:
            values[first] = input_value
        if app.namespace is None:
            # First call, or the first one after the view was retired: a fresh run.
            if cache is not None:
                cache.on_retire(cache.current, app.close)
            wake = functools.partial(cache.wake, cache.current) if background and cache is not None else None
            app.start(values, wake)
        if not app.loaded:
            imgui.text(f'Loading {label}')
            return
        if app.error is None and app.frames:
            namespace = app.namespace
            namespace.update(values)
            before = namespace.get(first)
            app.size[:] = draw_state.width, draw_state.height
            app.step()
            if live and app.frames and cache is not None:
                cache.wake(cache.current)
            after = namespace.get(first)
            if app.error is None and differs(before, after):
                return True, after
        if app.error is not None:
            imgui.text(f'{label} failed')
            imgui.text(app.error)

    view.split = program
    return view
