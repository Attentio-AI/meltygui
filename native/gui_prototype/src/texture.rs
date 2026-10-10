//! Throwaway desktop OpenGL command replay. No framebuffer snapshots or masks.
//! The bridge copies finalized ImGui buffers; all GPU upload/replay is native.
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use std::{
    collections::HashMap,
    ffi::{c_void, CString},
    ptr,
};

#[link(name = "GL")]
extern "C" {
    fn glGetIntegerv(p: u32, v: *mut i32);
    fn glGetFloatv(p: u32, v: *mut f32);
    fn glGetBooleanv(p: u32, v: *mut u8);
    fn glIsEnabled(p: u32) -> u8;
    fn glEnable(p: u32);
    fn glDisable(p: u32);
    fn glGenTextures(n: i32, p: *mut u32);
    fn glDeleteTextures(n: i32, p: *const u32);
    fn glBindTexture(t: u32, p: u32);
    fn glActiveTexture(p: u32);
    fn glBindSampler(unit: u32, s: u32);
    fn glTexImage2D(
        t: u32,
        l: i32,
        i: i32,
        w: i32,
        h: i32,
        b: i32,
        f: u32,
        k: u32,
        d: *const c_void,
    );
    fn glTexParameteri(t: u32, p: u32, v: i32);
    fn glGenFramebuffers(n: i32, p: *mut u32);
    fn glDeleteFramebuffers(n: i32, p: *const u32);
    fn glBindFramebuffer(t: u32, p: u32);
    fn glFramebufferTexture2D(t: u32, a: u32, tt: u32, tex: u32, l: i32);
    fn glCheckFramebufferStatus(t: u32) -> u32;
    fn glGenVertexArrays(n: i32, p: *mut u32);
    fn glDeleteVertexArrays(n: i32, p: *const u32);
    fn glBindVertexArray(p: u32);
    fn glGenBuffers(n: i32, p: *mut u32);
    fn glDeleteBuffers(n: i32, p: *const u32);
    fn glBindBuffer(t: u32, p: u32);
    fn glBufferData(t: u32, s: isize, d: *const c_void, u: u32);
    fn glEnableVertexAttribArray(i: u32);
    fn glVertexAttribPointer(i: u32, s: i32, t: u32, n: u8, stride: i32, p: *const c_void);
    fn glCreateShader(k: u32) -> u32;
    fn glShaderSource(s: u32, n: i32, p: *const *const i8, l: *const i32);
    fn glCompileShader(s: u32);
    fn glGetShaderiv(s: u32, p: u32, v: *mut i32);
    fn glGetShaderInfoLog(s: u32, n: i32, l: *mut i32, p: *mut i8);
    fn glDeleteShader(s: u32);
    fn glCreateProgram() -> u32;
    fn glAttachShader(p: u32, s: u32);
    fn glLinkProgram(p: u32);
    fn glGetProgramiv(s: u32, p: u32, v: *mut i32);
    fn glGetProgramInfoLog(s: u32, n: i32, l: *mut i32, p: *mut i8);
    fn glDeleteProgram(p: u32);
    fn glUseProgram(p: u32);
    fn glGetUniformLocation(p: u32, n: *const i8) -> i32;
    fn glUniform2f(l: i32, x: f32, y: f32);
    fn glUniform1i(l: i32, x: i32);
    fn glViewport(x: i32, y: i32, w: i32, h: i32);
    fn glScissor(x: i32, y: i32, w: i32, h: i32);
    fn glBlendEquationSeparate(r: u32, a: u32);
    fn glBlendFuncSeparate(sr: u32, dr: u32, sa: u32, da: u32);
    fn glColorMask(r: u8, g: u8, b: u8, a: u8);
    fn glClearColor(r: f32, g: f32, b: f32, a: f32);
    fn glClear(m: u32);
    fn glDrawElementsBaseVertex(m: u32, n: i32, t: u32, p: *const c_void, b: i32);
}
const TEX: u32 = 0x0DE1;
const FB: u32 = 0x8D40;
const ARRAY: u32 = 0x8892;
const ELEMENT: u32 = 0x8893;
unsafe fn integer(p: u32) -> i32 {
    let mut v = 0;
    glGetIntegerv(p, &mut v);
    v
}
struct Guard {
    ints: Vec<(u32, i32)>,
    viewport: [i32; 4],
    scissor: [i32; 4],
    color: [f32; 4],
    mask: [u8; 4],
    enabled: Vec<(u32, bool)>,
    tex0: i32,
    sampler0: i32,
}
impl Guard {
    unsafe fn new() -> Self {
        let ints = [
            0x8CA6, 0x8CAA, 0x8B8D, 0x85B5, 0x8894, 0x84E0, 0x80C9, 0x80C8, 0x80CB, 0x80CA, 0x8009,
            0x883D,
        ]
        .iter()
        .map(|p| (*p, integer(*p)))
        .collect();
        let mut s = Self {
            ints,
            viewport: [0; 4],
            scissor: [0; 4],
            color: [0.; 4],
            mask: [0; 4],
            enabled: vec![],
            tex0: 0,
            sampler0: 0,
        };
        glGetIntegerv(0x0BA2, s.viewport.as_mut_ptr());
        glGetIntegerv(0x0C10, s.scissor.as_mut_ptr());
        glGetFloatv(0x0C22, s.color.as_mut_ptr());
        glGetBooleanv(0x0C23, s.mask.as_mut_ptr());
        s.enabled = [0x0BE2, 0x0C11, 0x0B71, 0x0B44, 0x0B90, 0x8DB9]
            .iter()
            .map(|p| (*p, glIsEnabled(*p) != 0))
            .collect();
        glActiveTexture(0x84C0);
        s.tex0 = integer(0x8069);
        s.sampler0 = integer(0x8919);
        s
    }
    fn get(&self, p: u32) -> u32 {
        self.ints.iter().find(|x| x.0 == p).unwrap().1 as u32
    }
}
impl Drop for Guard {
    fn drop(&mut self) {
        unsafe {
            glBindFramebuffer(0x8CA9, self.get(0x8CA6));
            glBindFramebuffer(0x8CA8, self.get(0x8CAA));
            glUseProgram(self.get(0x8B8D));
            glBindVertexArray(self.get(0x85B5));
            glBindBuffer(ARRAY, self.get(0x8894));
            glBindTexture(TEX, self.tex0 as u32);
            glBindSampler(0, self.sampler0 as u32);
            glActiveTexture(self.get(0x84E0));
            glViewport(
                self.viewport[0],
                self.viewport[1],
                self.viewport[2],
                self.viewport[3],
            );
            glScissor(
                self.scissor[0],
                self.scissor[1],
                self.scissor[2],
                self.scissor[3],
            );
            glClearColor(self.color[0], self.color[1], self.color[2], self.color[3]);
            glColorMask(self.mask[0], self.mask[1], self.mask[2], self.mask[3]);
            glBlendEquationSeparate(self.get(0x8009), self.get(0x883D));
            glBlendFuncSeparate(
                self.get(0x80C9),
                self.get(0x80C8),
                self.get(0x80CB),
                self.get(0x80CA),
            );
            for (p, on) in &self.enabled {
                if *on {
                    glEnable(*p)
                } else {
                    glDisable(*p)
                }
            }
        }
    }
}

