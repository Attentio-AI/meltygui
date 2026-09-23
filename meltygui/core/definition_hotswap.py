"""Keep live definitions canonical when their source moves between modules."""
import inspect
import gc
import sys
import types

from meltygui.core.melty import Melty


def bind_class_namespace(cls, compiled_namespace, live_namespace):
    """Class-only compilation isolates bindings, but methods use live globals.

    A temporary exec dictionary is not a new module. Leaving methods attached
    to it freezes global values and makes module_is_live(globals()) false.
    Only fresh functions from that dictionary are rebound, including wrappers.
    """
    rebound = {}

    def function(value):
        if not isinstance(value, types.FunctionType):
            return value
        if id(value) in rebound:
            return rebound[id(value)]
        result = value
        if value.__globals__ is compiled_namespace:
            result = types.FunctionType(value.__code__, live_namespace, value.__name__,
                                        value.__defaults__, value.__closure__)
        rebound[id(value)] = result
        if result is not value:
            result.__dict__.update(value.__dict__)
            result.__kwdefaults__ = value.__kwdefaults__
            result.__annotations__ = value.__annotations__
            result.__qualname__, result.__module__ = value.__qualname__, value.__module__
            result.__doc__ = value.__doc__
        for cell in result.__closure__ or ():
            try:
                held = cell.cell_contents
            except ValueError:
                continue
            if isinstance(held, types.FunctionType):
                cell.cell_contents = function(held)
        if isinstance(result.__dict__.get('__wrapped__'), types.FunctionType):
            result.__wrapped__ = function(result.__wrapped__)
        return result

    for name, value in tuple(vars(cls).items()):
        if isinstance(value, types.FunctionType):
            setattr(cls, name, function(value))
        elif isinstance(value, (staticmethod, classmethod)):
            setattr(cls, name, type(value)(function(value.__func__)))
        elif isinstance(value, property):
            setattr(cls, name, property(function(value.fget), function(value.fset),
                                        function(value.fdel), value.__doc__))
        elif (isinstance(value, type)
              and value.__qualname__.startswith(cls.__qualname__ + '.')):
            bind_class_namespace(value, compiled_namespace, live_namespace)


def _patch_nested_functions(previous, replacement, namespace):
    """Update existing factory-created callbacks without recreating closures."""
    def nested_codes(code):
        result = {}
        for child in code.co_consts:
            if isinstance(child, types.CodeType):
                if not child.co_name.startswith("<"):
                    result[child.co_qualname] = child
                result.update(nested_codes(child))
        return result

    fresh = nested_codes(replacement)
    if not fresh:
        return lambda: None
    edits = []
    # Earlier factory-only swaps can leave callbacks more than one code
    # generation behind. Match their defining namespace, source and qualified
    # local name rather than only referrers of the immediately previous code.
    for function in gc.get_objects():
        if type(function) is not types.FunctionType or function.__globals__ is not namespace:
            continue
        old_code = function.__code__
        new_code = fresh.get(old_code.co_qualname)
        if (new_code is None or new_code is old_code
                or old_code.co_filename != previous.co_filename):
            continue
        if old_code.co_freevars != new_code.co_freevars:
            raise ValueError(f"cannot preserve live closure for {old_code.co_qualname}: free variables changed")
        edits.append((function, old_code, new_code))
    for function, _old_code, new_code in edits:
        function.__code__ = new_code

    def restore():
        for function, old_code, _new_code in edits:
            function.__code__ = old_code
    return restore


def patch_function(live, replacement, *, force=False):
    """Patch in place, retaining the defining namespace after a source move.

    Python cannot reassign a function's __globals__. For a relocated function,
    its original identity becomes a small forwarding entry point; __wrapped__
    exposes the actual source implementation to inspection and later edits.
    Normal edits still patch code directly and pay no forwarding cost.
    """
    previous = (live.__code__, live.__defaults__, live.__kwdefaults__,
                dict(live.__annotations__), live.__doc__, live.__module__,
                live.__qualname__, dict(live.__dict__))
    previous_target = Melty.relocated_functions.get(id(live))
    target = inspect.unwrap(live) if live.__dict__.get('__melty_relocated__') else live
    target_previous = (target.__code__, target.__defaults__, target.__kwdefaults__,
                       dict(target.__annotations__), target.__doc__, target.__module__,
                       target.__qualname__)

    if force and replacement.__globals__ is live.__globals__:
        target = live
        live.__dict__.pop('__wrapped__', None)
        live.__dict__.pop('__melty_relocated__', None)
        Melty.relocated_functions.pop(id(live), None)

    restore_nested = None
    if target.__globals__ is replacement.__globals__:
        restore_nested = _patch_nested_functions(target.__code__, replacement.__code__, target.__globals__)
        target.__code__ = replacement.__code__
        target.__defaults__ = replacement.__defaults__
        target.__kwdefaults__ = replacement.__kwdefaults__
        target.__annotations__ = replacement.__annotations__
        target.__doc__ = replacement.__doc__
        target.__module__ = replacement.__module__
        target.__qualname__ = replacement.__qualname__
    else:
        # Keep existing lexical state when the free-variable contract survives.
        implementation = replacement
        if target.__code__.co_freevars and target.__code__.co_freevars == replacement.__code__.co_freevars:
            implementation = types.FunctionType(
                replacement.__code__, replacement.__globals__, replacement.__name__,
                replacement.__defaults__, target.__closure__)
            implementation.__kwdefaults__ = replacement.__kwdefaults__
            implementation.__annotations__ = replacement.__annotations__
            implementation.__doc__ = replacement.__doc__
            implementation.__qualname__ = replacement.__qualname__
        Melty.relocated_functions[id(live)] = implementation
        captures = [f'capture_{index}' for index in range(len(live.__closure__ or ()))]
        capture_line = f'        ({", ".join(captures)},)\n' if captures else ''
        source = (
            f'def make_entry({", ".join(captures)}):\n'
            '    def entry(*args, **kwargs):\n'
            + capture_line
            + '        from meltygui.core.melty import Melty\n'
            + f'        return Melty.relocated_functions[{id(live)}](*args, **kwargs)\n'
            + '    return entry\n')
        namespace = {}
        exec(compile(source, '<melty definition relocation>', 'exec'), namespace)
        entry = namespace['make_entry'](*([None] * len(captures)))
        live.__code__ = entry.__code__
        live.__defaults__ = None
        live.__kwdefaults__ = None
        live.__wrapped__ = implementation
        live.__melty_relocated__ = True
        live.__annotations__ = replacement.__annotations__
        live.__doc__ = replacement.__doc__
    live.__module__ = replacement.__module__
    live.__qualname__ = replacement.__qualname__

    def restore():
        if restore_nested is not None:
            restore_nested()
        (live.__code__, live.__defaults__, live.__kwdefaults__, annotations,
         live.__doc__, live.__module__, live.__qualname__, attributes) = previous
        live.__annotations__ = annotations
        live.__dict__.clear()
        live.__dict__.update(attributes)
        if target is not live:
            (target.__code__, target.__defaults__, target.__kwdefaults__, annotations,
             target.__doc__, target.__module__, target.__qualname__) = target_previous
            target.__annotations__ = annotations
        if previous_target is None:
            Melty.relocated_functions.pop(id(live), None)
        else:
            Melty.relocated_functions[id(live)] = previous_target
    return restore


