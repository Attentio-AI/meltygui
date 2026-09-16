"""Voxel and line rendering playground. Run: python examples/voxel_playground.py."""
import math
import numpy as np
import meltygui
from meltygui.view.voxel_view import draw_voxels
from meltygui.view.graph_view import draw_line_graph

_VOLUME = globals().get("_VOLUME")


def demo_volume():
    """Procedural (96,96,96) float32 volume: a torus around Z plus two
    gaussian blobs — enough structure to judge orientation and filtering."""
    global _VOLUME
    if _VOLUME is None:
        n = 96
        c = np.linspace(-1.0, 1.0, n, dtype=np.float32)
        z, y, x = np.meshgrid(c, c, c, indexing="ij")
        ring = np.sqrt(x * x + y * y) - 0.55
        torus = np.exp(-(ring * ring + z * z) / 0.018)
        blob1 = np.exp(-((x - 0.35) ** 2 + (y + 0.30) ** 2 + (z - 0.40) ** 2) / 0.045)
        blob2 = np.exp(-((x + 0.40) ** 2 + (y - 0.25) ** 2 + (z + 0.35) ** 2) / 0.030)
        _VOLUME = np.clip(torus + 0.9 * blob1 + 0.8 * blob2, 0.0, 1.0).astype(np.float32)
    return _VOLUME


def demo_4d():
    """(time=8, depth=24, height=32, width=40): a torus whose radius breathes
    across the time dim — scrub `dim0` to watch it."""
    key = "_DEMO_4D"
    cached = globals().get(key)
    if cached is None:
        c = lambda n: np.linspace(-1.0, 1.0, n, dtype=np.float32)
        z, y, x = np.meshgrid(c(24), c(32), c(40), indexing="ij")
        frames = []
        for ti in range(8):
            ring = np.sqrt(x * x + y * y) - (0.35 + 0.05 * ti)
            frames.append(np.exp(-(ring * ring + z * z) / 0.02))
        cached = globals()[key] = np.stack(frames).astype(np.float32)
    return cached


def demo_5d():
    """(layer=3, head=4, d=16, h=24, w=32): per-layer/head frequency pattern —
    two scrubbers."""
    key = "_DEMO_5D"
    cached = globals().get(key)
    if cached is None:
        c = lambda n: np.linspace(0.0, 1.0, n, dtype=np.float32)
        z, y, x = np.meshgrid(c(16), c(24), c(32), indexing="ij")
        vols = [[np.abs(np.sin((layer + 1) * 3 * x + head) * np.cos((head + 1) * 3 * y) *
                        np.sin((layer + head + 1) * 2 * z))
                 for head in range(4)] for layer in range(3)]
        cached = globals()[key] = np.asarray(vols, dtype=np.float32)
    return cached


def demo_flat():
    """(seq=96, feature=4096): the neuralflow showcase — a 2-D matrix whose
    feature dim has per-128-chunk structure. Raw it's a 1-deep slab; turn on
    neural flow (chop x, along z, chunk 128) and the 32 chunks become a
    browsable volume."""
    key = "_DEMO_FLAT"
    cached = globals().get(key)
    if cached is None:
        seq = np.linspace(0.0, 6.0, 96, dtype=np.float32)[:, None]
        feat = np.arange(4096, dtype=np.float32)[None, :]
        chunk_id = np.floor(feat / 128.0)
        cached = globals()[key] = np.abs(
            np.sin(seq + chunk_id * 0.7) * np.cos(feat * (0.05 + 0.01 * chunk_id))
        ).astype(np.float32)
    return cached


def demo_lines_4d():
    """(phase=8, freq=6, samples=256, lines=12): damped sines — scrub
    `phase` / `freq`, the 12 lines fan out by amplitude."""
    key = "_DEMO_LINES_4D"
    cached = globals().get(key)
    if cached is None:
        x = np.linspace(0.0, 4.0 * math.pi, 256, dtype=np.float32)
        out = np.zeros((8, 6, 256, 12), dtype=np.float32)
        for p in range(8):
            for f in range(6):
                for k in range(12):
                    out[p, f, :, k] = ((k + 1) / 12.0) * np.sin((f + 1) * 0.5 * x
                                                                 + p * math.pi / 8) \
                        * np.exp(-x * 0.08 * (k % 3))
        cached = globals()[key] = out
    return cached


def main():
    # Explicit application composition; importing a production view never creates demos.
    meltygui.glfw_window(draw_voxels, name="Voxel torus", value=demo_volume(),
                        x_dim=-1, y_dim=-1, z_dim=-1,
                        app_id="melty-voxel-playground", width=720, height=640)
    meltygui.glfw_window(draw_voxels, name="Voxel 4D", value=demo_4d(),
                        x_dim=-1, y_dim=-1, z_dim=-1,
                        app_id="melty-voxel-playground", width=720, height=640)
    meltygui.glfw_window(draw_voxels, name="Voxel 5D", value=demo_5d(),
                        x_dim=-1, y_dim=-1, z_dim=-1,
                        app_id="melty-voxel-playground", width=720, height=640)
    meltygui.glfw_window(draw_voxels, name="Voxel neural flow", value=demo_flat(),
                        x_dim=-1, y_dim=-1, z_dim=-1, nf_on=True, nf_chunk=128,
                        app_id="melty-voxel-playground", width=720, height=640)
    meltygui.glfw_window(draw_line_graph, name="Line 4D", value=demo_lines_4d(),
                        app_id="melty-voxel-playground", width=720, height=640)


if __name__ == "__main__":
    main()
    meltygui.run()