struct Target {
    tex: u32,
    fbo: u32,
    w: i32,
    h: i32,
}
impl Target {
    unsafe fn new(w: i32, h: i32) -> PyResult<Self> {
        let mut t = Self {
            tex: 0,
            fbo: 0,
            w,
            h,
        };
        glGenTextures(1, &mut t.tex);
        glBindTexture(TEX, t.tex);
        glTexImage2D(TEX, 0, 0x8058, w, h, 0, 0x1908, 0x1401, ptr::null());
        for p in [0x2801, 0x2800] {
            glTexParameteri(TEX, p, 0x2601);
        }
        for p in [0x2802, 0x2803] {
            glTexParameteri(TEX, p, 0x812F);
        }
        glGenFramebuffers(1, &mut t.fbo);
        glBindFramebuffer(FB, t.fbo);
        glFramebufferTexture2D(FB, 0x8CE0, TEX, t.tex, 0);
        if glCheckFramebufferStatus(FB) != 0x8CD5 {
            t.delete();
            return Err(PyRuntimeError::new_err(
                "prototype capture framebuffer incomplete",
            ));
        }
        Ok(t)
    }
    unsafe fn delete(&self) {
        glDeleteFramebuffers(1, &self.fbo);
        glDeleteTextures(1, &self.tex);
    }
}
// Texture, element count, clip rectangle, index offset, vertex offset.
type Command = (u32, i32, (f32, f32, f32, f32), usize, i32);
struct Packet {
    vertices: Vec<u8>,
    indices: Vec<u8>,
    commands: Vec<Command>,
}

