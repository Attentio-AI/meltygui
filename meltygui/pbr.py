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
function (define new shapes by decorating a generator). begin_scene binds
the FBO and clears; end_scene restores GL state and hands the FBO back.
Nothing is retained between frames but the GL resources (meshes, programs,
the environment), which GLState owns and dedupes by key.

Shading is learnopengl's Cook-Torrance BRDF, metallic / roughness workflow:
GGX / Trowbridge-Reitz normal distribution, Smith-Schlick geometry,
Fresnel-Schlick, Lambert diffuse weighted by (1 - F)(1 - metallic), up to
four point / directional lights (packed as two mat4 uniforms: rows of xyz +
kind, and rgb + intensity). Environment lighting is a BOX MAP — a cubemap
generated procedurally (a lit studio room or an outdoor sky), mip-mapped so
the split-sum approximation reads diffuse irradiance from the coarsest mip
and the specular reflection from the mip that matches the roughness, with
Karis's analytic environment BRDF. HDR is tone-mapped (ACES) and gamma
encoded at the end; the FBO holds straight alpha 1 on every covered pixel
and 0 where nothing was drawn, so views composite it over their own bg.
"""

from __future__ import annotations
import math

import numpy as np
import OpenGL.GL as gl

from src.lsd.gl_gui.gl_state import GLState, GLTexture, _scalar
from src.lsd.gl_gui.shader_func import shader_func


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


def plane_mesh():
    """Unit square in XZ facing +Y, centred on the origin."""
    positions = [(-0.5, 0, -0.5), (0.5, 0, -0.5), (0.5, 0, 0.5), (-0.5, 0, 0.5)]
    normals = [(0, 1, 0)] * 4
    return MeshData(positions, normals, [0, 2, 1, 0, 3, 2])


class Mesh:
    """A mesh uploaded to a GLState: the VAO plus its index count."""
    __slots__ = ("vao", "count")

    def __init__(self, vao, count):
        self.vao, self.count = vao, count


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
    return Mesh(vao, int(data.indices.shape[0]))


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
    """Up to MAX_LIGHTS lights → two 4x4 matrices: rows of (x, y, z, kind)
    with kind 0 = off, 1 = point, 2 = directional, and rows of (r, g, b,
    intensity). One mat4 uniform each — no array uniforms needed."""
    pos = np.zeros((4, 4)); col = np.zeros((4, 4))
    for i, light in enumerate(list(lights)[:MAX_LIGHTS]):
        pos[i, :3] = light.position
        pos[i, 3] = 2.0 if light.directional else 1.0
        col[i, :3] = light.color
        col[i, 3] = light.intensity
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


ENVIRONMENTS = {"studio": _env_studio, "outdoor": _env_outdoor}

# Cubemap face axis conventions (GL): (major axis,, u axis, v axis)
_FACES = (
    ((1, 0, 0), (0, 0, -1), (0, -1, 0)), ((-1, 0, 0), (0, 0, 1), (0, -1, 0)),
    ((0, 1, 0), (1, 0, 0), (0, 0, 1)), ((0, -1, 0), (1, 0, 0), (0, 0, -1)),
    ((0, 0, 1), (1, 0, 0), (0, -1, 0)), ((0, 0, -1), (-1, 0, 0), (0, -1, 0)),
)


def environment(gl_state: GLState, preset="studio", size=64, strength=1.0) -> GLTexture:
    """The box map: a mip-mapped RGB16F cubemap of the named preset,
    generated on the CPU once per (gl_state, preset, size). `strength` is
    stamped on the texture and scales the ambient contribution."""
    fn = ENVIRONMENTS[preset]

    def create():
        tex = _scalar(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, tex)
        grid = (np.arange(size) + 0.5) / size * 2.0 - 1.0
        u, v = np.meshgrid(grid, grid)      # v runs down the image (row 0 = top)
        for i, (axis, ua, va) in enumerate(_FACES):
            d = (np.asarray(axis, np.float64)[None, :] + u.reshape(-1, 1) * np.asarray(ua)[None, :]
                 + v.reshape(-1, 1) * np.asarray(va)[None, :])
            d = d / np.linalg.norm(d, axis=1, keepdims=True)
            face = fn(d).astype(np.float32).reshape(size, size, 3)
            gl.glTexImage2D(gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + i, 0, gl.GL_RGB16F, size, size, 0,
                            gl.GL_RGB, gl.GL_FLOAT, np.ascontiguousarray(face))
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        for wrap in (gl.GL_TEXTURE_WRAP_S, gl.GL_TEXTURE_WRAP_T, gl.GL_TEXTURE_WRAP_R):
            gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, wrap, gl.GL_CLAMP_TO_EDGE)
        gl.glGenerateMipmap(gl.GL_TEXTURE_CUBE_MAP)
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
        gl.glEnable(gl.GL_TEXTURE_CUBE_MAP_SEAMLESS)
        t = GLTexture(tex, gl.GL_TEXTURE_CUBE_MAP, (6, size, size), gl.GL_RGB16F)
        t.max_lod = float(int(math.log2(size)))
        return t

    def delete(t):
        gl.glDeleteTextures([t.texture_id])

    tex = gl_state.get(("pbr_env", preset, size), create, delete, deps=(preset, size))
    tex.strength = float(strength)
    return tex


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
uniform samplerCube env_map;
const float PI = 3.14159265359;

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
// Karis' analytic fit of the split-sum environment BRDF (no LUT texture)
vec2 env_brdf_approx(float NdotV, float rough) {
    const vec4 c0 = vec4(-1.0, -0.0275, -0.572, 0.022);
    const vec4 c1 = vec4(1.0, 0.0425, 1.04, -0.04);
    vec4 r = rough * c0 + c1;
    float a004 = min(r.x * r.x, exp2(-9.28 * NdotV)) * r.x + r.y;
    return vec2(-1.04, 1.04) * a004 + r.zw;
}
vec3 aces(vec3 x) {
    return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0);
}

void main() {
    vec3 N = normalize(v_normal);
    vec3 V = normalize(camera_pos - v_world);
    float rough = clamp(roughness, 0.04, 1.0);
    vec3 albedo = color;
    vec3 F0 = mix(vec3(0.04), albedo, metallic);

    // ── direct lighting: the packed lights, rows of (xyz, kind) / (rgb, I) ──
    vec3 Lo = vec3(0.0);
    for (int i = 0; i < 4; i++) {
        vec4 lp = light_pos[i];      // row i (shader_func uploads numpy row-major transposed)
        vec4 lc = light_color[i];
        if (lp.w < 0.5) continue;
        vec3 L; float attenuation;
        if (lp.w > 1.5) { L = normalize(lp.xyz); attenuation = 1.0; }
        else { vec3 to = lp.xyz - v_world; float d2 = max(dot(to, to), 1e-4);
               L = to * inversesqrt(d2); attenuation = 1.0 / d2; }
        vec3 H = normalize(V + L);
        vec3 radiance = lc.rgb * lc.w * attenuation;
        float NDF = distribution_ggx(N, H, rough);
        float G = geometry_smith(N, V, L, rough);
        vec3 F = fresnel_schlick(max(dot(H, V), 0.0), F0);
        vec3 kD = (vec3(1.0) - F) * (1.0 - metallic);
        float NdotL = max(dot(N, L), 0.0);
        vec3 specular = (NDF * G * F) / (4.0 * max(dot(N, V), 0.0) * NdotL + 1e-4);
        Lo += (kD * albedo / PI + specular) * radiance * NdotL;
    }

    // ── environment (the box map): split-sum with the mip chain as the
    // prefiltered radiance — coarsest mip ≈ irradiance, roughness picks the
    // reflection's mip; Karis' fit stands in for the BRDF LUT ──
    float NdotV = max(dot(N, V), 0.0);
    vec3 F = fresnel_schlick_roughness(NdotV, F0, rough);
    vec3 kD = (1.0 - F) * (1.0 - metallic);
    vec3 irradiance = textureLod(env_map, N, env_max_lod).rgb;
    vec3 diffuse = irradiance * albedo;
    vec3 R = reflect(-V, N);
    vec3 prefiltered = textureLod(env_map, R, rough * env_max_lod).rgb;
    vec2 brdf = env_brdf_approx(NdotV, rough);
    vec3 spec_ibl = prefiltered * (F * brdf.x + brdf.y);
    vec3 ambient = (kD * diffuse + spec_ibl) * ao * env_strength;

    vec3 c = ambient + Lo + emissive;
    c = aces(c * exposure);
    c = pow(c, vec3(1.0 / 2.2));
    FragColor = vec4(c, 1.0);
}
"""


