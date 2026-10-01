import itertools

import pytest


def test_astar_cost_equals_dijkstra_cost(grid_graph):
    pairs = [(0, 99), (9, 90), (12, 87), (45, 54), (3, 3)]
    for src, dst in pairs:
        _, a_cost = grid_graph.astar(src, dst)
        _, _, d_path_cost = grid_graph.dijkstra(src, {dst})
        assert a_cost == pytest.approx(d_path_cost)


def test_astar_path_is_connected_and_ends_right(grid_graph):
    path, _ = grid_graph.astar(0, 99)
    assert path[0] == 0 and path[-1] == 99
    for u, v in itertools.pairwise(path):
        assert any(w == v for w, _, _ in grid_graph.adj[u])


def test_dijkstra_stops_at_nearest_target(grid_graph):
    node, path, _ = grid_graph.dijkstra(0, {1, 99})
    assert node == 1 and path == [0, 1]


def test_nearest_node(grid_graph):
    assert grid_graph.nearest_node(12.95 + 0.0031, 77.58 + 0.0049) == 3 * 10 + 5
