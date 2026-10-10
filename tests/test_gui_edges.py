"""The Rust collision kernel against the existing solver and explicit rules."""
import random
import pytest
from meltygui.core.rendering._gui_native import EdgeGraph
from meltygui.core.layout.edge_constraints import EdgeGraph as Reference, solve_edge


def chain(positions, minima, maxima=None):
    graph = EdgeGraph()
    edges = [graph.edge(1, str(i), x) for i, x in enumerate(positions)]
    maxima = maxima or [None] * len(minima)
    graph.replace_cells(1, [(a, b, lo, hi) for a, b, lo, hi in
                            zip(edges, edges[1:], minima, maxima)])
    return graph, edges


@pytest.mark.parametrize('seed', range(30))
def test_shared_constraint_graph_matches_reference(seed):
    rng = random.Random(seed)
    graph = EdgeGraph()
    positions = [i * 100. for i in range(9)]
    edges = [graph.edge(1, str(i), x) for i, x in enumerate(positions)]
    ref = [{'x': x} for x in positions]
    specs = [(i, i + 1) for i in range(8)] + [(0, 4), (4, 8), (2, 6)]
    cells = [(a, b, rng.uniform(0, (b-a)*80),
              (rng.uniform((b-a)*110, (b-a)*180) if rng.random() < .6 else None)) for a,b in specs]
    graph.replace_cells(1, [(edges[a], edges[b], lo, hi) for a,b,lo,hi in cells])
    reference = Reference([(ref[a], ref[b], lo, hi) for a,b,lo,hi in cells])
    for _ in range(30):
        index = rng.randrange(1, 8)
        target = ref[index]['x'] + rng.uniform(-300, 300)
        walls = [0,8] if rng.random() < .5 else []
        solve_edge(reference, ref[index], target, {id(ref[i]) for i in walls})
        graph.solve(edges[index], target, [edges[i] for i in walls])
        assert [graph.position(e) for e in edges] == pytest.approx([r['x'] for r in ref])


def test_sticky_wall_opposite_edge_and_exact_reversal():
    graph, (left, right, wall) = chain([100, 200, 300], [60, 0])
    for distance, expected in [(50, [100,250,300]), (150,[50,300,300]),
                               (150,[50,300,300]), (0,[100,200,300])]:
        graph.drag(7, right, distance, [wall], left)
        assert list(graph.values().values()) == expected


def test_minimum_push_and_maximum_pull_share_one_gesture():
    graph, edges = chain([0,100,200,300], [60,60,60], [120,None,120])
    graph.drag(1, edges[1], 140)
    assert list(graph.values().values()) == [120,240,300,360]
    graph.drag(1, edges[1], 0)
    assert list(graph.values().values()) == [0,100,200,300]
    assert graph.span(edges[0], edges[-1]) == 180
    assert graph.span(edges[0], edges[-1], True) is None


def test_shared_edge_lifetime_limits_and_error_are_transactional():
    graph, edges = chain([0,100,200], [40,40])
    graph.replace_cells(2, [(edges[0],edges[1],70,90)])
    assert graph.span(edges[0],edges[1]) == 70
    before = graph.values()
    with pytest.raises(ValueError):
        graph.replace_cells(3, [(edges[0],edges[1],95,100)])
    assert graph.values() == before
    graph.remove(2)
    assert graph.span(edges[0],edges[1]) == 40
    graph.replace_cells(2, [(edges[0],edges[1],70,None)])
    assert graph.remove(1) == [2]
    assert graph.stats()[:2] == (0,0)


def test_cyclic_unsatisfiable_graph_rolls_back():
    graph, edges = chain([0,100], [10])
    before = graph.values()
    with pytest.raises(ValueError, match='cyclic'):
        graph.replace_cells(2, [(edges[1],edges[0],10,None)])
    assert graph.values() == before
