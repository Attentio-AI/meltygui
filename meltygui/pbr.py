"""
pbr — an immediate-mode 3-D object renderer with a physically based
material model, in the shape of the render functions.

    scene = begin_scene(gl_state, "target", width, height,
                        camera=orbit_camera(tilt, spin, zoom, target=(0, 0.4, 0)),
                        lights=[Light((2, 4, 3), (1, 1, 1), 40.0)],
                        environment=environment(gl_state, "studio"))
    draw_cylinder(scene, position=(0, 0.15, 0), scale=(1, 0.3, 1),
                  color=(0.45, 0.45, 0.48), roughness=0.55, metallic=0.1)
    draw_cube(scene, position=(1.5, 0.5, 0), rotation=(0, 0.6, 0),
              color=(0.8, 0.2, 0.1), roughness=0.3)
    draw_sphere(scene, position=(-1.5, 0.5, 0), color=(1.0, 0.85, 0.5),
                metallic=1.0, roughness=0.15)
    fbo = end_scene(scene)
    imgui.image(fbo.texture_id, width, height, uv0=(0, 1), uv1=(1, 0))

Every `draw_*` call is one draw, the scene its first argument the way a
@shader_func takes gl_state: its kwargs ARE the material (`color`,
`roughness`, `metallic`, `ao`, `emissive`) and the transform (`position`,
`rotation` Euler XYZ radians, `scale`, or a full `transform` 4x4) — the same
"parameters as function arguments" contract as @render_func / @shader_func,
and `@mesh_func(mesh_builder)` is what turns a mesh generator into such a
function (define new shapes by decorating a generator). begin_scene opens
the frame; end_scene renders the recorded draws — a depth pass from light 0
for shadow mapping (3x3 PCF), then the shaded pass — restores GL state and
hands the FBO back. `shadow_catcher=True` on a draw makes a surface that is
invisible except for the shadow it receives.
Nothing is retained between frames but the GL resources (meshes, programs,
the environment), which GLState owns and dedupes by key.

Shading is learnopengl's Cook-Torrance BRDF, metallic / roughness workflow:
GGX / Trowbridge-Reitz normal distribution, Smith-Schlick geometry,
Fresnel-Schlick, Lambert diffuse weighted by (1 - F)(1 - metallic), up to
four point / directional lights (packed as two mat4 uniforms: rows of xyz +
kind, and rgb + intensity). Environment lighting is learnopengl's
image-based lighting (PBR/IBL/Diffuse-irradiance + Specular-IBL) over a
CUBEMAP: the source is a Radiance .hdr equirectangular photo
(resources/hdri, Poly Haven CC0) or one of the procedural rooms, converted
to a cube (`_equirect_to_cube`), then baked ONCE per GLState into an
IRRADIANCE map (hemisphere convolution, the diffuse term), a PREFILTERED
specular map (GGX importance-sampled per mip, roughness → mip, the
split-sum's first half) and the BRDF integration LUT (its second half).
`Environment` holds the three plus the source cube. HDR is tone-mapped
(ACES) and gamma encoded at the end; the FBO holds straight alpha 1 on
every covered pixel and 0 where nothing was drawn, so views composite it
over their own bg. Meshes come from the generators here or from a file
(`load_model`: OBJ / STL / GLB, split by part name, `draw_mesh`).
"""

from __future__ import annotations
import json
import math
import os
import struct

import numpy as np
import OpenGL.GL as gl

from src.lsd.gl_gui.gl_state import GLState, GLTexture, _scalar
from src.lsd.gl_gui.shader_func import shader_func



def _frame_framebuffer():
    """The frame's render target (the fp16 scene while a frame is open)."""
    from src.lsd.gl_gui.melty import Melty
    return Melty.default_framebuffer()


# ═══════════════════════════════════════════════════════════════════════════
#  Meshes - numpy generators → (positions, normals, indices) → GL VAOs
# ═══════════════════════════════════════════════════════════════════════════

class MeshData:
    """CPU mesh: float32 (N,3) positions and normals, uint32 (M,) indices."""
    __slots__ = ("positions", "normals", "indices")

    def __init__(self, positions, normals, indices):
        self.positions = np.ascontiguousarray(positions, np.float32)
        self.normals = np.ascontiguousarray(normals, np.float32)
        self.indices = np.ascontiguousarray(indices, np.uint32)


def cube_mesh():
    """Unit cube centred on the origin, flat-shaded faces (24 verts)."""
    positions, normals, indices = [], [], []
    for axis in range(3):
        for sign in (-1.0, 1.0):
            n = np.zeros(3); n[axis] = sign
            u = np.zeros(3); u[(axis + 1) % 3] = 1.0
            v = np.cross(n, u)
            base = len(positions)
            for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                positions.append((n * 0.5 + u * (0.5 * su) + v * (0.5 * sv)))
                normals.append(n)
            indices += [base, base + 1, base + 2, base, base + 2, base + 3]
    return MeshData(positions, normals, indices)


def lathe_mesh(profile, segments=48):
    """Surface of revolution about Y. `profile` = [(r, y, nr, ny), ...] from
    bottom to top, each a ring with its outward normal in the (r, y) plane;
    consecutive rings are stitched with quads. A ring with r = 0 is a pole
    (still emitted as `segments + 1` verts so the seam stays simple)."""
    rings = len(profile)
    positions, normals = [], []
    for r, y, nr, ny in profile:
        for i in range(segments + 1):
            a = 2.0 * math.pi * i / segments
            c, s = math.cos(a), math.sin(a)
            positions.append((r * c, y, r * s))
            normals.append((nr * c, ny, nr * s))
    indices = []
    stride = segments + 1
    for k in range(rings - 1):
        for i in range(segments):
            a, b = k * stride + i, k * stride + i + 1
            c, d = (k + 1) * stride + i, (k + 1) * stride + i + 1
            indices += [a, c, b, b, c, d]
    return MeshData(positions, normals, indices)


def cylinder_mesh(edge_radius=0.0, segments=48, arc_steps=6):
    """Unit cylinder: radius 1, height 1 centred on the origin, with an
    optional rounded rim of `edge_radius` (in units of the radius). Flat
    caps and a straight wall, joined by quarter arcs when rounded."""
    e = max(0.0, min(0.5, float(edge_radius)))
    top, bottom = 0.5, -0.5
    profile = [(0.0, bottom, 0.0, -1.0)]            # bottom pole
    if e <= 0.0:
        profile += [(1.0, bottom, 0.0, -1.0), (1.0, bottom, 1.0, 0.0),
                    (1.0, top, 1.0, 0.0), (1.0, top, 0.0, 1.0)]
    else:
        profile.append((1.0 - e, bottom, 0.0, -1.0))
        for i in range(arc_steps + 1):              # bottom rim arc
            a = -math.pi / 2 + (math.pi / 2) * i / arc_steps
            profile.append((1.0 - e + e * math.cos(a), bottom + e + e * math.sin(a),
                            math.cos(a), math.sin(a)))
        for i in range(arc_steps + 1):              # top rim arc
            a = (math.pi / 2) * i / arc_steps
            profile.append((1.0 - e + e * math.cos(a), top - e + e * math.sin(a),
                            math.cos(a), math.sin(a)))
        profile.append((1.0 - e, top, 0.0, 1.0))
    profile.append((0.0, top, 0.0, 1.0))            # top pole
    return lathe_mesh(profile, segments)


def sphere_mesh(segments=48, rings=24):
    """Unit sphere (radius 1) as a lathe of a semicircle."""
    profile = []
    for i in range(rings + 1):
        a = -math.pi / 2 + math.pi * i / rings
        profile.append((math.cos(a), math.sin(a), math.cos(a), math.sin(a)))
    return lathe_mesh(profile, segments)


def rounded_box_mesh(radius=0.1, segments=8):
    """Unit box (1 × 1 × 1, centred) with every edge and corner rounded by
    `radius` (fraction of the half-size, ≤ 1). Each face is a `segments`²
    grid of the unit cube's surface; a surface point p is projected onto
    the inner box (the cube shrunk by the radius) as q, and the rounded
    surface is q + radius · normalize(p − q) — flat where p − q is a face
    normal, a quarter-cylinder along the edges, a sphere octant at the
    corners — with that direction as its normal."""
    r = max(1e-4, min(1.0, float(radius))) * 0.5
    inner = 0.5 - r
    positions, normals, indices = [], [], []
    grid = np.linspace(-0.5, 0.5, segments + 1)
    for axis in range(3):
        for sign in (-1.0, 1.0):
            n = np.zeros(3); n[axis] = sign
            u = np.zeros(3); u[(axis + 1) % 3] = 1.0
            v = np.cross(n, u)
            base = len(positions)
            for gv in grid:
                for gu in grid:
                    p = n * 0.5 + u * gu + v * gv
                    q = np.clip(p, -inner, inner)
                    d = p - q
                    nn = d / max(np.linalg.norm(d), 1e-9)
                    positions.append(q + nn * r)
                    normals.append(nn)
            stride = segments + 1
            for j in range(segments):
                for i in range(segments):
                    a = base + j * stride + i
                    b, c, d_ = a + 1, a + stride, a + stride + 1
                    indices += [a, b, c, b, d_, c]
    return MeshData(positions, normals, indices)


