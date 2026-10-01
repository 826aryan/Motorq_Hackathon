"""One-off: download the OSM drive network and save it as a compact .npz for the road graph.

Run via docker (needs internet once):
    docker compose -f infra/docker-compose.yml --env-file .env --profile tools run --rm graph-builder
"""
from pathlib import Path

import numpy as np
import osmnx as ox
import yaml


def main() -> None:
    cfg = yaml.safe_load(Path("simulator/config.yaml").read_text())["graph"]
    out = Path(cfg["path"])
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"Downloading drive network: {cfg['center']} radius {cfg['radius_m']} m ...")
    g = ox.graph_from_point(tuple(cfg["center"]), dist=cfg["radius_m"], network_type="drive")
    # Keep only the strongly connected part so every A* query has an answer.
    g = ox.truncate.largest_component(g, strongly=True)
    g = ox.add_edge_speeds(g, fallback=30)

    nodes = list(g.nodes)
    index = {n: i for i, n in enumerate(nodes)}
    best: dict[tuple[int, int], tuple[float, float]] = {}   # parallel edges: keep the shortest
    for u, v, data in g.edges(data=True):
        key = (index[u], index[v])
        length = float(data["length"])
        if key not in best or length < best[key][0]:
            best[key] = (length, float(data["speed_kph"]))

    edges = np.array([(u, v, ln, sp) for (u, v), (ln, sp) in best.items()])
    np.savez_compressed(
        out,
        node_lat=np.array([g.nodes[n]["y"] for n in nodes]),
        node_lon=np.array([g.nodes[n]["x"] for n in nodes]),
        edge_u=edges[:, 0].astype(np.int32),
        edge_v=edges[:, 1].astype(np.int32),
        edge_len_m=edges[:, 2].astype(np.float32),
        edge_speed_kmh=edges[:, 3].astype(np.float32),
    )
    print(f"Saved {len(nodes)} nodes, {len(edges)} edges -> {out}")


if __name__ == "__main__":
    main()
