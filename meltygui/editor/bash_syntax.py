"""Bash tokens in the text editor's palette, cached by source identity."""
from bisect import bisect_right
from pygments.lexers import BashLexer
from pygments.token import Comment, Keyword, Name, Number, String


def window_tokens(state, text, offsets, first, last):
    memo = getattr(state, '_bash_tokens', None)
    if memo is None or memo[0] is not text:
        tokens = []
        for offset, kind, value in BashLexer().get_tokens_unprocessed(text):
            color = ('comment' if kind in Comment else 'keyword' if kind in Keyword else
                     'builtin' if kind in Name.Builtin else 'builtin_pseudo' if kind in Name.Variable else
                     'string' if kind in String else 'number' if kind in Number else 'default')
            tokens.append((offset, value, color))
        memo = (text, tokens, [token[0] for token in tokens])
        state._bash_tokens = memo
    first = min(max(0, first), len(offsets) - 1)
    last = min(last, len(offsets) - 1)
    start = offsets[first]
    end = offsets[last + 1] if last + 1 < len(offsets) else len(text)
    result = []
    for index in range(max(0, bisect_right(memo[2], start) - 1), len(memo[1])):
        offset, value, color = memo[1][index]
        if offset >= end:
            break
        part = value[max(0, start - offset):end - offset]
        if part:
            result.append((part, color))
    return first, start, result