def extrude_polygon_mesh(points=(), height=1.0):
    """A CONVEX polygon in the XZ plane (counter-clockwise seen from +Y),
    extruded `height` along Y and centred on y = 0, flat-shaded. Compose
    concave shapes (an arrow) from several draws."""
    pts = [(float(x), float(z)) for x, z in points]
    n = len(pts)
    top, bottom = height * 0.5, -height * 0.5
    positions, normals, indices = [], [], []
    # caps (fan)
    for y, ny, order in ((top, 1.0, 1), (bottom, -1.0, -1)):
        base = len(positions)
        for x, z in pts:
            positions.append((x, y, z)); normals.append((0.0, ny, 0.0))
        for k in range(1, n - 1):
            tri = (base, base + k, base + k + 1)
            indices += list(tri if order > 0 else tri[::-1])
    # sides
    for i in range(n):
        (x0, z0), (x1, z1) = pts[i], pts[(i + 1) % n]
        nx, nz = (z1 - z0), -(x1 - x0)          # outward for a CCW polygon (seen from +Y)
        length = math.hypot(nx, nz) or 1.0
        nx, nz = nx / length, nz / length
        base = len(positions)
        for x, z in ((x0, z0), (x1, z1)):
            positions.append((x, bottom, z)); normals.append((nx, 0.0, nz))
            positions.append((x, top, z)); normals.append((nx, 0.0, nz))
        indices += [base, base + 2, base + 1, base + 1, base + 2, base + 3]
    return MeshData(positions, normals, indices)


def plane_mesh():
    """Unit square in XZ facing +Y, centred on the origin."""
    positions = [(-0.5, 0, -0.5), (0.5, 0, -0.5), (0.5, 0, 0.5), (-0.5, 0, 0.5)]
    normals = [(0, 1, 0)] * 4
    return MeshData(positions, normals, [0, 2, 1, 0, 3, 2])


def _smooth_normals(positions, indices):
    """Area-weighted vertex normals from the triangle list."""
    tri = positions[indices.reshape(-1, 3)]
    face_n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    normals = np.zeros_like(positions)
    for k in range(3):
        np.add.at(normals, indices.reshape(-1, 3)[:, k], face_n)
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    return normals / np.maximum(lengths, 1e-12)


def _load_obj(path):
    """Wavefront OBJ → {part name: MeshData}. Parts are the `o` / `g`
    groups (one part "mesh" without any); polygons fan-triangulated; vertex
    normals from the file where present, smooth normals otherwise."""
    positions, normals = [], []
    parts, current = {}, "mesh"
    faces = {}                                   # part → [(vi, ni), ...] triangles

    def add(part, tri):
        faces.setdefault(part, []).append(tri)

    with open(path, "r", errors="replace") as f:
        for line in f:
            if line.startswith("v "):
                positions.append([float(x) for x in line.split()[1:4]])
            elif line.startswith("vn "):
                normals.append([float(x) for x in line.split()[1:4]])
            elif line.startswith(("o ", "g ")):
                current = line[2:].strip() or current
            elif line.startswith("f "):
                verts = []
                for tok in line.split()[1:]:
                    idx = tok.split("/")
                    vi = int(idx[0]); vi = vi - 1 if vi > 0 else len(positions) + vi
                    ni = None
                    if len(idx) >= 3 and idx[2]:
                        ni = int(idx[2]); ni = ni - 1 if ni > 0 else len(normals) + ni
                    verts.append((vi, ni))
                for k in range(1, len(verts) - 1):
                    add(current, (verts[0], verts[k], verts[k + 1]))
    positions = np.asarray(positions, np.float32)
    normals = np.asarray(normals, np.float32) if normals else None
    for part, tris in faces.items():
        flat = [v for tri in tris for v in tri]
        if normals is not None and all(ni is not None for _, ni in flat):
            # per-corner normals; unique (vi, ni) pairs become vertices
            pairs = {}
            idx = np.array([pairs.setdefault(v, len(pairs)) for v in flat], np.uint32)
            keys = list(pairs)
            pos = positions[[vi for vi, _ in keys]]
            nrm = normals[[ni for _, ni in keys]]
            parts[part] = MeshData(pos, nrm, idx)
        else:
            idx = np.array([vi for vi, _ in flat], np.uint32)
            used = np.unique(idx)
            remap = np.zeros(positions.shape[0], np.uint32); remap[used] = np.arange(len(used))
            pos = positions[used]
            idx = remap[idx]
            parts[part] = MeshData(pos, _smooth_normals(pos, idx), idx)
    return parts


def _load_stl(path):
    """STL (binary or ASCII) → {"mesh": MeshData}, welded and smooth-shaded."""
    with open(path, "rb") as f:
        head = f.read(80)
        rest = f.read()
    if head.startswith(b"solid") and b"facet" in rest[:4000]:
        tris = [[float(x) for x in line.split()[1:4]]
                for line in (head + rest).decode(errors="replace").splitlines()
                if line.strip().startswith("vertex")]
        tri = np.asarray(tris, np.float32).reshape(-1, 3)
    else:
        n = struct.unpack("<I", rest[:4])[0]
        rec = np.frombuffer(rest[4:4 + n * 50], dtype=np.dtype([("n", "<3f4"), ("v", "<9f4"), ("a", "<u2")]))
        tri = rec["v"].reshape(-1, 3).astype(np.float32)
    quant = np.round(tri, 5)
    uniq, inverse = np.unique(quant, axis=0, return_inverse=True)
    idx = inverse.astype(np.uint32).reshape(-1)
    return {"mesh": MeshData(uniq, _smooth_normals(uniq, idx), idx)}


def _load_glb(path):
    """glTF binary → {node or mesh name: MeshData} for every triangle
    primitive with POSITION (NORMAL used when present, else smoothed),
    node transforms applied."""
    with open(path, "rb") as f:
        magic, _ver, _len = struct.unpack("<III", f.read(12))
        assert magic == 0x46546C67, "not a GLB"
        chunks = {}
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            clen, ctype = struct.unpack("<II", hdr)
            chunks[ctype] = f.read(clen)
    doc = json.loads(chunks[0x4E4F534A])
    bin_chunk = chunks.get(0x004E4942, b"")
    _ctype = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16,
              5125: np.uint32, 5126: np.float32}
    _ncomp = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}

    def accessor(i):
        acc = doc["accessors"][i]
        view = doc["bufferViews"][acc["bufferView"]]
        dtype = np.dtype(_ctype[acc["componentType"]])
        n = _ncomp[acc["type"]]
        start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
        stride = view.get("byteStride", dtype.itemsize * n)
        raw = np.frombuffer(bin_chunk, np.uint8, count=stride * (acc["count"] - 1) + dtype.itemsize * n,
                            offset=start)
        arr = np.lib.stride_tricks.as_strided(raw, shape=(acc["count"], dtype.itemsize * n),
                                              strides=(stride, 1))
        return np.ascontiguousarray(arr).view(dtype).reshape(acc["count"], n)

    def node_matrix(node):
        if "matrix" in node:
            return np.asarray(node["matrix"], np.float64).reshape(4, 4).T
        m = np.eye(4)
        t = node.get("translation", (0, 0, 0)); q = node.get("rotation", (0, 0, 0, 1))
        sc = node.get("scale", (1, 1, 1))
        x, y, z, w = q
        R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        m[:3, :3] = R * np.asarray(sc)[None, :]
        m[:3, 3] = t
        return m

    parts = {}

    def visit(ni, parent):
        node = doc["nodes"][ni]
        m = parent @ node_matrix(node)
        if "mesh" in node:
            mesh = doc["meshes"][node["mesh"]]
            name = node.get("name") or mesh.get("name") or f"mesh{node['mesh']}"
            for pi, prim in enumerate(mesh["primitives"]):
                if prim.get("mode", 4) != 4 or "POSITION" not in prim["attributes"]:
                    continue
                pos = accessor(prim["attributes"]["POSITION"]).astype(np.float64)
                pos = (m[:3, :3] @ pos.T).T + m[:3, 3]
                idx = (accessor(prim["indices"]).reshape(-1).astype(np.uint32) if "indices" in prim
                       else np.arange(pos.shape[0], dtype=np.uint32))
                if "NORMAL" in prim["attributes"]:
                    nrm = accessor(prim["attributes"]["NORMAL"]).astype(np.float64)
                    nrm = (np.linalg.inv(m[:3, :3]).T @ nrm.T).T
                    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
                else:
                    nrm = _smooth_normals(pos.astype(np.float32), idx)
                key = name if pi == 0 else f"{name}.{pi}"
                parts[key] = MeshData(pos, nrm, idx)
        for child in node.get("children", ()):
            visit(child, m)

    scene = doc.get("scenes", [{}])[doc.get("scene", 0)]
    for root in scene.get("nodes", range(len(doc.get("nodes", [])))):
        visit(root, np.eye(4))
    return parts


