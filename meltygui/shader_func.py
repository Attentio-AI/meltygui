"""@shader_func — kwargs-driven GLSL uniform injection.

Melty's parameter injection means a value travels by NAME from the call site
into whatever declares that name. shader_func extends the same idea into the
shader: the GLSL never declares plumbing uniforms — any kwarg whose name
appears as an identifier in a stage's source gets a `uniform <type> <name>;`
spliced in (type inferred from the Python value), and is set every call via
the program's own reflection table.

    VOXEL_FRAG = '''
    #version 330 core
    out vec4 FragColor;
    void main() { FragColor = vec4(uv * brightness, tilt, 1.0); }
    '''

    @shader_func(fragment=VOXEL_FRAG)
    def voxel_pass(gl_state: GLState = None, brightness=0.64, tilt=0.0,
                   volume_texture=None, **kwargs):
        # program is already bound, uniforms already set — body is draw calls
        gl.glBindVertexArray(gl_state.vao("fs_triangle"))
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

    # inside a @render_func, with gl_state auto-injected:
    voxel_pass(gl_state, brightness=brightness, tilt=tilt)

How it decides what to declare/set, per call:
- candidate values = body signature defaults overlaid with call kwargs
- a candidate becomes a uniform iff its name appears in a stage's
  (comment-stripped) identifiers AND its value maps to a GLSL type
  (infer_glsl_type — strings/objects return None and are simply body kwargs)
- already-declared uniforms in the source are never re-declared, just set
- setting is driven by glGetActiveUniform reflection, so over-matching is
  harmless: a name the compiler optimized out has no location and is skipped

There is deliberately no GLSL parser here. The transformation is purely
additive (declarations after the #version/#extension block) and the GL
compiler is the validator — its errors are surfaced with line numbers
remapped to the original source.

Compile lifecycle rides GLState.get with deps = the generated sources:
editing the GLSL (hotswap re-runs the decorator) changes the deps, so the
program recompiles next call and the old one is queued for deletion. A failed
compile keeps the last good program running (GLState's last-good semantics),
stores the remapped log on `.last_error`, and doesn't retry until the source
actually changes.

Threading: GL is main-thread-only in this app. Off the main thread (chain
converters, Background runs) a shader_func call is a silent no-op returning
None.
"""

import inspect
import re

import numpy as np
import OpenGL.GL as gl

from src.lsd.gl_gui.gl_state import GLState, GLTexture, _scalar, is_gl_thread

# Fullscreen triangle for fragment-only shader_funcs (no VBO needed; core
# profile still requires a VAO bound - gl_state.vao(key) provides an empty one).
DEFAULT_VERTEX_FULLSCREEN = """
#version 330 core
out vec2 uv;
void main() {
    vec2 pos = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    uv = pos;
    gl_Position = vec4(pos * 2.0 - 1.0, 0.0, 1.0);
}
"""


class ShaderError(RuntimeError):
    """Shader compile/link failure, message already remapped to original
    source line numbers."""


# ── GLSL source analysis (pure logic, unit-tested without GL) ────────────

_DIRECTIVE_RE = re.compile(r"^\s*#\s*(version|extension)\b")
_IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")
# `uniform ... name;` - possibly prefixed by layout/precision qualifiers,
# possibly an array. [^;{}]* keeps us out of interface blocks. Multi-declarator
# lines (`uniform float a, b;`) only register the last name - declare such
# uniforms yourself.
_UNIFORM_DECL_RE = re.compile(r"\buniform\b[^;{}]*?(\w+)\s*(?:\[[^\]]*\])?\s*;")


def strip_comments(source):
    """GLSL minus // and /* */ comments, line structure preserved (GLSL has
    no string literals, so this can't over-strip)."""
    source = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), source, flags=re.S)
    return re.sub(r"//[^\n]*", "", source)


def glsl_identifiers(source):
    return frozenset(_IDENT_RE.findall(strip_comments(source)))


