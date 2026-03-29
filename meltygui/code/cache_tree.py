from src.lsd.gl_gui.view.core_conversion.address import Address


class _Unset:
    """Sentinel indicating a step hasn't produced a real value yet."""
    def __repr__(self):
        return "UNSET_VALUE"

UNSET_VALUE = _Unset()

# Types safe for early cutoff equality comparison
_CUTOFF_TYPES = {Address, str, int, float, bool, tuple, bytes, frozenset}

def register_cutoff_type(*types):
    """Register types as safe for early cutoff equality comparison."""
    _CUTOFF_TYPES.update(types)


class CacheNode:
    def __init__(self, type_key):
        self.type_key = type_key
        self.value = UNSET_VALUE
        self.children = []
        self._child_cursor = 0

    def __repr__(self):
        kids = [c.type_key for c in self.children]
        return f"CacheNode({self.type_key}, value={self.value!r}, children={kids})"


class CacheTree:
    """
    Tree-shaped cache that auto-shares nodes for symmetric type sequences.
    Types are inferred from output values.

    Builds progressively — UNSET positions are filled in on later walks.
    Handles type changes by replacing nodes and invalidating subtrees.
    """

    def __init__(self):
        self._root = CacheNode("__root__")
        self._mapping = []
        self._stack = None
        self._cursor = 0

    def begin(self):
        """Reset cursors for a new walk."""
        self._cursor = 0
        self._stack = [self._root]
        self._reset_child_cursors(self._root)

    def step(self, changed, value):
        """
        Advance one position. Returns (changed, cached_value).

        Early cutoff: if a step re-executes but produces the same result
        as what's cached, changed is flipped to False — stopping propagation.
        """
        if value is UNSET_VALUE or value is None:
            return self._step_unset()

        type_key = type(value).__name__
        node = self._resolve(type_key)

        # Check if mapping changed at this position
        if self._cursor < len(self._mapping):
            old_node = self._mapping[self._cursor]
            if old_node is not node:
                if old_node is not None:
                    self._clear(old_node)
                self._mapping[self._cursor] = node
        else:
            self._mapping.append(node)

        if changed or node.value is UNSET_VALUE:
            node.value = value

        self._cursor += 1
        return node.value

    def _step_unset(self):
        """Handle an UNSET_VALUE step."""
        if self._cursor < len(self._mapping) and self._mapping[self._cursor] is not None:
            # Replay old type through stack to keep it consistent
            old_node = self._mapping[self._cursor]
            self._resolve(old_node.type_key)
            self._cursor += 1
            return old_node.value
        else:
            # No prior mapping - can't resolve, leave placeholder
            if self._cursor >= len(self._mapping):
                self._mapping.append(None)
            self._cursor += 1
            return UNSET_VALUE

    def peek(self):
        """Return the cached value at the current cursor without advancing."""
        if self._cursor < len(self._mapping):
            node = self._mapping[self._cursor]
            if node is not None:
                return node.value
        return UNSET_VALUE

    def end(self):
        """End of walk. Stack is rebuilt each begin(), nothing to finalize."""
        self._stack = None

    def _resolve(self, type_key):
        current = self._stack[-1]

        # Same as current → stay at this level
        if current.type_key == type_key:
            return current

        # Parent matches → pop back up (reuse node)
        if len(self._stack) > 1 and self._stack[-2].type_key == type_key:
            self._stack.pop()
            return self._stack[-1]

        # Push - check existing children first
        child_idx = current._child_cursor
        if child_idx < len(current.children):
            existing = current.children[child_idx]
            if existing.type_key == type_key:
                # Reuse existing child
                current._child_cursor += 1
                self._stack.append(existing)
                return existing
            else:
                # Type changed - replace child, invalidate its subtree
                self._clear(existing)
                new_child = CacheNode(type_key)
                current.children[child_idx] = new_child
                current._child_cursor += 1
                self._stack.append(new_child)
                return new_child
        else:
            # New child
            child = CacheNode(type_key)
            current.children.append(child)
            current._child_cursor += 1
            self._stack.append(child)
            return child

    def _reset_child_cursors(self, node):
        node._child_cursor = 0
        for child in node.children:
            self._reset_child_cursors(child)

    def node_at(self, index):
        return self._mapping[index]

    def invalidate(self):
        self._clear(self._root)

    def _clear(self, node):
        node.value = UNSET_VALUE
        for child in node.children:
            self._clear(child)

    def print_tree(self, node=None, indent=0):
        if node is None:
            node = self._root
        prefix = "  " * indent
        print(f"{prefix}{node.type_key}: {node.value!r}")
        for child in node.children:
            self.print_tree(child, indent + 1)

    def print_mapping(self, labels=None):
        for i, node in enumerate(self._mapping):
            label = labels[i] if labels else str(i)
            if node is None:
                print(f"  [{i}] {label:>15} → (unset)")
            else:
                print(f"  [{i}] {label:>15} → {node.type_key} (id={id(node):#x})")

    def get_mapping_as_str(self, labels=None):
        lines = []
        for i, node in enumerate(self._mapping):
            label = labels[i] if labels else str(i)
            if node is None:
                lines.append(f"  [{i}] {label:>15} → (unset)")
            else:
                lines.append(f"  [{i}] {label:>15} → {node.type_key} (id={id(node):#x})")
        return "\n".join(lines)

    def __len__(self):
        return len(self._mapping)
