"""Spell checking for the text editor, backed by symspellpy (SymSpell).

SymSpell is used over pure-Python checkers because misspelling *detection* is a
distance-0 dictionary hit (cheap) while *suggestions* — which the editor wants on
demand — are SymSpell's strength: candidate generation is orders of magnitude
faster than the Norvig edit-distance approach.

The dictionary (~82k English words bundled with the package) is loaded lazily on
first use, in two parts with very different costs. Detection only asks whether a
word is a dictionary entry, so it reads the word list into a set (`_get_words`,
about 20 ms). The SymSpell index that suggestions need takes over a second of
interpreter time to build and several hundred MB of small objects (`_get_sym`):
it is built with the first `suggest`, never for underlining.

What is handed to it is code as often as prose, so a word is read the way
code spells it (`find_misspellings`): every character that is no letter
delimits (quotes, underscores, digits, dots), a camel case name is its parts
(`TextEditor`: Text, Editor), a possessive or a contraction is its stem
(`window's`: window; `doesn't`: does), and
a part is known when the dictionary has it, its singular, or the two words it
is run together from (`cleanslate`), or when code has it: Python's keywords
and builtins with the methods of the plain types, the standard library's and
the loaded modules' names, and the terms of `CODE`. Parts shorter than `SHORTEST` are never flagged (`os`, `cw`).
To make it stricter or laxer, edit `known`; to teach it a term, add it to `CODE`.
"""

import importlib.resources
import re

_MAX_EDIT = 2

# A part of a word: a capitalised or lower case run of letters, or a run of capitals (an acronym,
# which ends before the capital that starts the next part: `HTTPServer` is HTTP, Server).
_PART_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
# Lever: parts shorter than this are never flagged.
SHORTEST = 4
# Lever: terms of code the dictionary lacks. One per word, lower case.
CODE = frozenset("""
    args kwargs argv async await bool calc config const ctrl ctx dict enum env func glfw gui imgui init int iter json
    kwarg len rebase rgba utils
    lexer melty meltygui metadata middleware namespace noqa nullable param params parser popover popovers pragma
    pyproject readme regex repo repr runtime schema stderr stdin stdout str struct subclass submodule sync
    todo toml tooltip tuple uint unicode utf uuid wayland whitespace xdg yaml
""".split())

_sym = None    # lazy SymSpell singleton: suggestions only
_words = None  # lazy set of the dictionary's words: detection
_code = None   # lazy set of the words code has (`_get_code`)
_seen = 0      # the modules loaded when `_code` was last put together


def _dictionary():
    return importlib.resources.as_file(importlib.resources.files("symspellpy") / "frequency_dictionary_en_82_765.txt")


def _get_words(between=None):
    """The dictionary's words as a set (once): what SymSpell answers a distance-0 lookup from.
    `between()` is called between pieces of the load (a worker gives way to a frame there)."""
    global _words
    if _words is None:
        words = set()
        with _dictionary() as dict_path, open(dict_path, encoding="utf8") as lines:
            for at, line in enumerate(lines):
                if line.strip():
                    words.add(line.split(" ", 1)[0])
                if between is not None and at % 2000 == 1999:
                    between()
        _words = frozenset(words)
    return _words


def _get_sym():
    """Build (once) and return the SymSpell instance with the English dict loaded."""
    global _sym
    if _sym is None:
        from symspellpy import SymSpell
        sym = SymSpell(max_dictionary_edit_distance=_MAX_EDIT)
        with _dictionary() as dict_path:
            sym.load_dictionary(str(dict_path), term_index=0, count_index=1)
        _sym = sym
    return _sym


def _get_code():
    """The words code has that the dictionary may lack: `CODE`, and the parts of Python's keywords and
    builtins, the plain types' methods and the names of the standard library's and the loaded modules. Put together again
    when more modules were loaded (a set of a few thousand short strings)."""
    global _code, _seen
    import sys
    if _code is None or len(sys.modules) != _seen:
        import builtins
        import keyword
        _seen = len(sys.modules)
        names = [*keyword.kwlist, *dir(builtins), *sys.stdlib_module_names, *{name.partition('.')[0] for name in list(sys.modules)},
                 *{name for kind in (str, bytes, list, dict, set, int, float) for name in dir(kind)}]
        _code = CODE | {part.lower() for name in names for part in _PART_RE.findall(name)}
    return _code


def known(word, words, code):
    """Whether `word` (lower case) is no misspelling: the dictionary's or code's, a plural of one of
    theirs, or two of the dictionary's words run together (each at least 3 letters)."""
    if word in words or word in code:
        return True
    if word.endswith('s') and (word[:-1] in words or word[:-1] in code or (word.endswith('es') and word[:-2] in words)):
        return True
    return any(word[:cut] in words and word[cut:] in words for cut in range(3, len(word) - 2))


def find_misspellings(text):
    """Return ``[(start, end, word), ...]`` for the parts of words that are misspelled.

    ``start``/``end`` are absolute indices into ``text`` (so the caller can map
    them straight to screen coordinates). See the module text for what a part
    is and when it is known.
    """
    words, code = _get_words(), _get_code()
    errors = []
    for match in _PART_RE.finditer(text):
        word = match.group(0)
        if len(word) < SHORTEST:
            continue
        # (What follows an apostrophe is shorter than `SHORTEST`: `'s`, `'re`. Of `doesn't` the word is `does`.)
        stem = word[:-1] if word.endswith('n') and text.startswith("'t", match.end()) else word
        if not known(stem.lower(), words, code):
            errors.append((match.start(), match.end(), word))
    return errors


def suggest(word, max_results=5):
    """Return up to ``max_results`` correction candidates for ``word``.

    Closest-first. The original word's leading capitalization is preserved so a
    suggestion for "Teh" comes back as "The", not "the".
    """
    from symspellpy import Verbosity
    sym = _get_sym()
    candidates = sym.lookup(word.lower(), Verbosity.CLOSEST, max_edit_distance=_MAX_EDIT)
    results = [c.term for c in candidates][:max_results]
    if word[:1].isupper():
        results = [r.capitalize() for r in results]
    return results
