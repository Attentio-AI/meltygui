"""One-dimensional edge constraints, independent of views and native backends.

Cells connect shared edge objects with a minimum and optional maximum span.
Motion propagates only through those connections. The caller supplies walls and
chooses which contacts belong to an interaction; overlapping coordinates alone
do not create a collision.
"""

class EdgeGraph:
    """Adjacency over cells: ``ahead[id(near)]`` → ``[(far, floor, cap)]``,
    ``behind[id(far)]`` → ``[(near, floor, cap)]``, ``nodes`` id → edge."""

    def __init__(self, cells):
        self.ahead, self.behind, self.nodes = {}, {}, {}
        for near, far, floor, cap in cells:
            self.nodes[id(near)] = near
            self.nodes[id(far)] = far
            self.ahead.setdefault(id(near), []).append((far, floor, cap))
            self.behind.setdefault(id(far), []).append((near, floor, cap))

    def chain(self, start, goal, walls=frozenset(), forward=True,
              capped_only=False):
        """Weight of the binding chain of cells from ``start`` to ``goal``:
        the LONGEST sum of floors (a push chain — ``capped_only=False``) or
        the SHORTEST sum of caps over capped cells only (a pull chain —
        ``capped_only=True``), walking cells ahead (``forward``) or behind.
        None when no chain links them. Chains never pass through another
        wall: a wall never moves, so nothing propagates past it."""
        links = self.ahead if forward else self.behind
        pick = min if capped_only else max
        memo, on_stack = {}, set()

        def best(edge):
            edge_id = id(edge)
            if edge_id == id(goal):
                return 0.0
            if edge_id in walls or edge_id in on_stack:
                return None
            if edge_id in memo:
                return memo[edge_id]
            on_stack.add(edge_id)
            found = None
            for other, floor, cap in links.get(edge_id, ()):
                if capped_only:
                    if cap is None:
                        continue
                    weight = cap
                else:
                    weight = floor
                rest = best(other)
                if rest is None:
                    continue
                total = weight + rest
                found = total if found is None else pick(found, total)
            on_stack.discard(edge_id)
            memo[edge_id] = found
            return found

        return best(start)


def solve_edge(graph, edge, target, walls=frozenset(), axis="x"):
    """Move ``edge`` to ``target`` through the cell graph. Edges are
    independent objects: no other edge moves unless the moving edge (or
    one it already carried) makes CONTACT through a cell — two kinds, one
    per side of the moving edge:

      PUSH, ahead: the cell in front closes to its floor (_edge_min) and
      its far edge is shoved on ahead.
      PULL, behind: the cell it leaves behind opens to its cap (_edge_max)
      and its far edge is dragged along behind.

    Either chain runs cell by cell — a pushed edge closes the next cell, a
    pulled edge opens the next — and stops at the first cell with slack;
    consecutive capped cells therefore travel as one, exactly as
    consecutive floor-packed cells do. A cell with no cap never pulls. An
    edge several cells share (a nested layout's far edge) carries every
    cell it bounds.

    ``walls`` is a set of edge ids the cascade must NOT move. Contact stops
    dead at a wall: the *dragged* edge itself is clamped so the pile packs
    against the wall at its floors (push side) or stretches to its summed
    caps (pull side) instead of the chain shoving the wall along. Used by
    _solve_collisions to keep one FRAME edge from moving the other (breaks
    the foreign-width feedback loop — see there; its mirror image is a
    fully-capped row, which refuses a foreign widening the same way);
    interior divider drags pass no walls, so a divider can still push or
    pull a frame edge and slide/grow/shrink the window 1:1 with the
    cursor. Returns True if anything moved."""
    old = edge[axis]
    if target == old or id(edge) not in graph.nodes:
        return False
    forward = target > old
    # Wall clamps first: ahead through the floors, behind through the caps
    # (a pull chain can only reach a wall over capped cells). The
    # binding chain per wall is the longest floor chain / shortest cap
    # chain - the one that would move the wall first.
    for wall_id in walls:
        wall = graph.nodes.get(wall_id)
        if wall is None or wall is edge:
            continue
        push = graph.chain(edge, wall, walls, forward=forward)
        if push is not None:
            target = (min(target, wall[axis] - push) if forward
                      else max(target, wall[axis] + push))
        pull = graph.chain(edge, wall, walls, forward=not forward,
                           capped_only=True)
        if pull is not None:
            target = (min(target, wall[axis] + pull) if forward
                      else max(target, wall[axis] - pull))
    if target == old:
        return False
    edge[axis] = float(target)
    # Propagate contact. Every relaxation moves an edge the way the drag
    # went and never back, so this is a monotone worklist that settles on
    # its own; the guard only ever trips on a cyclic (corrupt) cell graph.
    pending = [edge]
    guard = 64 * (len(graph.nodes) + 1)
    while pending and guard > 0:
        guard -= 1
        current = pending.pop()
        current_id = id(current)
        if forward:
            for far, floor, _cap in graph.ahead.get(current_id, ()):    # push ahead
                if id(far) in walls:
                    continue
                need = current[axis] + floor
                if far[axis] < need:
                    far[axis] = need
                    pending.append(far)
            for near, _floor, cap in graph.behind.get(current_id, ()):  # pull behind
                if cap is None or id(near) in walls:
                    continue
                need = current[axis] - cap
                if near[axis] < need:
                    near[axis] = need
                    pending.append(near)
        else:
            for near, floor, _cap in graph.behind.get(current_id, ()):  # push ahead
                if id(near) in walls:
                    continue
                need = current[axis] - floor
                if near[axis] > need:
                    near[axis] = need
                    pending.append(near)
            for far, _floor, cap in graph.ahead.get(current_id, ()):    # pull behind
                if cap is None or id(far) in walls:
                    continue
                need = current[axis] + cap
                if far[axis] > need:
                    far[axis] = need
                    pending.append(far)
    return True


