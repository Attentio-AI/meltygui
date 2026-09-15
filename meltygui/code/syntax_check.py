"""Pure compiler validation, also loadable inside an isolated interpreter."""

def check_syntax(text, wrap_prefixes):
    if not isinstance(text, str):
        return None
    import textwrap
    dedented = textwrap.dedent(text)
    try:
        compile(dedented, "<editor>", "exec")
        return None
    except SyntaxError as e:
        # Retry inside a function - then an async function - so a statement valid
        # only inside a function body isn't flagged just for missing that context:
        # `return` / `yield` / `yield from` need a `def`, `await` needs `async def`.
        # Clean under EITHER wrapper → not a bug, report nothing. The wrappers are
        # one line, so map a wrapped error's line back by 1.
        indented = textwrap.indent(dedented, "    ")
        wrapped_e = None
        for prefix in wrap_prefixes:
            try:
                compile(prefix + indented, "<editor>", "exec")
                return None
            except SyntaxError as we:
                wrapped_e = we
            except Exception:
                return e
        if wrapped_e is not None and wrapped_e.lineno is not None:
            wrapped_e.lineno = max(1, wrapped_e.lineno - 1)
        return wrapped_e if wrapped_e is not None else e
    except Exception:
        # A non-SyntaxError here (e.g. ValueError on null bytes) isn't the
        # user's code being invalid in a way we can pin to a line - ignore it.
        return None