class Model:
    """A loaded model: `parts` (name → MeshData) and the bounds of the whole."""
    __slots__ = ("path", "parts", "bounds_min", "bounds_max")

    def __init__(self, path, parts):
        self.path, self.parts = path, parts
        allpos = np.vstack([p.positions for p in parts.values()]) if parts else np.zeros((1, 3))
        self.bounds_min, self.bounds_max = allpos.min(axis=0), allpos.max(axis=0)

    @property
    def size(self):
        return self.bounds_max - self.bounds_min

    @property
    def center(self):
        return (self.bounds_max + self.bounds_min) * 0.5

    def fit_transform(self, height=1.0, floor=True, up="y"):
        """A 4x4 that centres the model on the origin, scales it to `height`
        along the up axis (Z-up files: up="z" swings them to Y-up) and, with
        `floor`, rests its lowest point on y = 0."""
        m = np.eye(4)
        if up == "z":
            m[:3, :3] = rotation_matrix((-math.pi / 2, 0, 0))
        size = self.size.copy()
        if up == "z":
            size = size[[0, 2, 1]]
        s = height / max(size[1], 1e-9)
        center = m[:3, :3] @ self.center
        m2 = np.eye(4)
        m2[:3, :3] = m[:3, :3] * s
        m2[:3, 3] = -center * s
        if floor:
            m2[1, 3] += size[1] * s * 0.5
        return m2


_MODELS: dict = {}


def load_model(path) -> Model:
    """OBJ / STL / GLB from disk, cached on (path, mtime)."""
    path = os.fspath(path)
    stamp = os.path.getmtime(path)
    cached = _MODELS.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    ext = os.path.splitext(path)[1].lower()
    parts = {".obj": _load_obj, ".stl": _load_stl, ".glb": _load_glb}[ext](path)
    model = Model(path, parts)
    _MODELS[path] = (stamp, model)
    return model


class Mesh:
    """A mesh uploaded to a GLState: the VAO, its index count and its local
    bounds (for the shadow camera's fit)."""
    __slots__ = ("vao", "count", "bounds")

    def __init__(self, vao, count, bounds=None):
        self.vao, self.count = vao, count
        self.bounds = bounds if bounds is not None else (np.zeros(3), np.zeros(3))


def upload_mesh(gl_state: GLState, key, data: MeshData) -> Mesh:
    """Upload once per (gl_state, key): interleaved position + normal VBO and
    an index buffer, both owned by the VAO record for deletion."""
    def build():
        inter = np.hstack([data.positions, data.normals]).astype(np.float32)
        vbo = _scalar(gl.glGenBuffers(1))
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, inter.nbytes, inter, gl.GL_STATIC_DRAW)
        ibo = _scalar(gl.glGenBuffers(1))
        gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, ibo)
        gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, data.indices.nbytes, data.indices,
                        gl.GL_STATIC_DRAW)
        stride = 6 * 4
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.ctypes.c_void_p(12))
        return (vbo, ibo)
    vao = gl_state.vao(("pbr_mesh",) + tuple(key), build)
    mesh = Mesh(vao, int(data.indices.shape[0]),
                (data.positions.min(axis=0).astype(np.float64), data.positions.max(axis=0).astype(np.float64)))
    _mesh_counts[key] = (mesh.count, mesh.bounds)
    return mesh


# ═══════════════════════════════════════════════════════════════════════════
#  Transforms and camera - plain numpy, row-major (shader_func transposes)
# ═══════════════════════════════════════════════════════════════════════════

def _normalize(v):
    v = np.asarray(v, np.float64)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def rotation_matrix(rotation):
    """Euler XYZ (radians), applied in that order about the object's own
    axes: R = Rz · Ry · Rx (so a point is rotated about X first)."""
    rx, ry, rz = (float(a) for a in rotation)
    cx, sx, cy, sy, cz, sz = (math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry),
                              math.cos(rz), math.sin(rz))
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def model_matrix(position=(0, 0, 0), rotation=(0, 0, 0), scale=(1, 1, 1)):
    """4x4 model matrix: translate · rotate · scale."""
    s = np.asarray(scale if not np.isscalar(scale) else (scale,) * 3, np.float64)
    m = np.eye(4)
    m[:3, :3] = rotation_matrix(rotation) * s[None, :]
    m[:3, 3] = np.asarray(position, np.float64)
    return m


def look_at(eye, target, up=(0, 1, 0)):
    eye, target = np.asarray(eye, np.float64), np.asarray(target, np.float64)
    f = _normalize(target - eye)
    r = _normalize(np.cross(f, _normalize(up)))
    u = np.cross(r, f)
    view = np.eye(4)
    view[0, :3], view[1, :3], view[2, :3] = r, u, -f
    view[:3, 3] = -view[:3, :3] @ eye
    return view


def perspective(fov_y, aspect, near=0.05, far=100.0):
    f = 1.0 / math.tan(fov_y / 2.0)
    m = np.zeros((4, 4))
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = 2 * far * near / (near - far)
    m[3, 2] = -1.0
    return m


def orthographic(half_w, half_h, near, far):
    m = np.eye(4)
    m[0, 0] = 1.0 / half_w
    m[1, 1] = 1.0 / half_h
    m[2, 2] = -2.0 / (far - near)
    m[2, 3] = -(far + near) / (far - near)
    return m


class Camera:
    """Eye + target + vertical field of view; matrices on demand."""
    __slots__ = ("eye", "target", "up", "fov_y", "near", "far")

    def __init__(self, eye, target=(0, 0, 0), up=(0, 1, 0), fov_y=0.7, near=0.05, far=100.0):
        self.eye, self.target, self.up = tuple(eye), tuple(target), tuple(up)
        self.fov_y, self.near, self.far = float(fov_y), float(near), float(far)

    def view(self):
        return look_at(self.eye, self.target, self.up)

    def projection(self, aspect):
        return perspective(self.fov_y, aspect, self.near, self.far)


def orbit_camera(tilt, spin, zoom, target=(0, 0, 0), fov_y=0.7):
    """The voxel / space-mouse orbit in a Y-up frame: tilt = elevation, spin
    = azimuth, zoom = eye distance from `target`. Past a pole (cos(tilt) < 0)
    the up vector flips so the orbit continues over the top like draw_voxels'."""
    ct, st = math.cos(tilt), math.sin(tilt)
    fwd = np.array([-math.cos(spin) * ct, -st, -math.sin(spin) * ct])
    eye = np.asarray(target, np.float64) - fwd * float(zoom)
    up = (0, 1, 0) if ct >= 0 else (0, -1, 0)
    return Camera(eye, target, up, fov_y)


# ═══════════════════════════════════════════════════════════════════════════
#  Lights and the environment cube map
# ═══════════════════════════════════════════════════════════════════════════

class Light:
    """A point light (`position`, intensity falls off with 1/d²) or, with
    `directional=True`, a directional light where `position` is the direction
    TOWARD the light. `color` is linear RGB, `intensity` its scale."""
    __slots__ = ("position", "color", "intensity", "directional")

    def __init__(self, position, color=(1.0, 1.0, 1.0), intensity=10.0, directional=False):
        self.position, self.color = tuple(position), tuple(color)
        self.intensity, self.directional = float(intensity), bool(directional)


MAX_LIGHTS = 4


def pack_lights(lights):
    """Up to MAX_LIGHTS lights → two 4x4 matrices, one light per COLUMN:
    (x, y, z, kind) with kind 0 = off, 1 = point, 2 = directional, and
    (r, g, b, intensity). One mat4 uniform each — no array uniforms — and
    GLSL's `m[i]` is column i, which is why the lights go down the columns
    (packed as rows, `light_pos[i].w` read a stray 0 and every direct light
    was skipped: the first renders were lit by the environment alone)."""
    pos = np.zeros((4, 4)); col = np.zeros((4, 4))
    for i, light in enumerate(list(lights)[:MAX_LIGHTS]):
        pos[:3, i] = light.position
        pos[3, i] = 2.0 if light.directional else 1.0
        col[:3, i] = light.color
        col[3, i] = light.intensity
    return pos, col


def _env_studio(d):
    """Linear RGB of a lit studio room in direction d (N,3): a soft grey
    ceiling with two rectangular light panels, warm-grey walls, a dark floor."""
    x, y, z = d[:, 0], d[:, 1], d[:, 2]
    up = np.clip(y, 0, 1)
    col = np.empty_like(d)
    col[:] = (0.28, 0.29, 0.31)                                 # walls
    col += up[:, None] * np.array([0.32, 0.33, 0.35])           # soft on the ceiling
    col[y < 0] = (0.10, 0.09, 0.085)                            # floor
    col[y < 0] += (1.0 + y[y < 0])[:, None] * np.array([0.05, 0.05, 0.05])
    # Two light panels: key (up-right-front) and fill (up-left-back)
    for center, size, energy in (((0.45, 0.8, 0.4), 0.16, (5.0, 4.8, 4.4)),
                                 ((-0.6, 0.6, -0.5), 0.22, (2.2, 2.3, 2.6))):
        c = _normalize(center)
        cosang = d @ c
        mask = np.clip((cosang - (1.0 - size)) / (size * 0.35), 0.0, 1.0)
        col += mask[:, None] * np.array(energy)
    return col