def declared_uniform_names(source):
    return frozenset(m.group(1) for m in _UNIFORM_DECL_RE.finditer(strip_comments(source)))


def infer_glsl_type(value):
    """GLSL uniform type for a Python value, or None if the value isn't
    uniform-able (strings, objects, None — those stay plain body kwargs)."""
    if isinstance(value, (bool, np.bool_)):
        return "bool"
    if isinstance(value, (int, np.integer)):
        return "int"
    if isinstance(value, (float, np.floating)):
        return "float"
    # Name-compared (not isinstance) so textures created before a hotswap of
    # gl_state.py keep working the same logic as set_default's type check.
    if type(value).__name__ == "GLTexture":
        if value.target == int(gl.GL_TEXTURE_3D):
            return "sampler3D"
        if value.target == int(gl.GL_TEXTURE_1D):
            return "sampler1D"
        if value.target == int(gl.GL_TEXTURE_CUBE_MAP):
            return "samplerCube"
        return "sampler2D"
    tname = type(value).__name__
    if type(value).__module__ == "glm":
        if tname.startswith("d"):   # dvec3/dmat4x4 - setter upcasts to f32
            tname = tname[1:]
        if tname in ("vec2", "vec3", "vec4",
                     "ivec2", "ivec3", "ivec4", "bvec2", "bvec3", "bvec4"):
            return tname
        square = re.fullmatch(r"mat(\d)(?:x(\d))?", tname)
        if square and (square.group(2) is None or square.group(2) == square.group(1)):
            return f"mat{square.group(1)}"
        return None
    if isinstance(value, (tuple, list)):
        if 2 <= len(value) <= 4 and all(isinstance(v, (int, float, np.integer, np.floating))
                                        for v in value):
            return f"vec{len(value)}"
        return None
    if isinstance(value, np.ndarray):
        if value.ndim == 1 and 2 <= value.shape[0] <= 4:
            return f"vec{value.shape[0]}"
        if value.ndim == 2 and value.shape[0] == value.shape[1] and 2 <= value.shape[0] <= 4:
            return f"mat{value.shape[0]}"
        return None
    return None


def inject_uniforms(source, decls):
    """Splice `uniform <type> <name>;` lines (sorted, for stable deps hashing)
    right after the #version/#extension block. Returns (generated_source,
    insert_line_index, n_inserted_lines) — the last two drive error-log line
    remapping."""
    lines = source.split("\n")
    idx = 0
    for i, line in enumerate(lines):
        if _DIRECTIVE_RE.match(line):
            idx = i + 1
    if not decls:
        return source, idx, 0
    block = ["// shader_func: auto-injected uniforms"]
    block += [f"uniform {glsl_type} {name};" for name, glsl_type in sorted(decls)]
    return "\n".join(lines[:idx] + block + lines[idx:]), idx, len(block)


def _remap_log(log, insert_line, n_inserted):
    """Rewrite driver error line numbers (NVIDIA `0(57)`, Mesa `0:57(8)`)
    back to the original, pre-injection source."""
    if n_inserted == 0:
        return log

    def fix(ln):
        ln = int(ln)
        if ln > insert_line + n_inserted:
            return str(ln - n_inserted)
        if ln > insert_line:
            return f"{ln}<injected uniform block>"
        return str(ln)

    new, count = re.subn(r"(\d+):(\d+)(\()",
                         lambda m: f"{m.group(1)}:{fix(m.group(2))}{m.group(3)}", log)
    if count:
        return new
    new, _ = re.subn(r"(\d+)\((\d+)\)",
                     lambda m: f"{m.group(1)}({fix(m.group(2))})", log)
    return new


# ── compile / link / set ──────────────────────────────────────────────

def _compile_stage(kind, enum, generated, insert_line, n_inserted):
    shader = _scalar(gl.glCreateShader(enum))
    gl.glShaderSource(shader, generated)
    gl.glCompileShader(shader)
    if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
        log = gl.glGetShaderInfoLog(shader)
        gl.glDeleteShader(shader)
        log = log.decode(errors="replace") if isinstance(log, bytes) else str(log)
        raise ShaderError(f"{kind} shader: {_remap_log(log.strip(), insert_line, n_inserted)}")
    return shader


