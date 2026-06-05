"""Spell checking for the text editor, backed by symspellpy (SymSpell).

SymSpell is used over pure-Python checkers because misspelling *detection* is a
distance-0 dictionary hit (cheap) while *suggestions* — which the editor wants on
demand — are SymSpell's strength: candidate generation is orders of magnitude
faster than the Norvig edit-distance approach.

The dictionary (~82k English words bundled with the package) is loaded lazily on
first use into a module-level singleton: that load is a one-time cost paid the
first time spell checking is toggled on, not at import time and never per frame.

NOTE: this currently spell-checks *every* alphabetic word handed to it. Making it
symbol-aware (only comments / strings / identifiers, with identifier splitting)
is driven from the caller via the libcst tree — see the TODO at the integration
point in text_editor.draw_text. Deliberately not coupled to that view's syntax
`tokenize()`.
"""

import importlib.resources
import re

_MAX_EDIT = 2

# A "word" for spell checking purposes is a run of letters, allowing internal
# apostrophes (don't, it's). Digits/underscores end a word, so identifiers like
# x1 or foo_bar split into their alphabetic runs.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")

_sym = None  # lazy SymSpell singleton


def _get_sym():
    """Build (once) and return the SymSpell instance with the English dict loaded."""
    global _sym
    if _sym is None:
        from symspellpy import SymSpell
        sym = SymSpell(max_dictionary_edit_distance=_MAX_EDIT)
        dict_ref = (importlib.resources.files("symspellpy")
                    / "frequency_dictionary_en_82_765.txt")
        with importlib.resources.as_file(dict_ref) as dict_path:
            sym.load_dictionary(str(dict_path), term_index=0, count_index=1)
        _sym = sym
    return _sym


def find_misspellings(text):
    """Return ``[(start, end, word), ...]`` for words not found in the dictionary.

    ``start``/``end`` are absolute indices into ``text`` (so the caller can map
    them straight to screen coordinates). A word is "misspelled" if a distance-0
    lookup misses — i.e. it is not a known dictionary entry.
    """
    from symspellpy import Verbosity
    sym = _get_sym()
    errors = []
    for match in _WORD_RE.finditer(text):
        word = match.group(0)
        if len(word) < 2:
            continue  # single characters are never flagged
        if not sym.lookup(word.lower(), Verbosity.TOP, max_edit_distance=0):
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