def _env_outdoor(d):
    """Linear RGB of a clear sky over a green-grey ground, sun up-front-right."""
    y = d[:, 1]
    t = np.clip(y, 0, 1)[:, None]
    col = (1 - t) * np.array([0.85, 0.85, 0.9]) + t * np.array([0.25, 0.45, 0.95])
    ground = y < 0
    col[ground] = (0.22, 0.24, 0.16)
    col[ground] += (1.0 + y[ground])[:, None] * np.array([0.15, 0.13, 0.10])
    sun = _normalize((0.4, 0.7, 0.55))
    cosang = d @ sun
    col += np.clip((cosang - 0.985) / 0.015, 0, 1)[:, None] * np.array([40.0, 36.0, 30.0])
    col += np.clip((cosang - 0.90) / 0.10, 0, 1)[:, None] * np.array([0.8, 0.7, 0.5])
    return col


ENVIRONMENTS = {"room": _env_studio, "outdoor": _env_outdoor}

# Radiance .hdr images under resources/hdri (Poly Haven, CC0). "studio" is
# the default: a small photo studio with softboxes.
HDRIS = {"studio": "studio_small_09_1k.hdr"}
_HDRI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "hdri")

# Cubemap face axis conventions (GL): (major axis,, u axis, v axis)
_FACES = (
    ((1, 0, 0), (0, 0, -1), (0, -1, 0)), ((-1, 0, 0), (0, 0, 1), (0, -1, 0)),
    ((0, 1, 0), (1, 0, 0), (0, 0, 1)), ((0, -1, 0), (1, 0, 0), (0, 0, -1)),
    ((0, 0, 1), (1, 0, 0), (0, -1, 0)), ((0, 0, -1), (-1, 0, 0), (0, -1, 0)),
)


def read_hdr(path):
    """Radiance RGBE (.hdr) → float32 (H, W, 3) linear RGB, row 0 = TOP.
    Handles the new-style RLE scanlines Poly Haven writes and flat data."""
    with open(path, "rb") as f:
        data = f.read()
    pos = 0
    width = height = None
    while True:
        end = data.index(b"\n", pos)
        line = data[pos:end]
        pos = end + 1
        if line.startswith(b"-Y") or line.startswith(b"+Y"):
            parts = line.split()
            height, width = int(parts[1]), int(parts[3])
            flip_y = line.startswith(b"+Y")
            break
    buf = np.frombuffer(data, np.uint8, offset=pos)
    out = np.empty((height, width, 4), np.uint8)
    i = 0
    for y in range(height):
        if width >= 8 and width < 32768 and buf[i] == 2 and buf[i + 1] == 2 and buf[i + 2] < 128:
            i += 4
            row = np.empty((4, width), np.uint8)
            for c in range(4):
                x = 0
                while x < width:
                    n = int(buf[i]); i += 1
                    if n > 128:
                        n -= 128
                        row[c, x:x + n] = buf[i]; i += 1
                    else:
                        row[c, x:x + n] = buf[i:i + n]; i += n
                    x += n
            out[y] = row.T
        else:
            out[y] = buf[i:i + width * 4].reshape(width, 4); i += width * 4
    rgbe = out.astype(np.float32)
    scale = np.where(out[:, :, 3] > 0, np.ldexp(1.0, out[:, :, 3].astype(np.int32) - 136), 0.0)
    rgb = rgbe[:, :, :3] * scale[:, :, None]
    if flip_y:
        rgb = rgb[::-1]
    return np.ascontiguousarray(rgb, np.float32)


def _cube_texture(size, levels=1, internal=gl.GL_RGB16F):
    tex = _scalar(gl.glGenTextures(1))
    gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, tex)
    for level in range(levels):
        n = max(1, size >> level)
        for i in range(6):
            gl.glTexImage2D(gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + i, level, internal, n, n, 0,
                            gl.GL_RGB, gl.GL_FLOAT, None)
    gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MIN_FILTER,
                       gl.GL_LINEAR_MIPMAP_LINEAR if levels > 1 else gl.GL_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MAX_LEVEL, levels - 1)
    for wrap in (gl.GL_TEXTURE_WRAP_S, gl.GL_TEXTURE_WRAP_T, gl.GL_TEXTURE_WRAP_R):
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, wrap, gl.GL_CLAMP_TO_EDGE)
    gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
    t = GLTexture(tex, gl.GL_TEXTURE_CUBE_MAP, (6, size, size), internal)
    t.max_lod = float(levels - 1)
    return t


def _bake_fbo(gl_state):
    """The scratch framebuffer the bakes attach cube faces / the LUT to."""
    return gl_state.get(("pbr_bake_fbo",), lambda: _scalar(gl.glGenFramebuffers(1)),
                        lambda f: gl.glDeleteFramebuffers(1, [int(f)]))


def _render_cube_faces(gl_state, target: GLTexture, level, pass_fn, **uniforms):
    """Run a fullscreen shader_func into each face of `target` at `level`;
    the pass reads `face_axis` / `face_u` / `face_v` to turn uv into the
    direction (row 0 = the face's top, matching _FACES and the CPU path)."""
    fbo = _bake_fbo(gl_state)
    n = max(1, target.shape[1] >> level)
    prev_fbo = _scalar(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
    prev_vp = gl.glGetIntegerv(gl.GL_VIEWPORT)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
    gl.glViewport(0, 0, n, n)
    gl.glDisable(gl.GL_DEPTH_TEST)
    for i, (axis, ua, va) in enumerate(_FACES):
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + i, target.texture_id, level)
        pass_fn(gl_state, face_axis=tuple(float(c) for c in axis),
                face_u=tuple(float(c) for c in ua), face_v=tuple(float(c) for c in va), **uniforms)
    gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, 0, 0)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, prev_fbo)
    gl.glViewport(int(prev_vp[0]), int(prev_vp[1]), int(prev_vp[2]), int(prev_vp[3]))


_FACE_DIR = """
vec3 face_dir(vec2 uv) {
    return normalize(face_axis + (uv.x * 2.0 - 1.0) * face_u + (uv.y * 2.0 - 1.0) * face_v);
}
"""

EQUIRECT_FRAG = """
#version 330 core
in vec2 uv;
out vec4 FragColor;
uniform sampler2D equirect;
""" + _FACE_DIR + """
void main() {
    vec3 d = face_dir(uv);
    vec2 st = vec2(atan(d.z, d.x) * 0.15915494, asin(clamp(d.y, -1.0, 1.0)) * 0.31830989) + 0.5;
    FragColor = vec4(texture(equirect, st).rgb, 1.0);
}
"""

IRRADIANCE_FRAG = """
#version 330 core
in vec2 uv;
out vec4 FragColor;
uniform samplerCube source;
const float PI = 3.14159265359;
""" + _FACE_DIR + """
void main() {
    // learnopengl Diffuse-irradiance: a discrete hemisphere integral of
    // radiance · cos(theta) · sin(theta) around the texel's direction
    vec3 N = face_dir(uv);
    vec3 up = abs(N.y) < 0.999 ? vec3(0.0, 1.0, 0.0) : vec3(1.0, 0.0, 0.0);
    vec3 right = normalize(cross(up, N));
    up = cross(N, right);
    vec3 irradiance = vec3(0.0);
    float delta = 0.04;
    float count = 0.0;
    for (float phi = 0.0; phi < 2.0 * PI; phi += delta) {
        for (float theta = 0.0; theta < 0.5 * PI; theta += delta) {
            vec3 t = vec3(sin(theta) * cos(phi), sin(theta) * sin(phi), cos(theta));
            vec3 s = t.x * right + t.y * up + t.z * N;
            irradiance += textureLod(source, s, source_lod).rgb * cos(theta) * sin(theta);
            count += 1.0;
        }
    }
    FragColor = vec4(PI * irradiance / count, 1.0);
}
"""

_SAMPLING = """
const float PI = 3.14159265359;
float radical_inverse_vdc(uint bits) {
    bits = (bits << 16u) | (bits >> 16u);
    bits = ((bits & 0x55555555u) << 1u) | ((bits & 0xAAAAAAAAu) >> 1u);
    bits = ((bits & 0x33333333u) << 2u) | ((bits & 0xCCCCCCCCu) >> 2u);
    bits = ((bits & 0x0F0F0F0Fu) << 4u) | ((bits & 0xF0F0F0F0u) >> 4u);
    bits = ((bits & 0x00FF00FFu) << 8u) | ((bits & 0xFF00FF00u) >> 8u);
    return float(bits) * 2.3283064365386963e-10;
}
vec2 hammersley(uint i, uint n) { return vec2(float(i) / float(n), radical_inverse_vdc(i)); }
vec3 importance_sample_ggx(vec2 xi, vec3 N, float rough) {
    float a = rough * rough;
    float phi = 2.0 * PI * xi.x;
    float cos_theta = sqrt((1.0 - xi.y) / (1.0 + (a * a - 1.0) * xi.y));
    float sin_theta = sqrt(1.0 - cos_theta * cos_theta);
    vec3 H = vec3(cos(phi) * sin_theta, sin(phi) * sin_theta, cos_theta);
    vec3 up = abs(N.z) < 0.999 ? vec3(0.0, 0.0, 1.0) : vec3(1.0, 0.0, 0.0);
    vec3 tangent = normalize(cross(up, N));
    vec3 bitangent = cross(N, tangent);
    return normalize(tangent * H.x + bitangent * H.y + N * H.z);
}
float distribution_ggx(float NdotH, float rough) {
    float a = rough * rough, a2 = a * a;
    float d = NdotH * NdotH * (a2 - 1.0) + 1.0;
    return a2 / (PI * d * d);
}
"""

