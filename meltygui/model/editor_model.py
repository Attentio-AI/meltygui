"""Background analysis of an editor's text, exposed as plain values.

    analysis = Analysis(path)
    draw_editor(text, state=state, errors=analysis.errors, imports=analysis.imports,
                tints=analysis.tints, code_tree=analysis.tree)
    analysis.feed(text, state.pane.version)     # after every run; cheap

A worker parses and lints the latest text and swaps in new result objects,
then asks for a frame. The view only compares object identity, so results
repaint once when they land. Results that are marks (`tints`) carry the
document version they were computed for, so the editor can bring them
forward over what was typed meanwhile. Nothing here runs on the render thread.
"""
import threading

# Seconds the text must rest before it is analysed.
QUIET = 0.25


class Analysis:
    def __init__(self, path=None, text=''):
        self.path = path
        self.errors, self.imports, self.tints, self.tree = [], {}, None, None
        self.analysed = None       # the text the results belong to
        self.version = None        # the editor's document version of the latest text
        self._text = None
        self._wake = threading.Event()
        self._thread = None
        self.closed = False
        if text:
            self.text = text

    @property
    def text(self):
        return self._text

    @text.setter
    def text(self, value):
        if value is self._text:
            return
        self._text = value
        if self._thread is None:
            self._thread = threading.Thread(target=self._work, name='editor-analysis', daemon=True)
            self._thread.start()
        self._wake.set()

    def feed(self, text, version=None):
        """The editor's current text and its document version (`EditorState.pane.version`)."""
        self.version = version
        self.text = text

    def close(self):
        self.closed = True
        self._wake.set()

    def _work(self):
        while not self.closed:
            self._wake.wait()
            self._wake.clear()
            # Typing restarts the wait: analyse once the text rests.
            while self._wake.wait(QUIET):
                self._wake.clear()
            text, version = self._text, self.version
            if self.closed or text is self.analysed:
                continue
            self.errors, self.imports, self.tree, self.tints = analyse(text, self.path, version)
            self.analysed = text
            from meltygui.core.windowing.glfw_utils import request_render
            request_render()


def analyse(text, path=None, version=None):
    """(errors, imports, tree, tints) of a Python source text: syntax and
    lint findings as [(line, message)], missing import statements by line,
    the parse tree and the definition tints as `Marks` for document version
    `version`."""
    from meltygui.code import code_checks
    from meltygui.editor import text_editor
    errors, imports, tree, tints = [], {}, None, None
    try:
        compile(text, str(path or '<editor>'), 'exec')
    except SyntaxError as error:
        errors = list(text_editor._exception_errors(error))
        errors += [(more.lineno, more.msg) for more in text_editor._compile_check_more(text, error)]
    else:
        try:
            errors = list(code_checks.check_source(text, path))
            imports = code_checks.collect_import_suggestions(text, path)
        except Exception as error:
            errors = [(1, f'lint failed: {error}')]
    try:
        from meltygui.code.libcst_conversion import cst_module_to_dict
        tree = cst_module_to_dict(text)
        tints = text_editor._collect_def_tints(tree, text)
        if tints and tints[0] and not tints[1]:
            # No usage index for this text: wash the tinted names where they occur.
            starts = [0]
            for line in text.split('\n'):
                starts.append(starts[-1] + len(line) + 1)
            spans = []
            for head, _, _, rgb in tints[0]:
                words = text[starts[head]:starts[head + 1]].replace('(', ' ').replace(':', ' ').split()
                if len(words) > 1 and words[0] in ('def', 'class', 'async'):
                    name = words[2] if words[0] == 'async' and len(words) > 2 else words[1]
                    spans += [(low, high, rgb, 1.) for low, high in text_editor._word_match_ranges(text, name)]
            tints = (tints[0], tuple(spans)) + tuple(tints[2:])
        from meltygui.editor import marks
        tints = marks.tints(tints, text, version)
    except Exception:
        tree = tints = None
    return errors, imports, tree, tints
