"""Keep live definitions canonical when their source moves between modules."""
import inspect
import sys
import types

from meltygui.core.melty import Melty


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

    if target.__globals__ is replacement.__globals__:
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