PREFILTER_FRAG = """
#version 330 core
in vec2 uv;
out vec4 FragColor;
uniform samplerCube source;
""" + _FACE_DIR + _SAMPLING + """
void main() {
    // learnopengl Specular-IBL: GGX importance sampling with V = R = N,
    // each sample read from the source mip its solid angle covers (the
    // pdf-based lod that removes the bright-dot noise)
    vec3 N = face_dir(uv);
    vec3 V = N;
    const uint SAMPLES = 256u;
    float sa_texel = 4.0 * PI / (6.0 * source_size * source_size);
    vec3 acc = vec3(0.0);
    float weight = 0.0;
    for (uint i = 0u; i < SAMPLES; i++) {
        vec2 xi = hammersley(i, SAMPLES);
        vec3 H = importance_sample_ggx(xi, N, roughness);
        vec3 L = normalize(2.0 * dot(V, H) * H - V);
        float NdotL = max(dot(N, L), 0.0);
        if (NdotL > 0.0) {
            float NdotH = max(dot(N, H), 0.0);
            float HdotV = max(dot(H, V), 0.0);
            float pdf = distribution_ggx(NdotH, roughness) * NdotH / (4.0 * HdotV) + 0.0001;
            float sa_sample = 1.0 / (float(SAMPLES) * pdf + 0.0001);
            float lod = roughness == 0.0 ? 0.0 : 0.5 * log2(sa_sample / sa_texel);
            acc += textureLod(source, L, lod).rgb * NdotL;
            weight += NdotL;
        }
    }
    FragColor = vec4(acc / max(weight, 1e-4), 1.0);
}
"""

BRDF_LUT_FRAG = """
#version 330 core
in vec2 uv;
out vec4 FragColor;
""" + _SAMPLING + """
float geometry_schlick_ggx_ibl(float NdotV, float rough) {
    float k = (rough * rough) / 2.0;
    return NdotV / (NdotV * (1.0 - k) + k);
}
void main() {
    // the split-sum's second half: scale and bias to F0 over (NdotV, roughness)
    float NdotV = max(uv.x, 1e-3), rough = uv.y;
    vec3 V = vec3(sqrt(1.0 - NdotV * NdotV), 0.0, NdotV);
    vec3 N = vec3(0.0, 0.0, 1.0);
    float A = 0.0, B = 0.0;
    const uint SAMPLES = 512u;
    for (uint i = 0u; i < SAMPLES; i++) {
        vec2 xi = hammersley(i, SAMPLES);
        vec3 H = importance_sample_ggx(xi, N, rough);
        vec3 L = normalize(2.0 * dot(V, H) * H - V);
        float NdotL = max(L.z, 0.0), NdotH = max(H.z, 0.0), VdotH = max(dot(V, H), 0.0);
        if (NdotL > 0.0) {
            float G = geometry_schlick_ggx_ibl(NdotV, rough) * geometry_schlick_ggx_ibl(NdotL, rough);
            float G_vis = (G * VdotH) / (NdotH * NdotV);
            float Fc = pow(1.0 - VdotH, 5.0);
            A += (1.0 - Fc) * G_vis;
            B += Fc * G_vis;
        }
    }
    FragColor = vec4(A / float(SAMPLES), B / float(SAMPLES), 0.0, 1.0);
}
"""


def _fs_triangle(gl_state):
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)


@shader_func(fragment=EQUIRECT_FRAG)
def equirect_pass(gl_state: GLState = None, equirect=None, face_axis=(1.0, 0.0, 0.0),
                  face_u=(0.0, 0.0, -1.0), face_v=(0.0, -1.0, 0.0), **kwargs):
    _fs_triangle(gl_state)


@shader_func(fragment=IRRADIANCE_FRAG)
def irradiance_pass(gl_state: GLState = None, source=None, source_lod=0.0, face_axis=(1.0, 0.0, 0.0),
                    face_u=(0.0, 0.0, -1.0), face_v=(0.0, -1.0, 0.0), **kwargs):
    _fs_triangle(gl_state)


@shader_func(fragment=PREFILTER_FRAG)
def prefilter_pass(gl_state: GLState = None, source=None, source_size=256.0, roughness=0.0,
                   face_axis=(1.0, 0.0, 0.0), face_u=(0.0, 0.0, -1.0), face_v=(0.0, -1.0, 0.0),
                   **kwargs):
    _fs_triangle(gl_state)


@shader_func(fragment=BRDF_LUT_FRAG)
def brdf_lut_pass(gl_state: GLState = None, **kwargs):
    _fs_triangle(gl_state)


class Environment:
    """The baked image-based lighting of one environment: `source` (the
    radiance cubemap, mip-mapped), `irradiance` (diffuse), `prefiltered`
    (specular, roughness → mip up to `max_lod`), `brdf_lut` (2-D), and
    `strength` scaling the whole ambient term."""
    __slots__ = ("name", "source", "irradiance", "prefiltered", "brdf_lut", "max_lod", "strength")

    def __init__(self, name, source, irradiance, prefiltered, brdf_lut, max_lod):
        self.name, self.source = name, source
        self.irradiance, self.prefiltered, self.brdf_lut = irradiance, prefiltered, brdf_lut
        self.max_lod, self.strength = float(max_lod), 1.0


def _source_cube(gl_state, preset, size):
    """The radiance cubemap: an .hdr photo run through equirect_pass, or a
    procedural room / sky evaluated on the CPU."""
    if preset in HDRIS:
        img = read_hdr(os.path.join(_HDRI_DIR, HDRIS[preset]))
        eq = _scalar(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, eq)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGB16F, img.shape[1], img.shape[0], 0,
                        gl.GL_RGB, gl.GL_FLOAT, np.ascontiguousarray(img[::-1]))   # row 0 → bottom
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        levels = int(math.log2(size)) + 1
        cube = _cube_texture(size, levels)
        _render_cube_faces(gl_state, cube, 0, equirect_pass,
                           equirect=GLTexture(eq, gl.GL_TEXTURE_2D, img.shape[:2], gl.GL_RGB16F))
        gl.glDeleteTextures([eq])
    else:
        fn = ENVIRONMENTS[preset]
        levels = int(math.log2(size)) + 1
        cube = _cube_texture(size, levels)
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, cube.texture_id)
        grid = (np.arange(size) + 0.5) / size * 2.0 - 1.0
        u, v = np.meshgrid(grid, grid)
        for i, (axis, ua, va) in enumerate(_FACES):
            d = (np.asarray(axis, np.float64)[None, :] + u.reshape(-1, 1) * np.asarray(ua)[None, :]
                 + v.reshape(-1, 1) * np.asarray(va)[None, :])
            d = d / np.linalg.norm(d, axis=1, keepdims=True)
            face = fn(d).astype(np.float32).reshape(size, size, 3)
            gl.glTexSubImage2D(gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + i, 0, 0, 0, size, size,
                               gl.GL_RGB, gl.GL_FLOAT, np.ascontiguousarray(face))
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
    gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, cube.texture_id)
    gl.glGenerateMipmap(gl.GL_TEXTURE_CUBE_MAP)
    gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
    return cube


def environment(gl_state: GLState, preset="studio", strength=1.0, size=256,
                irradiance_size=32, prefilter_size=128) -> Environment:
    """The baked IBL for `preset` — an HDRIS photo or an ENVIRONMENTS
    procedural map — once per (gl_state, preset, sizes). `strength` scales
    the ambient term (stamped on the returned object, read live)."""
    if preset not in HDRIS and preset not in ENVIRONMENTS:
        preset = "studio"

    def create():
        gl.glEnable(gl.GL_TEXTURE_CUBE_MAP_SEAMLESS)
        source = _source_cube(gl_state, preset, size)
        irr = _cube_texture(irradiance_size, 1)
        _render_cube_faces(gl_state, irr, 0, irradiance_pass, source=source,
                           source_lod=float(int(math.log2(size / 64))))
        levels = 5
        pre = _cube_texture(prefilter_size, levels)
        for level in range(levels):
            _render_cube_faces(gl_state, pre, level, prefilter_pass, source=source,
                               source_size=float(size), roughness=level / (levels - 1))
        # the BRDF LUT: a 2-D RG16F target on the bake framebuffer
        lut = _scalar(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, lut)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RG16F, 256, 256, 0, gl.GL_RG, gl.GL_FLOAT, None)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        fbo = _bake_fbo(gl_state)
        prev_fbo = _scalar(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        prev_vp = gl.glGetIntegerv(gl.GL_VIEWPORT)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, lut, 0)
        gl.glViewport(0, 0, 256, 256)
        gl.glDisable(gl.GL_DEPTH_TEST)
        brdf_lut_pass(gl_state)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, 0, 0)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, prev_fbo)
        gl.glViewport(int(prev_vp[0]), int(prev_vp[1]), int(prev_vp[2]), int(prev_vp[3]))
        lut_tex = GLTexture(lut, gl.GL_TEXTURE_2D, (256, 256), gl.GL_RG16F)
        return Environment(preset, source, irr, pre, lut_tex, levels - 1)

    def delete(env):
        gl.glDeleteTextures([env.source.texture_id, env.irradiance.texture_id,
                             env.prefiltered.texture_id, env.brdf_lut.texture_id])

    env = gl_state.get(("pbr_env", preset, size, irradiance_size, prefilter_size), create, delete,
                       deps=(preset, size, irradiance_size, prefilter_size))
    env.strength = float(strength)
    return env