def _link_program(stage_shaders):
    program = _scalar(gl.glCreateProgram())
    for shader in stage_shaders:
        gl.glAttachShader(program, shader)
    gl.glLinkProgram(program)
    for shader in stage_shaders:
        gl.glDetachShader(program, shader)
        gl.glDeleteShader(shader)
    if not gl.glGetProgramiv(program, gl.GL_LINK_STATUS):
        log = gl.glGetProgramInfoLog(program)
        gl.glDeleteProgram(program)
        log = log.decode(errors="replace") if isinstance(log, bytes) else str(log)
        raise ShaderError(f"link: {log.strip()}")
    return program


_SAMPLER_TARGETS = {
    int(gl.GL_SAMPLER_1D): gl.GL_TEXTURE_1D,
    int(gl.GL_SAMPLER_2D): gl.GL_TEXTURE_2D,
    int(gl.GL_SAMPLER_3D): gl.GL_TEXTURE_3D,
    int(gl.GL_SAMPLER_CUBE): gl.GL_TEXTURE_CUBE_MAP,
    int(gl.GL_SAMPLER_2D_ARRAY): gl.GL_TEXTURE_2D_ARRAY,
}


def reflect_uniforms(program):
    """{name: (location, gl_type, size)} for every active uniform — the
    ground truth for what to set. Anything the compiler optimized out simply
    isn't here."""
    out = {}
    count = _scalar(gl.glGetProgramiv(program, gl.GL_ACTIVE_UNIFORMS))
    for i in range(count):
        name, size, utype = gl.glGetActiveUniform(program, i)
        name = name.decode(errors="replace") if isinstance(name, bytes) else str(name)
        name = name.split("[")[0]
        loc = gl.glGetUniformLocation(program, name)
        if loc >= 0:
            out[name] = (int(loc), int(utype), int(size))
    return out


def _as_f32(v):
    return np.ascontiguousarray(v, dtype=np.float32)


def _as_i32(v):
    return np.ascontiguousarray(v, dtype=np.int32)


def _set_matrix(fn, loc, val):
    if type(val).__module__ == "glm":
        # PyGLM uses the buffer protocol with column-major memory: upload
        # as-is. Row-major numpy math matrices need the transpose flag.
        fn(loc, 1, gl.GL_FALSE, np.asarray(val, dtype=np.float32))
    else:
        fn(loc, 1, gl.GL_TRUE, _as_f32(val))


_SETTERS = {
    int(gl.GL_FLOAT): lambda loc, v: gl.glUniform1f(loc, float(v)),
    int(gl.GL_INT): lambda loc, v: gl.glUniform1i(loc, int(v)),
    int(gl.GL_UNSIGNED_INT): lambda loc, v: gl.glUniform1ui(loc, int(v)),
    int(gl.GL_BOOL): lambda loc, v: gl.glUniform1i(loc, 1 if v else 0),
    int(gl.GL_FLOAT_VEC2): lambda loc, v: gl.glUniform2fv(loc, 1, _as_f32(v)),
    int(gl.GL_FLOAT_VEC3): lambda loc, v: gl.glUniform3fv(loc, 1, _as_f32(v)),
    int(gl.GL_FLOAT_VEC4): lambda loc, v: gl.glUniform4fv(loc, 1, _as_f32(v)),
    int(gl.GL_INT_VEC2): lambda loc, v: gl.glUniform2iv(loc, 1, _as_i32(v)),
    int(gl.GL_INT_VEC3): lambda loc, v: gl.glUniform3iv(loc, 1, _as_i32(v)),
    int(gl.GL_INT_VEC4): lambda loc, v: gl.glUniform4iv(loc, 1, _as_i32(v)),
    int(gl.GL_FLOAT_MAT2): lambda loc, v: _set_matrix(gl.glUniformMatrix2fv, loc, v),
    int(gl.GL_FLOAT_MAT3): lambda loc, v: _set_matrix(gl.glUniformMatrix3fv, loc, v),
    int(gl.GL_FLOAT_MAT4): lambda loc, v: _set_matrix(gl.glUniformMatrix4fv, loc, v),
}