@shader_func(fragment=PBR_FRAG, vertex=PBR_VERT)
def pbr_pass(gl_state: GLState = None, mesh=None, model=None, normal_matrix=None,
             view=None, proj=None, camera_pos=(0.0, 0.0, 5.0),
             color=(0.8, 0.8, 0.8), roughness=0.5, metallic=0.0, ao=1.0,
             emissive=(0.0, 0.0, 0.0), light_pos=None, light_color=None,
             env_map=None, env_max_lod=6.0, env_strength=1.0, exposure=1.0, **kwargs):
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
                 lights=(), environment=None, exposure=1.0, clear_color=(0, 0, 0, 0)):
        self.gl_state = gl_state
        self.key, self.width, self.height = key, int(width), int(height)
        self.camera = camera
        self.lights = list(lights)
        self.environment = environment
        self.exposure = float(exposure)
        self.clear_color = tuple(clear_color)
        self.fbo = None
        self._view = self._proj = None
        self._light_pos = self._light_color = None

    def begin(self):
        self.fbo = self.gl_state.fbo(self.key, self.width, self.height)
        self._view = self.camera.view()
        self._proj = self.camera.projection(self.width / max(1, self.height))
        self._light_pos, self._light_color = pack_lights(self.lights)
        self._depth_was = bool(gl.glIsEnabled(gl.GL_DEPTH_TEST))
        self._cull_was = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        self._blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))
        self.fbo.bind()
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LESS)
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK)
        gl.glDisable(gl.GL_BLEND)
        gl.glClearColor(*self.clear_color)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        return self

    def end(self):
        if not self._depth_was:
            gl.glDisable(gl.GL_DEPTH_TEST)
        if not self._cull_was:
            gl.glDisable(gl.GL_CULL_FACE)
        if self._blend_was:
            gl.glEnable(gl.GL_BLEND)
        self.fbo.unbind()
        return self.fbo


    def draw(self, mesh: Mesh, *, position=(0, 0, 0), rotation=(0, 0, 0), scale=(1, 1, 1),
             transform=None, color=(0.8, 0.8, 0.8), roughness=0.5, metallic=0.0, ao=1.0,
             emissive=(0.0, 0.0, 0.0)):
        """One draw of `mesh` with this material / transform."""
        model = np.asarray(transform, np.float64) if transform is not None \
            else model_matrix(position, rotation, scale)
        normal_matrix = np.linalg.inv(model[:3, :3]).T
        env = self.environment
        pbr_pass(self.gl_state, mesh=mesh, model=model, normal_matrix=normal_matrix,
                 view=self._view, proj=self._proj,
                 camera_pos=tuple(float(c) for c in self.camera.eye),
                 color=tuple(float(c) for c in color)[:3], roughness=float(roughness),
                 metallic=float(metallic), ao=float(ao),
                 emissive=tuple(float(c) for c in emissive)[:3],
                 light_pos=self._light_pos, light_color=self._light_color,
                 env_map=env, env_max_lod=float(getattr(env, "max_lod", 0.0)),
                 env_strength=float(getattr(env, "strength", 0.0)) if env is not None else 0.0,
                 exposure=self.exposure)


def begin_scene(gl_state: GLState, key, width, height, camera: Camera, lights=(),
                environment=None, exposure=1.0, clear_color=(0, 0, 0, 0)) -> Scene:
    """Bind a fresh frame: the FBO under `key` at width × height, cleared,
    depth test on. Returns the Scene every draw_* takes as its first arg."""
    return Scene(gl_state, key, width, height, camera, lights, environment,
                 exposure, clear_color).begin()


def end_scene(scene: Scene):
    """Restore GL state; returns the FBO (`.texture_id` for imgui.image)."""
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
            if cache is None:
                mesh = upload_mesh(scene.gl_state, key, builder(**mesh_kwargs))
            else:
                mesh = Mesh(cache[0], _mesh_counts[key])
            _mesh_counts.setdefault(key, mesh.count)
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
