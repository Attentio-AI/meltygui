"""Dropdown model functions and supporting definitions."""



def _dd_entries(container):
    """Normalized (key, value, label, is_branch) rows for one level. Dict rows
    read by their key (small/medium); list/tuple rows by their value
    (left/center/right) since the index isn't meaningful to the user."""
    if isinstance(container, dict):
        items = list(container.items())
        labelled = [(k, v, str(k)) for k, v in items]
    else:
        labelled = [(i, v, str(v)) for i, v in enumerate(container)]
    return [(k, v, lbl, isinstance(v, (dict, list))) for k, v, lbl in labelled]


def _dd_subtree_matches(value, search):
    """True if `search` (already lowercased) appears anywhere in this value's
    subtree, so a branch stays visible while searching when a descendant matches."""
    if not search:
        return True
    if isinstance(value, dict):
        return any(search in str(k).lower() or _dd_subtree_matches(v, search)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(_dd_subtree_matches(v, search) for v in value)
    return search in str(value).lower()


def _dd_visible_entries(container, search=""):
    """Rows shown for a level under `search`: a leaf whose label matches, or a
    branch matching by label OR holding a matching descendant. Empty `search`
    keeps everything."""
    if not isinstance(container, (dict, list)):
        return []
    rows = _dd_entries(container)
    if not search:
        return rows
    return [(k, v, lbl, br) for (k, v, lbl, br) in rows
            if search in lbl.lower() or (br and _dd_subtree_matches(v, search))]


def _dd_walk(collection, path):
    """Descend `collection` along a key/index `path`, returning the node there or
    None if the path no longer resolves (e.g. after a search prunes it)."""
    node = collection
    for k in path:
        try:
            node = node[k]
        except (KeyError, IndexError, TypeError):
            return None
    return node


def _dd_rows_at(collection, path, search):
    """Visible rows at `path`, applying the once-a-branch-matches-by-label rule:
    if any ancestor key on `path` matched the search by its own label, that whole
    subtree counts as a match, so deeper levels are shown unfiltered."""
    container = _dd_walk(collection, path)
    ancestor_matched = bool(search) and any(search in str(k).lower() for k in path)
    return _dd_visible_entries(container, "" if ancestor_matched else search)


def _dd_first_match_leaf(container, search, prefix=()):
    """DFS for the path to the first selectable leaf the search reveals, so the
    cursor can jump straight to it (auto-expanding the branches above). A branch
    that matches by its own label contributes its first leaf unfiltered."""
    for key, value, label, is_branch in _dd_visible_entries(container, search):
        path = tuple(prefix) + (key,)
        if not is_branch:
            return path
        sub_search = "" if (search and search in label.lower()) else search
        sub = _dd_first_match_leaf(value, sub_search, path)
        if sub is not None:
            return sub
    return None


def _dd_as_tuple(x):
    """Coerce a stored path-state to a tuple. The states are meant to be key
    tuples, but DropDownState is a DictConversion and its machinery can alias a
    complex stored value (e.g. a Lora) across fields; this keeps the dropdown
    robust to any input type by never iterating a non-sequence."""
    if isinstance(x, tuple):
        return x
    if isinstance(x, list):
        return tuple(x)
    return ()


def _dd_path_for_value(collection, value, _depth=0):
    """The key/index path of the first LEAF in `collection` equal to
    `value` (depth-first through nested dicts / lists), or None when no
    leaf holds it — the inverse of _dd_walk for the trigger label sync."""
    if _depth > 8 or value is None:
        return None
    items = (collection.items() if isinstance(collection, dict)
             else enumerate(collection) if isinstance(collection, (list, tuple))
             else ())
    for key, node in items:
        if isinstance(node, (dict, list, tuple)):
            sub = _dd_path_for_value(node, value, _depth + 1)
            if sub is not None:
                return (key,) + sub
            continue
        try:
            same = node == value
        except Exception:
            same = False
        if same is True:
            return (key,)
    return None


def _dd_label_for_path(collection, path):
    """Display label for a selected leaf path: the KEY for a dict entry (e.g.
    "red"), the VALUE for a list entry (e.g. "left"). Used for the trigger title
    so it reads as a name, not a raw value (which may be a tuple/number)."""
    if not path:
        return ""
    parent = _dd_walk(collection, tuple(path[:-1]))
    if isinstance(parent, dict):
        return str(path[-1])
    return str(_dd_walk(collection, tuple(path)))


def _dd_row_lookup(mapping, value):

    """mapping.get(value), tolerant of UNHASHABLE row values — a BRANCH row's
    value is the nested collection dict itself, which raised TypeError from
    every value-keyed style lookup (row_tags/row_tints/...)."""
    if not mapping:
        return None
    try:
        return mapping.get(value)
    except TypeError:
        return None
