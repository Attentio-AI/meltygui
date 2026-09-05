"""Static command inspection. Never execute commands, import code, or read files."""
import ast
import posixpath
import re
import shlex


def shell_body(command):
    """Remove a literal shell -c wrapper, retaining the original script text."""
    for _ in range(3):
        try:
            words = shlex.split(command)
        except ValueError:
            break
        if len(words) >= 3 and posixpath.basename(words[0]) in ('bash', 'sh', 'zsh'):
            flag = next((i for i, word in enumerate(words[1:], 1)
                         if word.startswith('-') and 'c' in word), None)
            if flag is not None and flag + 1 < len(words):
                command = words[flag + 1]
                continue
        break
    return command


def parse_command(command, actions=(), scripts=None, cwd=""):
    """Return literal file references and captured script bodies.

    `access` is an operation requested by the command, not proof it succeeded.
    Unknown variables, substitutions, imports and function calls stay unknown.
    """
    files = {}
    scripts = scripts if scripts is not None else {}

    def add(path, access='read', explicit=False):
        if not isinstance(path, str) or not path or path.startswith('-'):
            return
        if any(char in path for char in '\n\r$*?{}|') or '://' in path:
            return
        if not explicit and not re.fullmatch(r'[\w./@ +\-]+\.[\w]+', path):
            return
        path = posixpath.normpath(posixpath.join(cwd, path))
        old = files.get(path)
        if old is None or access == 'write':
            files[path] = access

    def python(source):
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return
        env = {}

        def resolve(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name):
                return env.get(node.id)
            if isinstance(node, ast.BinOp):
                left, right = resolve(node.left), resolve(node.right)
                if left is not None and right is not None:
                    if isinstance(node.op, ast.Div):
                        return posixpath.join(left, right)
                    if isinstance(node.op, ast.Add):
                        return left + right
            if isinstance(node, ast.Call):
                name = getattr(node.func, 'id', getattr(node.func, 'attr', ''))
                if name in ('Path', 'PurePath', 'PurePosixPath') and node.args:
                    parts = [resolve(arg) for arg in node.args]
                    if all(part is not None for part in parts):
                        return posixpath.join(*parts)
                if name == 'open' and node.args:
                    return resolve(node.args[0])
                if isinstance(node.func, ast.Attribute) and name == 'joinpath':
                    base = resolve(node.func.value)
                    parts = [resolve(arg) for arg in node.args]
                    if base is not None and all(part is not None for part in parts):
                        return posixpath.join(base, *parts)
            return None

        def visit(node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return  # a definition is not evidence that its body ran
            if isinstance(node, ast.Assign):
                value = resolve(node.value)
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        env[target.id] = value
            if isinstance(node, ast.With):
                for item in node.items:
                    if isinstance(item.optional_vars, ast.Name):
                        env[item.optional_vars.id] = resolve(item.context_expr)
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and isinstance(node.iter, (ast.List, ast.Tuple)):
                before = dict(env)
                for value in node.iter.elts[:100]:
                    env[node.target.id] = resolve(value)
                    for child in node.body:
                        visit(child)
                env.clear()
                env.update(before)
                return
            if isinstance(node, ast.Call):
                name = getattr(node.func, 'id', getattr(node.func, 'attr', ''))
                if name == 'open' and node.args:
                    mode = resolve(node.args[1]) if len(node.args) > 1 else 'r'
                    mode = next((resolve(kw.value) for kw in node.keywords if kw.arg == 'mode'), mode)
                    add(resolve(node.args[0]), 'write' if mode and any(c in mode for c in 'wax+') else 'read', True)
                if isinstance(node.func, ast.Attribute):
                    path = resolve(node.func.value)
                    if name in ('write_text', 'write_bytes', 'write', 'writelines', 'unlink', 'touch'):
                        add(path, 'write', True)
                    elif name in ('read_text', 'read_bytes', 'read', 'readlines'):
                        add(path, 'read', True)
                    elif name in ('rename', 'replace') and path is not None and node.args:
                        add(path, 'write', True)
                        add(resolve(node.args[0]), 'write', True)
            for child in ast.iter_child_nodes(node):
                visit(child)
        visit(tree)

    source = shell_body(str(command))
    # Heredocs expose Python directly, or the body of a script written
    # in one history item and run by a later item. Remember only the
    # literal scripts; never consult the filesystem for an indirect script.
    pattern = re.compile(r"^([^\n]*?)<<-?\s*(['\"]?)([A-Za-z_]\w*)\2([^\n]*)\n(.*?)^\3[ \t]*(?:\n|$)", re.M | re.S)
    segments = []
    last = 0
    for match in pattern.finditer(source):
        segments.append(source[last:match.start()])
        head, body = match[1] + match[4], match[5]
        try:
            words = shlex.split(head)
        except ValueError:
            words = []
        if any(re.fullmatch(r'python(?:\d+(?:\.\d+)?)?', posixpath.basename(w)) for w in words[:2]):
            python(body)
        else:
            target = re.search(r'>\s*([^\s]+)', head)
            if target:
                path = target[1].strip("'\"")
                add(path, 'write', True)
                if path.endswith('.py'):
                    scripts[posixpath.normpath(path)] = body
            if 'apply_patch' in head:
                for path in re.findall(r'^\*\*\* (?:Update|Add|Delete) File: (.+)$', body, re.M):
                    add(path, 'write', True)
        segments.append(head + '\n')
        last = match.end()
    segments.append(source[last:])
    source_without_bodies = ''.join(segments)
    try:
        lexer = shlex.shlex(source_without_bodies, posix=True, punctuation_chars=';&|<>\n')
        lexer.whitespace_split = True
        lexer.whitespace = ' \t\r'
        words = list(lexer)
    except ValueError:
        words = []
    groups, group = [], []
    for word in words + [';']:
        if word and all(char in ';|&\n' for char in word):
            if group:
                groups.append(group)
            group = []
        else:
            group.append(word)
    for group in groups:
        executable = posixpath.basename(group[0])
        if re.fullmatch(r'python(?:\d+(?:\.\d+)?)?', executable):
            if '-c' in group and group.index('-c') + 1 < len(group):
                python(group[group.index('-c') + 1])
            else:
                for word in group[1:]:
                    if word.endswith('.py'):
                        add(word)
                        captured = scripts.get(posixpath.normpath(word))
                        if captured is not None:
                            python(captured)
        for index, word in enumerate(group):
            if word in ('>', '>>') and index + 1 < len(group):
                add(group[index + 1], 'write', True)
            elif index > 0:
                access = 'write' if executable in ('tee', 'touch', 'rm', 'mv') or executable == 'sed' and any(w.startswith('-i') for w in group) else 'read'
                if executable in ('cp', 'install') and index == len(group) - 1:
                    access = 'write'
                add(word, access)
    def add_action(path):
        if not isinstance(path, str) or not path:
            return
        canonical = posixpath.normpath(posixpath.join(cwd, path))
        if canonical in files:
            return
        # Providers might report only a suffix for a fully parsed operand.
        # Resolve that suffix only when it identifies exactly one file.
        matches = [known for known in files if known.endswith('/' + posixpath.normpath(path))]
        if not posixpath.isabs(path) and len(matches) == 1:
            return
        add(path, 'read', True)

    for action in actions or ():
        if isinstance(action, dict):
            if action.get('path'):
                add_action(action['path'])
            for path in action.get('paths') or []:
                add_action(path)
    return source, files