#[pyclass(unsendable)]
pub struct TextureCache {
    program: u32,
    vao: u32,
    vbo: u32,
    ebo: u32,
    size_loc: i32,
    resolve_loc: i32,
    targets: HashMap<u64, Target>,
    packets: HashMap<u64, Vec<Packet>>,
    scratch: Vec<Target>,
    scratch_allocations: u64,
    renders: u64,
    uploads: u64,
    budget: usize,
}

unsafe fn shader(kind: u32, src: &str) -> PyResult<u32> {
    let s = glCreateShader(kind);
    let c = CString::new(src).unwrap();
    glShaderSource(s, 1, &c.as_ptr(), ptr::null());
    glCompileShader(s);
    let mut ok = 0;
    glGetShaderiv(s, 0x8B81, &mut ok);
    if ok == 0 {
        let mut b = vec![0i8; 4096];
        glGetShaderInfoLog(s, 4096, ptr::null_mut(), b.as_mut_ptr());
        glDeleteShader(s);
        return Err(PyRuntimeError::new_err(
            std::ffi::CStr::from_ptr(b.as_ptr())
                .to_string_lossy()
                .to_string(),
        ));
    }
    Ok(s)
}

#[pymethods]
impl TextureCache {
    #[new]
    #[pyo3(signature=(budget=268435456))]
    fn new(budget: usize) -> PyResult<Self> {
        unsafe {
            let _guard = Guard::new();
            let vs=shader(0x8B31,"#version 330 core\nlayout(location=0) in vec2 pos;layout(location=1) in vec2 uv;layout(location=2) in vec4 col;uniform vec2 size;out vec2 UV;out vec4 Color;void main(){UV=uv;Color=col;gl_Position=vec4(pos.x/size.x*2.-1.,1.-pos.y/size.y*2.,0.,1.);}")?;
            let fs=shader(0x8B30,"#version 330 core\nin vec2 UV;in vec4 Color;uniform sampler2D image;uniform int resolve;out vec4 Out;void main(){vec4 c=texture(image,UV)*Color;if(resolve!=0&&c.a>0.)c.rgb/=c.a;Out=c;}")?;
            let p = glCreateProgram();
            glAttachShader(p, vs);
            glAttachShader(p, fs);
            glLinkProgram(p);
            glDeleteShader(vs);
            glDeleteShader(fs);
            let mut ok = 0;
            glGetProgramiv(p, 0x8B82, &mut ok);
            if ok == 0 {
                let mut b = vec![0i8; 4096];
                glGetProgramInfoLog(p, 4096, ptr::null_mut(), b.as_mut_ptr());
                glDeleteProgram(p);
                return Err(PyRuntimeError::new_err(
                    std::ffi::CStr::from_ptr(b.as_ptr())
                        .to_string_lossy()
                        .to_string(),
                ));
            }
            let mut s = Self {
                program: p,
                vao: 0,
                vbo: 0,
                ebo: 0,
                size_loc: glGetUniformLocation(p, b"size\0".as_ptr().cast()),
                resolve_loc: glGetUniformLocation(p, b"resolve\0".as_ptr().cast()),
                targets: HashMap::new(),
                packets: HashMap::new(),
                scratch: Vec::new(),
                scratch_allocations: 0,
                renders: 0,
                uploads: 0,
                budget,
            };
            glGenVertexArrays(1, &mut s.vao);
            glGenBuffers(1, &mut s.vbo);
            glGenBuffers(1, &mut s.ebo);
            glBindVertexArray(s.vao);
            glBindBuffer(ARRAY, s.vbo);
            glBindBuffer(ELEMENT, s.ebo);
            for (i, n, t, norm, offset) in [
                (0, 2, 0x1406, 0, 0),
                (1, 2, 0x1406, 0, 8),
                (2, 4, 0x1401, 1, 16),
            ] {
                glEnableVertexAttribArray(i);
                glVertexAttribPointer(i, n, t, norm, 20, offset as *const c_void);
            }
            glUseProgram(p);
            glUniform1i(glGetUniformLocation(p, b"image\0".as_ptr().cast()), 0);
            Ok(s)
        }
    }
    fn target(&mut self, id: u64, w: i32, h: i32) -> PyResult<u32> {
        unsafe {
            if w <= 0 || h <= 0 || w > 8192 || h > 8192 {
                return Err(PyRuntimeError::new_err(
                    "cache boundary must be 1..8192 pixels",
                ));
            }
            if let Some(t) = self.targets.get(&id) {
                if t.w == w && t.h == h {
                    return Ok(t.tex);
                }
            }
            // No hidden eviction that could leave retained packets sampling stale IDs.
            // This bounded experiment fails explicitly when its budget is exceeded.
            let used: usize = self
                .targets
                .iter()
                .filter(|(k, _)| **k != id)
                .map(|(_, t)| (t.w * t.h * 4) as usize)
                .sum();
            let next_bytes = (w * h * 4) as usize;
            let largest = self.targets.iter().filter(|(k, _)| **k != id)
                .map(|(_, t)| (t.w * t.h * 4) as usize)
                .max().unwrap_or(0).max(next_bytes);
            // Reserve enough space to replay any target, even after later allocations.
            if used + next_bytes + largest > self.budget {
                return Err(PyRuntimeError::new_err("prototype texture budget exceeded"));
            }
            let _guard = Guard::new();
            while used + next_bytes + self.scratch_bytes() > self.budget {
                self.scratch.remove(0).delete();
            }
            if let Some(t) = self.targets.get_mut(&id) {
                // Keep the texture name stable: parent command packets reference it.
                glBindTexture(TEX, t.tex);
                glTexImage2D(TEX, 0, 0x8058, w, h, 0, 0x1908, 0x1401, ptr::null());
                t.w = w;
                t.h = h;
                return Ok(t.tex);
            }
            let t = Target::new(w, h)?;
            let tex = t.tex;
            self.targets.insert(id, t);
            Ok(tex)
        }
    }
    fn begin_packet(&mut self, id: u64) {
        self.packets.insert(id, vec![]);
    }
    fn place_child(
        &mut self,
        parent: u64,
        child: u64,
        rect: (f32, f32, f32, f32),
        clip: (f32, f32, f32, f32),
    ) -> PyResult<()> {
        let target = self
            .targets
            .get(&child)
            .ok_or_else(|| PyRuntimeError::new_err("missing child texture"))?;
        let tex = target.tex;
        // Cached pixels are never stretched to a fractional layout allocation.
        // Crop at snapped layout edges; keep one source texel per output pixel.
        let size = (target.w as f32, target.h as f32);
        let origin = ((rect.0 + 0.5).floor(), (rect.1 + 0.5).floor());
        let clip = ((clip.0 + 0.5).floor(), (clip.1 + 0.5).floor(),
                    (clip.2 + 0.5).floor(), (clip.3 + 0.5).floor());
        // Framework-generated child images are isolated texture commands. Keep
        // their place in the draw list while changing geometry without Python
        // execution of the parent. Other packet commands remain byte-identical.
        if let Some(packets) = self.packets.get_mut(&parent) {
            for packet in packets {
                for command in &mut packet.commands {
                    if command.0 != tex {
                        continue;
                    }
                    let (_, count, _, offset, base) = *command;
                    if count != 6 {
                        return Err(PyRuntimeError::new_err(
                            "layout child requires one framework image quad",
                        ));
                    }
                    let mut indices = std::collections::BTreeSet::new();
                    for i in offset..offset + 6 {
                        let at = u32::from_ne_bytes(
                            packet.indices[i * 4..i * 4 + 4].try_into().unwrap(),
                        ) as usize
                            + base as usize;
                        if at * 20 + 20 > packet.vertices.len() {
                            return Err(PyRuntimeError::new_err("invalid child image vertex"));
                        }
                        indices.insert(at);
                    }
                    for at in indices {
                        let p = at * 20;
                        let u =
                            f32::from_ne_bytes(packet.vertices[p + 8..p + 12].try_into().unwrap());
                        let v =
                            f32::from_ne_bytes(packet.vertices[p + 12..p + 16].try_into().unwrap());
                        packet.vertices[p..p + 4]
                            .copy_from_slice(&(origin.0 + u * size.0).to_ne_bytes());
                        packet.vertices[p + 4..p + 8]
                            .copy_from_slice(&(origin.1 + (1. - v) * size.1).to_ne_bytes());
                    }
                    command.2 = clip;
                }
            }
        }
        Ok(())
    }
    fn add_packet(
        &mut self,
        id: u64,
        vertices: Vec<u8>,
        indices: Vec<u8>,
        commands: Vec<Command>,
    ) -> PyResult<()> {
        if vertices.len() % 20 != 0 || indices.len() % 4 != 0 {
            return Err(PyRuntimeError::new_err(
                "expected ImGui 20-byte vertices / 32-bit indices",
            ));
        }
        for (_, count, _, offset, base) in &commands {
            if *count < 0
                || *base < 0
                || offset.saturating_add(*count as usize).saturating_mul(4) > indices.len()
            {
                return Err(PyRuntimeError::new_err("invalid draw-command range"));
            }
        }
        self.packets.entry(id).or_default().push(Packet {
            vertices,
            indices,
            commands,
        });
        self.uploads += 1;
        Ok(())
    }
    #[pyo3(signature=(id, background=(0.0, 0.0, 0.0, 0.0)))]
    fn render(&mut self, id: u64, background: (f32, f32, f32, f32)) -> PyResult<()> {
        let _guard = unsafe { Guard::new() };
        self.render_inner(id, background)
    }
    fn render_many(&mut self, views: Vec<(u64, (f32, f32, f32, f32))>) -> PyResult<()> {
        let _guard = unsafe { Guard::new() };
        for (id, background) in views {
            self.render_inner(id, background)?;
        }
        Ok(())
    }
    fn texture(&self, id: u64) -> Option<u32> {
        self.targets.get(&id).map(|t| t.tex)
    }
    fn image(&self, id: u64) -> Option<(u32, i32, i32)> {
        self.targets.get(&id).map(|t| (t.tex, t.w, t.h))
    }
    fn remove(&mut self, id: u64) {
        self.packets.remove(&id);
        if let Some(t) = self.targets.remove(&id) {
            unsafe {
                t.delete();
            }
        }
    }
    fn stats(&self) -> (u64, u64, usize, usize) {
        (
            self.renders,
            self.uploads,
            self.targets
                .values()
                .map(|t| (t.w * t.h * 4) as usize)
                .sum(),
            self.packets
                .values()
                .flatten()
                .map(|p| p.vertices.len() + p.indices.len())
                .sum(),
        )
    }
    fn scratch_stats(&self) -> (usize, usize, u64) {
        (self.scratch.len(), self.scratch_bytes(), self.scratch_allocations)
    }
    fn close(&mut self) {
        unsafe {
            for (_, t) in self.targets.drain() {
                t.delete();
            }
            self.packets.clear();
            for t in self.scratch.drain(..) {
                t.delete();
            }
            if self.program != 0 {
                glDeleteProgram(self.program);
                glDeleteBuffers(1, &self.vbo);
                glDeleteBuffers(1, &self.ebo);
                glDeleteVertexArrays(1, &self.vao);
                self.program = 0;
            }
        }
    }
}