class _Stage:
    __slots__ = ("kind", "enum", "source", "identifiers", "predeclared")

    def __init__(self, kind, enum, source):
        self.kind = kind
        self.enum = enum
        self.source = source
        self.identifiers = glsl_identifiers(source)
        self.predeclared = declared_uniform_names(source)


class _ProgramRecord:
    __slots__ = ("program", "uniforms", "sampler_units")

    def __init__(self, program):
        self.program = program
        self.uniforms = reflect_uniforms(program)
        # Texture units assigned by sorted uniform name - deterministic across
        # recompiles, so a given sampler keeps its unit as the source evolves.
        self.sampler_units = {
            name: unit for unit, name in enumerate(sorted(
                n for n, (_, t, _s) in self.uniforms.items() if t in _SAMPLER_TARGETS))
        }


class ShaderFunc:

    def __init__(self, func, fragment, vertex=None, name=None):
        if not fragment:
            raise TypeError("@shader_func requires fragment GLSL: @shader_func(fragment=...)")
        self._func = func
        self._name = name or f"{func.__module__}.{func.__qualname__}"
        self.__name__ = getattr(func, "__name__", self._name)
        self.__doc__ = getattr(func, "__doc__", None)
        self.stages = [
            _Stage("vertex", gl.GL_VERTEX_SHADER, vertex or DEFAULT_VERTEX_FULLSCREEN),
            _Stage("fragment", gl.GL_FRAGMENT_SHADER, fragment),
        ]

        sig = inspect.signature(func)
        self._wants_var_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD
                                     for p in sig.parameters.values())
        self._body_params = {n for n, p in sig.parameters.items()
                             if p.kind not in (inspect.Parameter.VAR_KEYWORD,
                                               inspect.Parameter.VAR_POSITIONAL)}
        self._defaults = {n: p.default for n, p in sig.parameters.items()
                          if p.default is not inspect.Parameter.empty}
        self._skip = {"gl_state", "program"}

        self._gen_cache = {}        # tuple of decl tuples -> [(src, idx, n)]
        # Failure latch is per (deps, gl_state): a bad source is retried at
        # most ONCE PER GLSTATE - never globally. A global latch starved every
        # window created after one transient failure (no last-good record →
        # silent None → permanently empty view) while older windows kept
        # rendering on cached programs.
        self._failed_deps = None
        self._failed_states = set()   # id(gl_state) that has tried _failed_deps
        self.last_error = None
        self.last_generated = {}    # stage kind -> generated source (debugging)

    def _generated_stages(self, stage_decls):
        key = tuple(tuple(sorted(d)) for d in stage_decls)
        gen = self._gen_cache.get(key)
        if gen is None:
            gen = [inject_uniforms(stage.source, decls)
                   for stage, decls in zip(self.stages, stage_decls)]
            self._gen_cache[key] = gen
        return gen

    def __call__(self, gl_state: GLState = None, **kwargs):
        # Duck-typed (hotswap of gl_state.py leaves live instances of the
        # old class, so typing would spuriously fail).
        if gl_state is None or not (hasattr(gl_state, "get") and hasattr(gl_state, "peek")):
            raise TypeError(
                f"shader_func {self.__name__!r} needs a GLState: declare "
                f"`gl_state: GLState = None` on the enclosing @render_func and "
                f"forward it — {self.__name__}(gl_state, ...)")
        if not is_gl_thread():
            return None   # Render mode / Background thread - no GL here

        merged = {**self._defaults, **kwargs}

        # Which candidates become uniforms, per-stage.
        stage_decls = [[] for _ in self.stages]
        values = {}
        for kw_name, val in merged.items():
            if kw_name in self._skip:
                continue
            glsl_type = infer_glsl_type(val)
            if glsl_type is None:
                continue
            used = False
            for stage, decls in zip(self.stages, stage_decls):
                if kw_name in stage.identifiers:
                    used = True
                    if kw_name not in stage.predeclared:
                        decls.append((kw_name, glsl_type))
            if used:
                values[kw_name] = val

        gen = self._generated_stages(stage_decls)
        self.last_generated = {stage.kind: g[0] for stage, g in zip(self.stages, gen)}
        deps = tuple(g[0] for g in gen)
        key = ("shader_func", self._name)

        def create():
            compiled = []
            try:
                for stage, (src, idx, n) in zip(self.stages, gen):
                    compiled.append(_compile_stage(stage.kind, stage.enum, src, idx, n))
            except Exception:
                for shader in compiled:
                    gl.glDeleteShader(shader)
                raise
            return _ProgramRecord(_link_program(compiled))

        def delete(rec):
            gl.glDeleteProgram(rec.program)

        if deps == self._failed_deps and id(gl_state) in self._failed_states:
            rec = gl_state.peek(key)       # known failure FOR THIS STATE: no retry
        else:
            try:
                rec = gl_state.get(key, create, delete, deps=deps)
                self.last_error = None
                self._failed_deps = None
                self._failed_states.clear()
            except ShaderError as e:
                if str(e) != self.last_error:
                    print(f"[shader_func] {self._name}: {e}")
                self.last_error = str(e)
                if deps != self._failed_deps:
                    self._failed_deps = deps
                    self._failed_states = set()
                self._failed_states.add(id(gl_state))
                rec = gl_state.peek(key)   # last good program, if any
        if rec is None:
            return None

        prev_program = _scalar(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM))
        gl.glUseProgram(rec.program)
        try:
            for uname, val in values.items():
                info = rec.uniforms.get(uname)
                if info is None:
                    continue   # optimized out (or only matched a non-uniform use)
                loc, utype, _size = info
                target = _SAMPLER_TARGETS.get(utype)
                if target is not None:
                    unit = rec.sampler_units.get(uname, 0)
                    gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
                    if type(val).__name__ == "GLTexture":
                        gl.glBindTexture(val.target, val.texture_id)
                    else:
                        # Raw texture id for a predeclared sampler - bind
                        # to the target implied by the sampler type.
                        gl.glBindTexture(target, int(val))
                    gl.glUniform1i(loc, unit)
                    continue
                setter = _SETTERS.get(utype)
                if setter is None:
                    continue
                try:
                    setter(loc, val)
                except Exception as e:
                    print(f"[shader_func] {self._name}: setting uniform "
                          f"{uname!r} from {type(val).__name__} failed: {e}")

            body_kwargs = dict(merged)
            body_kwargs["gl_state"] = gl_state
            body_kwargs["program"] = rec.program
            if not self._wants_var_kwargs:
                body_kwargs = {k: v for k, v in body_kwargs.items()
                               if k in self._body_params}
            return self._func(**body_kwargs)
        finally:
            gl.glBindVertexArray(0)
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glUseProgram(prev_program)

    def __repr__(self):
        state = f"error: {self.last_error.splitlines()[0]}" if self.last_error else "ok"
        return f"ShaderFunc({self._name}, {state})"


def shader_func(fragment=None, vertex=None, name=None):
    """Decorator: `@shader_func(fragment=FRAG)` or
    `@shader_func(fragment=FRAG, vertex=VERT)`. With no vertex source the
    fullscreen-triangle vertex stage is used (body: bind an empty VAO via
    gl_state.vao(key) and glDrawArrays(GL_TRIANGLES, 0, 3))."""
    if callable(fragment):
        raise TypeError("@shader_func needs GLSL — use @shader_func(fragment=...)")

    def deco(func):
        return ShaderFunc(func, fragment=fragment, vertex=vertex, name=name)

    return deco