# ═══════════════════════════════════════════════════════════════════════════
#  The shader
# ═══════════════════════════════════════════════════════════════════════════

PBR_VERT = """
#version 330 core
layout(location = 0) in vec3 a_position;
layout(location = 1) in vec3 a_normal;
out vec3 v_world;
out vec3 v_normal;
void main() {
    vec4 world = model * vec4(a_position, 1.0);
    v_world = world.xyz;
    v_normal = normal_matrix * a_normal;
    gl_Position = proj * view * world;
}
"""

PBR_FRAG = """
#version 330 core
in vec3 v_world;
in vec3 v_normal;
out vec4 FragColor;
uniform samplerCube irradiance_map;
uniform samplerCube prefilter_map;
uniform sampler2D brdf_lut;
uniform sampler2DShadow shadow_map;
const float PI = 3.14159265359;

// Visibility from the shadow-casting light (light 0). The receiving point
// is pushed along its normal by a texel's worth of world space (normal
// offset — kills acne on surfaces at a grazing angle to the light without
// a big depth bias) and projected into light clip space; a 4x4 grid of
// hardware-compared bilinear taps (16 taps x 4 texels) spread over
// `shadow_softness` texels gives the soft edge. Outside the map, or with
// shadows off, = fully lit.
float shadow_visibility(vec3 world, vec3 N, float NdotL) {
    if (!shadow_on) return 1.0;
    vec3 offset_world = world + N * shadow_normal_offset * (1.0 - NdotL * 0.5);
    vec4 lc = light_matrix * vec4(offset_world, 1.0);
    vec3 p = lc.xyz / lc.w * 0.5 + 0.5;
    if (p.z > 1.0 || p.x < 0.0 || p.x > 1.0 || p.y < 0.0 || p.y > 1.0) return 1.0;
    // receiver-plane depth bias: each tap compares against the depth the
    // receiving SURFACE has at that tap, not the centre's — the light-space
    // depth gradient over the map's uv, from the screen-space derivatives
    // (a wide kernel on a slanted receiver striped itself otherwise)
    vec3 dpdx = dFdx(p), dpdy = dFdy(p);
    float det = dpdx.x * dpdy.y - dpdx.y * dpdy.x;
    vec2 dz_duv = vec2(0.0);
    if (abs(det) > 1e-12) {
        dz_duv = vec2(dpdy.y * dpdx.z - dpdx.y * dpdy.z,
                      dpdx.x * dpdy.z - dpdy.x * dpdx.z) / det;
    }
    float z = p.z - shadow_bias;
    float lit = 0.0;
    float step = shadow_texel * shadow_softness * 0.5;
    float slope_cap = shadow_texel * shadow_softness * 2.0;
    for (int x = 0; x < 4; x++)
        for (int y = 0; y < 4; y++) {
            vec2 o = (vec2(x, y) - 1.5) * step;
            float dz = clamp(dot(o, dz_duv), -slope_cap, slope_cap);
            lit += texture(shadow_map, vec3(p.xy + o, z + dz));
        }
    return lit / 16.0;
}

// ── Cook-Torrance terms (learnopengl.com/PBR/Theory) ──
float distribution_ggx(vec3 N, vec3 H, float rough) {
    float a = rough * rough, a2 = a * a;
    float NdotH = max(dot(N, H), 0.0);
    float d = NdotH * NdotH * (a2 - 1.0) + 1.0;
    return a2 / (PI * d * d);
}
float geometry_schlick_ggx(float NdotV, float rough) {
    float r = rough + 1.0;
    float k = (r * r) / 8.0;
    return NdotV / (NdotV * (1.0 - k) + k);
}
float geometry_smith(vec3 N, vec3 V, vec3 L, float rough) {
    return geometry_schlick_ggx(max(dot(N, V), 0.0), rough)
         * geometry_schlick_ggx(max(dot(N, L), 0.0), rough);
}
vec3 fresnel_schlick(float cos_theta, vec3 F0) {
    return F0 + (1.0 - F0) * pow(clamp(1.0 - cos_theta, 0.0, 1.0), 5.0);
}
vec3 fresnel_schlick_roughness(float cos_theta, vec3 F0, float rough) {
    return F0 + (max(vec3(1.0 - rough), F0) - F0) * pow(clamp(1.0 - cos_theta, 0.0, 1.0), 5.0);
}
vec3 aces(vec3 x) {
    return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0);
}

void main() {
    vec3 N = normalize(v_normal);
    vec3 V = normalize(camera_pos - v_world);
    float rough = clamp(roughness, 0.04, 1.0);
    if (shadow_catcher && !shadow_on) { FragColor = vec4(0.0); return; }
    vec3 albedo = color;
    vec3 F0 = mix(vec3(0.04), albedo, metallic);

    // ── direct lighting: the packed lights, rows of (xyz, kind) / (rgb, I) ──
    vec3 Lo = vec3(0.0);
    for (int i = 0; i < 4; i++) {
        vec4 lp = light_pos[i];      // column i = light i (pack_lights)
        vec4 lc = light_color[i];
        if (lp.w < 0.5) continue;
        vec3 L; float attenuation;
        if (lp.w > 1.5) { L = normalize(lp.xyz); attenuation = 1.0; }
        else { vec3 to = lp.xyz - v_world; float d2 = max(dot(to, to), 1e-4);
               L = to * inversesqrt(d2); attenuation = 1.0 / d2; }
        vec3 H = normalize(V + L);
        vec3 radiance = lc.rgb * lc.w * attenuation;
        if (i == 0) {
            float vis = shadow_visibility(v_world, N, max(dot(N, L), 0.0));
            if (shadow_catcher) {
                // the catcher: nothing but the shadow it receives, as a
                // darkening the view composites over its own background
                FragColor = vec4(0.0, 0.0, 0.0, shadow_opacity * (1.0 - vis));
                return;
            }
            radiance *= vis;
        }
        float NDF = distribution_ggx(N, H, rough);
        float G = geometry_smith(N, V, L, rough);
        vec3 F = fresnel_schlick(max(dot(H, V), 0.0), F0);
        vec3 kD = (vec3(1.0) - F) * (1.0 - metallic);
        float NdotL = max(dot(N, L), 0.0);
        vec3 specular = (NDF * G * F) / (4.0 * max(dot(N, V), 0.0) * NdotL + 1e-4);
        Lo += (kD * albedo / PI + specular) * radiance * NdotL;
    }

    if (shadow_catcher) { FragColor = vec4(0.0); return; }   // no shadow light reached it

    // ── image-based lighting (learnopengl Specular-IBL): the baked
    // irradiance map for the diffuse term, the prefiltered map at the
    // roughness' mip and the BRDF LUT for the split-sum specular ──
    float NdotV = max(dot(N, V), 0.0);
    vec3 F = fresnel_schlick_roughness(NdotV, F0, rough);
    vec3 kD = (1.0 - F) * (1.0 - metallic);
    vec3 irradiance = texture(irradiance_map, N).rgb;
    vec3 diffuse = irradiance * albedo;
    vec3 R = reflect(-V, N);
    vec3 prefiltered = textureLod(prefilter_map, R, rough * env_max_lod).rgb;
    vec2 brdf = texture(brdf_lut, vec2(NdotV, rough)).rg;
    vec3 spec_ibl = prefiltered * (F * brdf.x + brdf.y);
    vec3 ambient = (kD * diffuse + spec_ibl) * ao * env_strength * env_tint;

    vec3 c = ambient + Lo + emissive;
    c = aces(c * exposure);
    // Linear out: the target is the fp16 scene (hdr_color.py), the
    // presentation pass encodes once.
    FragColor = vec4(c, 1.0);
}
"""


SHADOW_VERT = """
#version 330 core
layout(location = 0) in vec3 a_position;
void main() { gl_Position = light_matrix * model * vec4(a_position, 1.0); }
"""

SHADOW_FRAG = """
#version 330 core
void main() { }
"""


@shader_func(fragment=SHADOW_FRAG, vertex=SHADOW_VERT)
def shadow_pass(gl_state: GLState = None, mesh=None, model=None, light_matrix=None, **kwargs):
    gl.glBindVertexArray(mesh.vao)
    gl.glDrawElements(gl.GL_TRIANGLES, mesh.count, gl.GL_UNSIGNED_INT, None)


