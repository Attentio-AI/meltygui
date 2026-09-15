"""Line comparison primitives shared by text inspection and integrations."""
import difflib
import bisect

def diff_opcodes(a_lines, b_lines):
    """Non-equal SequenceMatcher opcodes over two line lists, with the common
    prefix/suffix trimmed BEFORE matching. SequenceMatcher walks its full
    matching structure even for identical inputs — 108ms on an 11k-line file
    with ZERO changes, paid on the render thread at every diff open — while
    real compares differ in a few localized spans. Trimming equal edge lines
    first drops the identical/localized case to ~2ms and leaves the spread
    case unchanged. The opcodes reconstruct b exactly and change the same
    number of lines as untrimmed difflib (pinned across repo-history pairs
    in tests/test_diff_trim.py); when a change is bracketed by identical
    lines (an inserted block that starts or ends with the blank line that
    also precedes/follows it) its ANCHOR can land a line away from
    untrimmed difflib's — equally minimal, equally valid, the same freedom
    every trimming diff tool (git included) exercises. Bit-exact parity is
    NOT attainable from a window: difflib resolves those ties through its
    global longest-match recursion, so the same local shape anchors
    differently depending on far-away content (fast_dock vs text_editor
    pairs, 08-24). Offsets are restored before returning."""
    len_a, len_b = len(a_lines), len(b_lines)
    lo = 0
    limit = min(len_a, len_b)
    while lo < limit and a_lines[lo] == b_lines[lo]:
        lo += 1
    hi = 0
    limit = min(len_a, len_b) - lo
    while hi < limit and a_lines[len_a - 1 - hi] == b_lines[len_b - 1 - hi]:
        hi += 1
    sm = difflib.SequenceMatcher(a=a_lines[lo:len_a - hi],
                                 b=b_lines[lo:len_b - hi], autojunk=False)
    return [(tag, i1 + lo, i2 + lo, j1 + lo, j2 + lo)
            for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]



def _diff_blocks(base, new):
    """Change blocks for the side-by-side compare view: SequenceMatcher
    opcodes over lines, as (new0, new1, base0, base1, tag) tuples with
    0-based half-open LINE ranges — new* in the editable buffer (left
    pane), base* in the reference (right pane, which draws the ribbons).
    tag is 'replace' / 'delete' / 'insert' (delete = lines only in the
    reference). Empty list = identical. diff_opcodes trims the common
    prefix/suffix before matching — the difference between ~108ms and
    ~2ms on the render thread for a big file's diff open."""
    from meltygui.editor.diff import diff_opcodes
    return [(j1, j2, i1, i2, tag)
            for tag, i1, i2, j1, j2 in diff_opcodes(base.split("\n"),
                                                    new.split("\n"))]


def _incremental_diff_blocks(base, old_text, new_text, blocks):
    """Splice-update `blocks` (the diff base→old_text) into the diff
    base→new_text for a LOCALIZED edit (typing) without re-matching the
    whole file: line-trim the old↔new change window, widen it (+margin)
    out of any overlapping blocks so both edges sit in verified-EQUAL
    regions, map those edges into base space exactly (cumulative block
    delta — exact outside blocks), SequenceMatcher only the windows, and
    splice: blocks before stay, blocks after shift by the line delta. The
    result is a correct (possibly non-minimal) diff. Returns None when the
    edit isn't localized enough to pay off — the caller falls back to the
    full (debounced) diff."""
    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    old_count, new_count = len(old_lines), len(new_lines)
    overlap = min(old_count, new_count)
    common_head = 0
    while (common_head < overlap
           and old_lines[common_head] == new_lines[common_head]):
        common_head += 1
    if common_head == old_count and old_count == new_count:
        return list(blocks)                    # identical diff
    common_tail = 0
    while (common_tail < overlap - common_head
           and old_lines[old_count - 1 - common_tail]
           == new_lines[new_count - 1 - common_tail]):
        common_tail += 1
    line_delta = new_count - old_count
    window_start = max(0, common_head - 8)
    window_end = min(old_count, old_count - common_tail + 8)
    # Widen past any overlapping block (fixpoint - widening can reach the
    # next block...).
    grew = True
    while grew:
        grew = False
        for n0, n1, _b0, _b1, _t in blocks:
            if (n0 < window_end and n1 > window_start
                    and (n0 < window_start or n1 > window_end)):
                window_start = min(window_start, n0)
                window_end = max(window_end, n1)
                grew = True
    if (window_end - window_start) > 0.5 * max(old_count, 1):
        return None                            # not localized - full diff
    before, inside, after = [], [], []
    for block in blocks:
        if block[1] <= window_start:
            before.append(block)
        elif block[0] >= window_end:
            after.append(block)
        elif window_start <= block[0] and block[1] <= window_end:
            inside.append(block)
        else:
            return None                        # straddler (shouldn't happen)
    delta_before = sum((b1 - b0) - (n1 - n0)
                       for n0, n1, b0, b1, _t in before)
    delta_inside = sum((b1 - b0) - (n1 - n0)
                       for n0, n1, b0, b1, _t in inside)
    base_lines = base.split("\n")
    base_start = window_start + delta_before
    base_end = window_end + delta_before + delta_inside
    if (not (0 <= base_start <= base_end <= len(base_lines))
            or window_end + line_delta < window_start):
        return None
    matcher = difflib.SequenceMatcher(
        a=base_lines[base_start:base_end],
        b=new_lines[window_start:window_end + line_delta], autojunk=False)
    local = [(window_start + j1, window_start + j2,
              base_start + i1, base_start + i2, tag)
             for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"]
    return (before + local
            + [(n0 + line_delta, n1 + line_delta, b0, b1, t)
               for n0, n1, b0, b1, t in after])


def _diff_disp_span(pane, l0, l1):
    """Half-open buffer line range -> half-open display line range. A range
    swallowed by a collapsed fold collapses onto the fold header's row, so
    its wash/ribbon lands on the collapse line (IntelliJ-style)."""
    d2b = getattr(pane, "_diff_d2b", None)
    if d2b is None:
        return l0, l1
    d0 = max(bisect.bisect_right(d2b, int(l0)) - 1, 0)
    if l1 <= l0:
        return d0, d0
    d1 = max(bisect.bisect_right(d2b, int(l1) - 1) - 1, 0) + 1
    return d0, max(d1, d0)

