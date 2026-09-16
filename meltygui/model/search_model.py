"""Search model functions and supporting definitions."""



def _fuzzy_substring_distance(q, k):
    """Min edit distance between `q` and any substring of `k` (the k-differences
    DP: row 0 is all zeros so the match may start anywhere in k). Damerau/OSA, so
    an adjacent transposition — the most common typo — costs 1, not 2. Both
    lowercase."""
    m, n = len(q), len(k)
    if m == 0:
        return 0
    prev2 = None
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        qi = q[i - 1]
        cur = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if qi == k[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (prev2 is not None and j > 1
                    and qi == k[j - 2] and q[i - 2] == k[j - 1]):
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
        prev2 = prev
        prev = cur
    return min(prev)


def _fuzzy_key_match(q, k):
    """Does query `q` match candidate `k` (both lowercase), tolerating a few
    typos? Exact substring first (fast, also covers short queries); for longer
    queries fall back to approximate substring matching with a small edit budget
    that scales with length (~1 typo per 4 chars). The single predicate the key
    match's count, current index and highlight all share, so they stay in sync."""
    if not q:
        return False
    if q in k:
        return True
    if len(q) < 4:
        return False
    return _fuzzy_substring_distance(q, k) <= max(1, len(q) // 4)


def _split_words(s):
    """Lowercased word tuple of an identifier / label: separators (`_`, `.`,
    `/`, ` `, `-`, ...) and camelCase boundaries both split; digits are their
    own words. ("draw_any" -> ("draw", "any"), "GLState" -> ("gl", "state"),
    "new_core_view.py" -> ("new", "core", "view", "py"))."""
    from meltygui.core.rendering.render_dispatch import _WORD_RE

    return tuple(w.lower() for w in _WORD_RE.findall(s))


def _edit_distance(a, b, cap):
    """Damerau/OSA edit distance, capped (returns cap + 1 once exceeded)."""
    m, n = len(a), len(b)
    if abs(m - n) > cap:
        return cap + 1
    prev2 = None
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        ai = a[i - 1]
        cur = [i] + [0] * n
        row_min = i
        for j in range(1, n + 1):
            cost = 0 if ai == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (prev2 is not None and j > 1
                    and ai == b[j - 2] and a[i - 2] == b[j - 1]):
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            if v < row_min:
                row_min = v
        if row_min > cap:
            return cap + 1
        prev2 = prev
        prev = cur
    return prev[n]


def _word_edits(w, tw, budget):
    """Cost of query word `w` claiming target word `tw`: 0 for an exact
    prefix or a >=2-char exact substring; the edit distance of a fuzzy
    PREFIX (same first char, >=3 chars, ~1 typo per 3 chars) when within
    `budget`; None when it can't claim it."""
    if tw.startswith(w):
        return 0
    if len(w) >= 2 and w in tw:
        return 0
    if budget <= 0 or len(w) < 3 or w[0] != tw[0]:
        return None
    tol = min(budget, 1 + (len(w) - 3) // 3)
    best = None
    for L in (len(w) - 1, len(w), len(w) + 1):
        if 0 < L <= len(tw):
            d = _edit_distance(w, tw[:L], tol)
            if d <= tol and (best is None or d < best):
                best = d
    return best


def _assign_words(qws, twords, budget):
    """Min total cost of every query word claiming a distinct target word
    (any order), or None. Tiny backtracking -- a handful of words a side."""
    n = len(twords)
    used = [False] * n
    best = [None]

    def rec(i, cost):
        if best[0] is not None and cost >= best[0]:
            return
        if i == len(qws):
            best[0] = cost
            return
        w = qws[i]
        for j in range(n):
            if used[j]:
                continue
            e = _word_edits(w, twords[j], budget - cost)
            if e is None:
                continue
            used[j] = True
            rec(i + 1, cost + e)
            used[j] = False

    rec(0, 0)
    return best[0]


def _segment_match(q, twords, budget):
    """Min cost of segmenting separator-less `q` into consecutive pieces
    that each claim a distinct target word (rules of _word_edits), or None.
    ("anydraw" -> any + draw; "drawamy" -> draw + amy~any.)"""
    n = len(twords)
    used = [False] * n
    best = [None]
    L = len(q)

    def rec(pos, cost):
        if best[0] is not None and cost >= best[0]:
            return
        if pos == L:
            best[0] = cost
            return
        for j in range(n):
            if used[j]:
                continue
            tw = twords[j]
            used[j] = True
            for k in range(pos + 1, L + 1):
                e = _word_edits(q[pos:k], tw, budget - cost)
                if e is not None:
                    rec(k, cost + e)
            used[j] = False

    rec(0, 0)
    return best[0]


def _word_match(q, qws, twords, budget):
    """Total edit cost of query `q` (lowercased; `qws` its words) against a
    target's words, or None. Word-form queries assign words; a single-word
    query tries a straight word claim, then segmentation."""
    if not twords:
        return None
    if len(qws) > 1:
        return _assign_words(qws, twords, budget)
    if len(qws) == 1:
        e = _assign_words(qws, twords, budget)
        if e is not None:
            return e
    return _segment_match(q, twords, budget)