def _shadow_target(gl_state: GLState, size):
    """A depth-only framebuffer + its depth texture (sampled as a plain
    sampler2D, compared in the shader with PCF), once per (gl_state, size)."""
    def create():
        tex = _scalar(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_DEPTH_COMPONENT24, size, size, 0,
                        gl.GL_DEPTH_COMPONENT, gl.GL_FLOAT, None)
        # sampled as sampler2DShadow, the hardware compares the reference
        # depth against the 4 nearest texels and bilinearly blends the
        # result - every tap of the kernel below is already a smooth PCF
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_COMPARE_MODE, gl.GL_COMPARE_REF_TO_TEXTURE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_COMPARE_FUNC, gl.GL_LEQUAL)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_BORDER)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_BORDER)
        gl.glTexParameterfv(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_BORDER_COLOR, (1.0, 1.0, 1.0, 1.0))
        fbo = _scalar(gl.glGenFramebuffers(1))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_ATTACHMENT, gl.GL_TEXTURE_2D, tex, 0)
        gl.glDrawBuffer(gl.GL_NONE)
        gl.glReadBuffer(gl.GL_NONE)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, _frame_framebuffer())
        return (fbo, GLTexture(tex, gl.GL_TEXTURE_2D, (size, size), gl.GL_DEPTH_COMPONENT24))

    def delete(v):
        gl.glDeleteTextures([v[1].texture_id])
        gl.glDeleteFramebuffers(1, [int(v[0])])

    return gl_state.get(("pbr_shadow", size), create, delete, deps=(size,))


def _msaa_target(gl_state: GLState, key, width, height, samples):
    """A multisampled render target (RGBA8 colour + 24-bit depth
    renderbuffers) the shaded pass draws into; end_scene resolves it into
    the plain FBO with a blit. Re-made on size / sample-count change."""
    def create():
        color = _scalar(gl.glGenRenderbuffers(1))
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, color)
        gl.glRenderbufferStorageMultisample(gl.GL_RENDERBUFFER, samples, gl.GL_RGBA16F, width, height)
        depth = _scalar(gl.glGenRenderbuffers(1))
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, depth)
        gl.glRenderbufferStorageMultisample(gl.GL_RENDERBUFFER, samples, gl.GL_DEPTH_COMPONENT24,
                                            width, height)
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, 0)
        fbo = _scalar(gl.glGenFramebuffers(1))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
        gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_RENDERBUFFER, color)
        gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_ATTACHMENT, gl.GL_RENDERBUFFER, depth)
        ok = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) == gl.GL_FRAMEBUFFER_COMPLETE
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, _frame_framebuffer())
        if not ok:
            gl.glDeleteFramebuffers(1, [fbo]); gl.glDeleteRenderbuffers(2, [color, depth])
            raise RuntimeError("multisample framebuffer incomplete")
        return (fbo, color, depth)

    def delete(v):
        gl.glDeleteFramebuffers(1, [int(v[0])])
        gl.glDeleteRenderbuffers(2, [int(v[1]), int(v[2])])

    return gl_state.get(("pbr_msaa", key), create, delete, deps=(width, height, samples))


@shader_func(fragment=PBR_FRAG, vertex=PBR_VERT)
def pbr_pass(gl_state: GLState = None, mesh=None, model=None, normal_matrix=None,
             view=None, proj=None, camera_pos=(0.0, 0.0, 5.0),
             color=(0.8, 0.8, 0.8), roughness=0.5, metallic=0.0, ao=1.0,
             emissive=(0.0, 0.0, 0.0), light_pos=None, light_color=None,
             irradiance_map=None, prefilter_map=None, brdf_lut=None,
             env_max_lod=4.0, env_strength=1.0, env_tint=(1.0, 1.0, 1.0), exposure=1.0,
             shadow_map=None, light_matrix=None, shadow_on=False, shadow_texel=1.0 / 2048,
             shadow_softness=1.0, shadow_bias=0.001, shadow_normal_offset=0.01,
             shadow_catcher=False, shadow_opacity=0.6, **kwargs):
    # Program bound, uniforms set - one indexed draw of the mesh.
    gl.glBindVertexArray(mesh.vao)
    gl.glDrawElements(gl.GL_TRIANGLES, mesh.count, gl.GL_UNSIGNED_INT, None)


# ═══════════════════════════════════════════════════════════════════════════
# === Scene - the immediate-mode frame
# ═══════════════════════════════════════════════════════════════════════════

class Scene:
    """One frame's render target + camera + lights + environment, between
    begin_scene (binds the FBO, cleared to transparent, depth on) and
    end_scene (restores GL state). `fbo` is the GLState FBO the image landed
    in (`fbo.texture_id` for imgui.image)."""

    def __init__(self, gl_state: GLState, key, width, height, camera: Camera,
                 lights=(), environment=None, exposure=1.0, clear_color=(0, 0, 0, 0),
                 shadows=True, shadow_size=2048, shadow_softness=1.0, shadow_opacity=0.6,
                 environment_tint=(1.0, 1.0, 1.0), shadow_extent=2.5, samples=4):
        self.gl_state = gl_state
        # multisample anti-aliasing: the shaded pass draws into a
        # `samples`-sample target, resolved into the FBO at the end (0 or 1
        # = off, straight into the FBO)
        self.samples = int(samples)
        # multiplies the whole environment (image-based) term - a view's environment
        # colouring the light its scene sits in
        self.environment_tint = tuple(float(c) for c in environment_tint)[:3]
        self.key, self.width, self.height = key, int(width), int(height)
        self.camera = camera
        self.lights = list(lights)
        self.environment = environment
        self.exposure = float(exposure)
        self.clear_color = tuple(clear_color)
        # Shadows come from light 0. The frame's draws are RECORDED (the
        # draw_* calls are ignored in the API) and rendered at end_scene:
        # first a depth pass from the light over every caster, then the
        # main pass sampling it - two passes need the whole list.
        self.shadows = bool(shadows) and bool(self.lights)
        self.shadow_size, self.shadow_softness = int(shadow_size), float(shadow_softness)
        self.shadow_opacity = float(shadow_opacity)
        # the shadow camera's far plane to this many caster-radii past
        # the casters, so the ground their shadow lands on - the catcher's
        # business - is inside the frustum (at 1.0 a long cast is clipped)
        self.shadow_extent = float(shadow_extent)
        self._shadow_world_texel = 0.01     # stamped by _light_space (normal-offset bias)
        self.fbo = None
        self._view = self._proj = None
        self._light_pos = self._light_color = None
        self._draws = []

    def begin(self):
        self.fbo = self.gl_state.fbo(self.key, self.width, self.height)
        self._view = self.camera.view()
        self._proj = self.camera.projection(self.width / max(1, self.height))
        self._light_pos, self._light_color = pack_lights(self.lights)
        self._draws = []
        return self

    def _light_space(self):
        """The shadow camera: light 0 looking at the casters' bounding
        sphere — a perspective frustum from a point light, an orthographic
        box along a directional one. None when nothing casts."""
        light = self.lights[0]
        pts = []
        for d in self._draws:
            if not d["cast_shadow"]:
                continue
            lo, hi = d["mesh"].bounds
            corners = np.array([[x, y, z, 1.0] for x in (lo[0], hi[0]) for y in (lo[1], hi[1])
                                for z in (lo[2], hi[2])])
            pts.append((d["model"] @ corners.T).T[:, :3])
        if not pts:
            return None
        pts = np.vstack(pts)
        center = (pts.min(axis=0) + pts.max(axis=0)) * 0.5
        # `fit` hugs the casters. it sets the field of view (every shadowed
        # point lies on a ray from the light THROUGH a caster, so the
        # casters' cone already holds every shadow they throw - expanding the
        # fov only spends map texels on empty ground) and the near plane;
        # `shadow_extent` stretches the FAR plane so the ground the shadow
        # lands on is inside the frustum (it was clipped at dist + fit)
        fit = max(float(np.linalg.norm(pts - center, axis=1).max()), 1e-3) * 1.05
        reach = fit * self.shadow_extent
        # a map texel's size in world units at the casters (the normal offset)
        self._shadow_world_texel = 2.0 * fit / self.shadow_size
        if light.directional:
            direction = _normalize(light.position)
            eye = center + direction * fit * 3.0
            up = (0, 1, 0) if abs(direction[1]) < 0.99 else (1, 0, 0)
            return orthographic(fit, fit, fit * 2.0, fit * 3.0 + reach) @ look_at(eye, center, up)
        eye = np.asarray(light.position, np.float64)
        dist = float(np.linalg.norm(eye - center))
        if dist < 1e-6:
            return None
        # a light inside the casters' sphere gets the widest possible frustum
        fov = 2.0 * math.asin(min(0.985, fit / dist)) * 1.1 if dist > fit else 2.8
        up = (0, 1, 0) if abs(_normalize(center - eye)[1]) < 0.99 else (1, 0, 0)
        return perspective(fov, 1.0, max(0.02, dist - fit), dist + reach) @ look_at(eye, center, up)

    def end(self):
        depth_was = bool(gl.glIsEnabled(gl.GL_DEPTH_TEST))
        cull_was = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LESS)
        gl.glEnable(gl.GL_CULL_FACE)
        # ── pass 1: depth from the light ──
        light_matrix = self._light_space() if self.shadows else None
        shadow_tex = None
        if light_matrix is not None:
            fbo, shadow_tex = _shadow_target(self.gl_state, self.shadow_size)
            prev_fbo = _scalar(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
            prev_vp = gl.glGetIntegerv(gl.GL_VIEWPORT)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glViewport(0, 0, self.shadow_size, self.shadow_size)
            gl.glClear(gl.GL_DEPTH_BUFFER_BIT)
            # back-face culling as in the main pass: one-sided surfaces (a
            # disc, a plane) must write their own depth, and a self-shadow
            # against whatever lies right beneath them (front culling striped
            # the puckered shadow); the normal-offset bias handles the acne
            gl.glCullFace(gl.GL_BACK)
            for d in self._draws:
                if d["cast_shadow"]:
                    shadow_pass(self.gl_state, mesh=d["mesh"], model=d["model"], light_matrix=light_matrix)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, prev_fbo)
            gl.glViewport(int(prev_vp[0]), int(prev_vp[1]), int(prev_vp[2]), int(prev_vp[3]))
        # ── pass 2: the shaded frame; catchers last so they blend over
        # whatever they don't occlude - into the multisample target when
        # anti-aliasing is on ──
        msaa = None
        if self.samples > 1:
            try:
                msaa = _msaa_target(self.gl_state, self.key, self.width, self.height, self.samples)
            except Exception as e:      # no multisample support: draw aliased
                if not getattr(self, "_msaa_warned", False):
                    print(f"[pbr] anti-aliasing off: {e}")
                    self._msaa_warned = True
        self.fbo.bind()
        if msaa is not None:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, msaa[0])
            gl.glEnable(gl.GL_MULTISAMPLE)
        gl.glCullFace(gl.GL_BACK)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFuncSeparate(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA, gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glClearColor(*self.clear_color)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        env = self.environment
        common = dict(view=self._view, proj=self._proj,
                      camera_pos=tuple(float(c) for c in self.camera.eye),
                      light_pos=self._light_pos, light_color=self._light_color,
                      irradiance_map=env.irradiance if env is not None else None,
                      prefilter_map=env.prefiltered if env is not None else None,
                      brdf_lut=env.brdf_lut if env is not None else None,
                      env_max_lod=float(env.max_lod) if env is not None else 0.0,
                      env_strength=float(env.strength) if env is not None else 0.0,
                      env_tint=self.environment_tint, exposure=self.exposure,
                      shadow_map=shadow_tex, light_matrix=light_matrix if light_matrix is not None else np.eye(4),
                      shadow_on=light_matrix is not None, shadow_texel=1.0 / self.shadow_size,
                      shadow_softness=self.shadow_softness, shadow_opacity=self.shadow_opacity,
                      shadow_normal_offset=self._shadow_world_texel * 1.5)
        for d in sorted(self._draws, key=lambda d: d["shadow_catcher"]):
            pbr_pass(self.gl_state, mesh=d["mesh"], model=d["model"], normal_matrix=d["normal_matrix"],
                     color=d["color"], roughness=d["roughness"], metallic=d["metallic"], ao=d["ao"],
                     emissive=d["emissive"], shadow_catcher=d["shadow_catcher"], **common)
        self._draws = []
        if msaa is not None:
            # resolve: the multisample colour into the FBO's texture
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, msaa[0])
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.fbo.fbo)
            gl.glBlitFramebuffer(0, 0, self.width, self.height, 0, 0, self.width, self.height,
                                 gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo.fbo)
        if not depth_was:
            gl.glDisable(gl.GL_DEPTH_TEST)
        if not cull_was:
            gl.glDisable(gl.GL_CULL_FACE)
        if not blend_was:
            gl.glDisable(gl.GL_BLEND)
        self.fbo.unbind()
        return self.fbo


    def draw(self, mesh: Mesh, *, position=(0, 0, 0), rotation=(0, 0, 0), scale=(1, 1, 1),
             transform=None, color=(0.8, 0.8, 0.8), roughness=0.5, metallic=0.0, ao=1.0,
             emissive=(0.0, 0.0, 0.0), cast_shadow=True, shadow_catcher=False):
        """One draw of `mesh` with this material / transform (recorded;
        rendered by end_scene). `shadow_catcher`: the surface is invisible
        and shows only the shadow it receives. `cast_shadow=False` keeps a
        mesh out of the depth pass."""
        model = np.asarray(transform, np.float64) if transform is not None \
            else model_matrix(position, rotation, scale)
        self._draws.append(dict(
            mesh=mesh, model=model, normal_matrix=np.linalg.inv(model[:3, :3]).T,
            color=tuple(float(c) for c in color)[:3], roughness=float(roughness),
            metallic=float(metallic), ao=float(ao), emissive=tuple(float(c) for c in emissive)[:3],
            cast_shadow=bool(cast_shadow) and not shadow_catcher, shadow_catcher=bool(shadow_catcher)))