def canonicalize_definitions(replacements):
    """Rebind exports and type-bearing function metadata to the live objects.

    Only exact object identities are replaced, never names shared by unrelated
    modules. Runtime instances and arbitrary application object graphs are not
    traversed. Closure metadata matters for render_func's injection plans and
    for zero-argument super() in relocated methods.
    """
    # Keep the original alive too: replaced tuples/signatures can otherwise
    # be freed mid-walk and their ids reused by later function metadata.
    seen = {}

    def replace(value):
        pair = replacements.get(id(value))
        if pair is not None and pair[0] is value:
            return pair[1]
        identity = id(value)
        if identity in seen:
            return seen[identity][1]
        seen[identity] = (value, value)
        if isinstance(value, dict):
            for key, item in list(value.items()):
                new_key, new_item = replace(key), replace(item)
                if new_key is not key:
                    del value[key]
                if new_key is not key or new_item is not item:
                    value[new_key] = new_item
        elif isinstance(value, list):
            for index, item in enumerate(value):
                value[index] = replace(item)
        elif isinstance(value, tuple):
            result = tuple(replace(item) for item in value)
            if any(a is not b for a, b in zip(result, value)):
                seen[identity] = (value, result)
                return result
        elif isinstance(value, inspect.Parameter):
            result = value.replace(annotation=replace(value.annotation), default=replace(value.default))
            seen[identity] = (value, result)
            return result
        elif isinstance(value, inspect.Signature):
            result = value.replace(parameters=[replace(p) for p in value.parameters.values()],
                                   return_annotation=replace(value.return_annotation))
            seen[identity] = (value, result)
            return result
        return value

    visited_functions = set()

    def function_metadata(function):
        if id(function) in visited_functions:
            return
        visited_functions.add(id(function))
        function.__annotations__ = replace(function.__annotations__)
        function.__defaults__ = replace(function.__defaults__)
        function.__kwdefaults__ = replace(function.__kwdefaults__)
        for cell in function.__closure__ or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            replacement = replace(value)
            if replacement is not value:
                cell.cell_contents = replacement
            if isinstance(replacement, types.FunctionType):
                function_metadata(replacement)
        wrapped = function.__dict__.get('__wrapped__')
        if isinstance(wrapped, types.FunctionType):
            function_metadata(wrapped)

    def definition_metadata(value):
        if isinstance(value, types.FunctionType):
            function_metadata(value)
        elif isinstance(value, type):
            # A re-executed subclass points at freshly compiled bases. Keep its
            # inheritance on the same live definitions as imports and closures.
            bases = tuple(replace(base) for base in value.__bases__)
            if bases != value.__bases__:
                value.__bases__ = bases
            for member in vars(value).values():
                if isinstance(member, types.FunctionType):
                    function_metadata(member)
                elif isinstance(member, (staticmethod, classmethod)):
                    function_metadata(member.__func__)
                elif isinstance(member, property):
                    for function in (member.fget, member.fset, member.fdel):
                        if function is not None:
                            function_metadata(function)

    for fresh, live in replacements.values():
        definition_metadata(fresh)
        definition_metadata(live)
    for module in tuple(sys.modules.values()):
        if not isinstance(module, types.ModuleType):
            continue
        touched = False
        for name, value in tuple(vars(module).items()):
            pair = replacements.get(id(value))
            if pair is not None and pair[0] is value:
                vars(module)[name] = pair[1]
                touched = True
        if touched:
            for value in tuple(vars(module).values()):
                definition_metadata(value)