impl TextureCache {
    fn scratch_bytes(&self) -> usize {
        self.scratch.iter().map(|t| (t.w * t.h * 4) as usize).sum()
    }
    // The caller owns a Guard for either this replay or a whole composition batch.
    fn render_inner(&mut self, id: u64, background: (f32, f32, f32, f32)) -> PyResult<()> {
        unsafe {
            let target = self
                .targets
                .get(&id)
                .ok_or_else(|| PyRuntimeError::new_err("missing target"))?;
            let (w, h, target_fbo) = (target.w, target.h, target.fbo);
            let index = if let Some(index) = self.scratch.iter().position(|s| s.w == w && s.h == h) {
                index
            } else {
                let used: usize = self.targets.values().map(|t| (t.w * t.h * 4) as usize).sum();
                let bytes = (w * h * 4) as usize;
                if used + bytes > self.budget {
                    return Err(PyRuntimeError::new_err("prototype texture budget exceeded"));
                }
                while self.scratch.len() >= 8 || used + self.scratch_bytes() + bytes > self.budget {
                    self.scratch.remove(0).delete();
                }
                self.scratch.push(Target::new(w, h)?);
                self.scratch_allocations += 1;
                self.scratch.len() - 1
            };
            // LRU pool of transient accumulation targets, independent of view caches.
            let scratch = self.scratch.remove(index);
            let (scratch_fbo, scratch_tex) = (scratch.fbo, scratch.tex);
            self.scratch.push(scratch);
            glBindFramebuffer(FB, scratch_fbo);
            glViewport(0, 0, w, h);
            glColorMask(1, 1, 1, 1);
            for p in [0x0B71, 0x0B44, 0x0B90, 0x8DB9, 0x0C11] {
                glDisable(p);
            }
            let (r, g, b, a) = background;
            glClearColor(r * a, g * a, b * a, a);
            glClear(0x4000);
            glEnable(0x0BE2);
            glBlendEquationSeparate(0x8006, 0x8006);
            glBlendFuncSeparate(0x0302, 0x0303, 1, 0x0303);
            glUseProgram(self.program);
            glUniform2f(self.size_loc, w as f32, h as f32);
            glUniform1i(self.resolve_loc, 0);
            glBindVertexArray(self.vao);
            glBindBuffer(ARRAY, self.vbo);
            glBindBuffer(ELEMENT, self.ebo);
            glBindSampler(0, 0);
            glEnable(0x0C11);
            for packet in self
                .packets
                .get(&id)
                .ok_or_else(|| PyRuntimeError::new_err("missing command packet"))?
            {
                glBufferData(
                    ARRAY,
                    packet.vertices.len() as isize,
                    packet.vertices.as_ptr().cast(),
                    0x88E0,
                );
                glBufferData(
                    ELEMENT,
                    packet.indices.len() as isize,
                    packet.indices.as_ptr().cast(),
                    0x88E0,
                );
                for (tex, count, (x1, y1, x2, y2), offset, base) in &packet.commands {
                    let x = x1.floor().max(0.) as i32;
                    let y = y1.floor().max(0.) as i32;
                    let right = x2.ceil().min(w as f32) as i32;
                    let bottom = y2.ceil().min(h as f32) as i32;
                    if right <= x || bottom <= y || *count == 0 {
                        continue;
                    }
                    glScissor(x, h - bottom, right - x, bottom - y);
                    glBindTexture(TEX, *tex);
                    glDrawElementsBaseVertex(
                        4,
                        *count,
                        0x1405,
                        (offset * 4) as *const c_void,
                        *base,
                    );
                }
            }
            // Resolve premultiplied accumulation to straight-alpha ImGui textures.
            // This prevents double-multiplication at nested cache boundaries.
            glBindFramebuffer(FB, target_fbo);
            glDisable(0x0BE2);
            glDisable(0x0C11);
            glUniform1i(self.resolve_loc, 1);
            glBindTexture(TEX, scratch_tex);
            let mut vertices = Vec::<u8>::new();
            for (x, y, u, v) in [
                (0., 0., 0., 1.),
                (w as f32, 0., 1., 1.),
                (w as f32, h as f32, 1., 0.),
                (0., h as f32, 0., 0.),
            ] {
                for f in [x, y, u, v] {
                    vertices.extend_from_slice(&f.to_ne_bytes());
                }
                vertices.extend_from_slice(&[255; 4]);
            }
            let indices: [u32; 6] = [0, 1, 2, 0, 2, 3];
            glBufferData(
                ARRAY,
                vertices.len() as isize,
                vertices.as_ptr().cast(),
                0x88E0,
            );
            glBufferData(ELEMENT, 24, indices.as_ptr().cast(), 0x88E0);
            glDrawElementsBaseVertex(4, 6, 0x1405, ptr::null(), 0);
            self.renders += 1;
            Ok(())
        }
    }
}