def begin_scene(gl_state: GLState, key, width, height, camera: Camera, lights=(),
                environment=None, exposure=1.0, clear_color=(0, 0, 0, 0), shadows=True,
                shadow_size=2048, shadow_softness=1.0, shadow_opacity=0.6,
                environment_tint=(1.0, 1.0, 1.0), shadow_extent=2.5, samples=4) -> Scene:
    """Open a frame: the FBO under `key` at width × height. Returns the
    Scene every draw_* takes as its first arg. Shadows (from light 0) are
    on by default; `shadow_softness` widens the PCF kernel in texels."""
    return Scene(gl_state, key, width, height, camera, lights, environment,
                 exposure, clear_color, shadows, shadow_size, shadow_softness, shadow_opacity,
                 environment_tint, shadow_extent, samples).begin()


def end_scene(scene: Scene):
    """Render the recorded draws (depth pass, then the shaded pass), restore
    GL state; returns the FBO (`.texture_id` for imgui.image)."""
    return scene.end()


def mesh_func(builder, **mesh_defaults):
    """Turn a mesh generator into an immediate-mode draw function:

        @mesh_func(cylinder_mesh, edge_radius=0.0, segments=48)
        def draw_cylinder(**kw): ...

    The decorated name becomes `draw_cylinder(scene, position=, rotation=,
    scale=, transform=, color=, roughness=, metallic=, ao=, emissive=,
    <mesh kwargs>)`. Kwargs naming the builder's own parameters
    (`edge_radius`, `segments`) select / build the mesh variant, cached on
    the scene's GLState under the builder name + those values; everything
    else is material and transform, handed to Scene.draw. The body of the
    decorated function is never called — like @shader_func, the signature
    is the contract."""
    mesh_keys = tuple(mesh_defaults)

    def deco(func):
        name = func.__name__

        def draw(scene: Scene, **kwargs):
            mesh_kwargs = {k: kwargs.pop(k, v) for k, v in mesh_defaults.items()}
            key = (name,) + tuple((k, _mesh_key_value(mesh_kwargs[k])) for k in mesh_keys)
            cache = scene.gl_state.peek(("pbr_mesh",) + key)
            if cache is None or key not in _mesh_counts:
                mesh = upload_mesh(scene.gl_state, key, builder(**mesh_kwargs))
            else:
                mesh = Mesh(cache[0], *_mesh_counts[key])
            scene.draw(mesh, **kwargs)
        draw.__name__ = name
        draw.__doc__ = func.__doc__
        draw.builder = builder
        return draw
    return deco


_mesh_counts: dict = {}


def _mesh_key_value(v):
    return round(float(v), 4) if isinstance(v, float) else v


@mesh_func(cube_mesh)
def draw_cube(scene, **kwargs):
    """Unit cube centred on `position`, sized by `scale`."""


@mesh_func(cylinder_mesh, edge_radius=0.0, segments=48)
def draw_cylinder(scene, **kwargs):
    """Unit cylinder (radius 1, height 1, axis Y) centred on `position`;
    `scale=(r, h, r)` sizes it, `edge_radius` (fraction of the radius)
    rounds its rim."""


@mesh_func(sphere_mesh, segments=48, rings=24)
def draw_sphere(scene, **kwargs):
    """Unit sphere centred on `position`; `scale` sets the radius."""


@mesh_func(plane_mesh)
def draw_plane(scene, **kwargs):
    """Unit square facing +Y centred on `position`; `scale=(w, 1, d)`."""


@mesh_func(extrude_polygon_mesh, points=((0, 0), (1, 0), (0, 1)), height=1.0)
def draw_prism(scene, **kwargs):
    """A convex polygon (`points`, (x, z) pairs, counter-clockwise from
    above) extruded `height` along Y, centred on `position`."""


@mesh_func(rounded_box_mesh, radius=0.1, segments=8)
def draw_rounded_box(scene, **kwargs):
    """Unit box centred on `position` with rounded edges (`radius` as a
    fraction of the half-size); `scale` sizes it — note the rounding scales
    with each axis, so keep the radius small on a flat, wide slab."""


def draw_mesh(scene, mesh, key=None, **kwargs):
    """Draw a MeshData (or a Model part) with the usual material / transform
    kwargs. `key` identifies the upload on the GLState — default: the
    MeshData's identity (a Model's parts are stable objects, so a loaded
    model uploads once)."""
    key = ("mesh", id(mesh)) if key is None else ("mesh",) + tuple(key)
    cache = scene.gl_state.peek(("pbr_mesh",) + key)
    if cache is None or key not in _mesh_counts:
        uploaded = upload_mesh(scene.gl_state, key, mesh)
    else:
        uploaded = Mesh(cache[0], *_mesh_counts[key])
    scene.draw(uploaded, **kwargs)