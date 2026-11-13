//! Bounded asynchronous timestamps. All calls belong to the current GL context.
//! No result is read before availability, and no timer target is nested.
use super::*;
use std::time::{Duration, Instant};

#[link(name = "GL")]
extern "C" {
    fn glGetString(name: u32) -> *const u8;
    fn glGetStringi(name: u32, index: u32) -> *const u8;
    fn glGenQueries(n: i32, ids: *mut u32);
    fn glDeleteQueries(n: i32, ids: *const u32);
    fn glQueryCounter(id: u32, target: u32);
    fn glGetQueryObjectiv(id: u32, name: u32, value: *mut i32);
    fn glGetQueryObjectui64v(id: u32, name: u32, value: *mut u64);
}

// The optional UUID extension must not become a module-load dependency.
#[cfg_attr(target_os = "linux", link(name = "dl"))]
extern "C" { fn dlsym(handle: *mut c_void, symbol: *const i8) -> *mut c_void; }
unsafe fn device_uuid_function() -> Option<unsafe extern "C" fn(u32, u32, *mut u8)> {
    #[cfg(target_os = "macos")]
    let handle = (-2_isize) as *mut c_void; // RTLD_DEFAULT on Darwin.
    #[cfg(not(target_os = "macos"))]
    let handle = ptr::null_mut(); // RTLD_DEFAULT on the prototype's Linux backend.
    let address = dlsym(handle, b"glGetUnsignedBytei_vEXT\0".as_ptr().cast());
    if address.is_null() { None } else { Some(std::mem::transmute(address)) }
}

pub type Observation = (String, u64, u64, f64, Option<f64>);
struct Pending {
    ids: [u32; 2],
    operation: String,
    view: u64,
    units: u64,
    start: Instant,
    cpu_ns: f64,
    targets: Vec<Target>,
    buffer: u32,
}
impl Pending {
    unsafe fn delete(self) {
        glDeleteQueries(2, self.ids.as_ptr());
        for target in self.targets { target.delete(); }
        if self.buffer != 0 { glDeleteBuffers(1, &self.buffer); }
    }
}
#[derive(Default)]
pub struct Sampler {
    pending: Vec<Pending>,
    pub armed: bool,
    sequence: usize,
}
impl Sampler {
    pub unsafe fn environment() -> Vec<String> {
        let mut environment: Vec<String> = [0x1F00, 0x1F01, 0x1F02, 0x8B8C].iter().map(|name| {
            let value = glGetString(*name);
            if value.is_null() { String::new() } else {
                std::ffi::CStr::from_ptr(value.cast()).to_string_lossy().chars().take(512).collect()
            }
        }).collect();
        let uuid_supported = (0..integer(0x821D).clamp(0,1024)).any(|index| {
            let name = glGetStringi(0x1F03,index as u32);
            !name.is_null() && std::ffi::CStr::from_ptr(name.cast()).to_bytes() == b"GL_EXT_memory_object"
        });
        let mut device = String::from("unidentified-device");
        if uuid_supported && integer(0x9596) == 1 {
            if let Some(get_uuid) = device_uuid_function() {
                let mut uuid = [0_u8;16];
                get_uuid(0x9597,0,uuid.as_mut_ptr());
                if uuid.iter().any(|byte| *byte != 0) {
                    device = format!("uuid:{}", uuid.iter().map(|byte| format!("{byte:02x}")).collect::<String>());
                }
            }
        }
        environment.push(device);
        environment.push(format!("desktop-gl/rgba8/samples1/replay-v1/flags{}/profile{}", integer(0x821E), integer(0x9126)));
        environment
    }
    pub unsafe fn begin(&mut self, operation: &str, view: u64, units: u64) -> bool {
        if self.pending.len() >= 4 { return false; }
        if operation == "replay" { self.sequence = self.sequence.wrapping_add(1); }
        let mut sample = Pending { ids: [0; 2], operation: operation.into(), view, units,
            start: Instant::now(), cpu_ns: 0., targets: vec![], buffer: 0 };
        glGenQueries(2, sample.ids.as_mut_ptr());
        glQueryCounter(sample.ids[0], 0x8E28);
        sample.start = Instant::now();
        self.pending.push(sample);
        true
    }
    pub unsafe fn end(&mut self, valid: bool) {
        let sample = self.pending.last_mut().unwrap();
        sample.cpu_ns = sample.start.elapsed().as_nanos() as f64;
        glQueryCounter(sample.ids[1], 0x8E28);
        if !valid { self.pending.pop().unwrap().delete(); }
    }
    pub unsafe fn poll(&mut self) -> Vec<Observation> {
        let mut observations = vec![];
        let mut index = 0;
        while index < self.pending.len() {
            let sample = &self.pending[index];
            let mut available = 0;
            glGetQueryObjectiv(sample.ids[1], 0x8867, &mut available);
            if available != 0 {
                let sample = self.pending.remove(index);
                let (mut start, mut end) = (0, 0);
                glGetQueryObjectui64v(sample.ids[0], 0x8866, &mut start);
                glGetQueryObjectui64v(sample.ids[1], 0x8866, &mut end);
                if end >= start {
                    observations.push((sample.operation.clone(), sample.view, sample.units,
                                       sample.cpu_ns, if end > start { Some((end-start) as f64) } else { None }));
                }
                sample.delete();
            } else if sample.start.elapsed() > Duration::from_secs(5) {
                self.pending.remove(index).delete();
            } else { index += 1; }
        }
        observations
    }
    pub unsafe fn cancel_probes(&mut self) {
        // Rendering gets the memory first when a real target needs allocation.
        let mut index = 0;
        while index < self.pending.len() {
            if self.pending[index].operation == "replay" { index += 1; }
            else { self.pending.remove(index).delete(); }
        }
    }
    pub fn context_lost(&mut self) {
        // The destroyed context owns reclamation. Never delete its numeric names
        // in a different (possibly already current) replacement context.
        self.pending.clear();
        self.armed = false;
    }
    pub unsafe fn close(&mut self) {
        for sample in self.pending.drain(..) { sample.delete(); }
        self.armed = false;
    }
    pub fn pending(&self) -> usize { self.pending.len() }
    pub fn selected(&self, count: usize) -> usize { self.sequence % count }
}

impl TextureCache {
    pub(super) unsafe fn probe(&mut self, operation: &str, size: i32) -> PyResult<bool> {
        if self.sampler.pending.len() >= 4 || ![32,128,512].contains(&size) { return Ok(false); }
        if !["allocate", "transition", "upload", "clear", "resolve", "composite"].contains(&operation) {
            return Err(PyRuntimeError::new_err("unknown calibration operation"));
        }
        let bytes = size as usize * size as usize * 8;
        let used: usize = self.targets.values().map(|t| t.w as usize * t.h as usize * 4).sum();
        if used + self.scratch_bytes() + bytes + self.sampler.pending.iter().map(|p|
            p.targets.iter().map(|t| t.w as usize*t.h as usize*4).sum::<usize>()
            + if p.buffer != 0 {p.units as usize} else {0}).sum::<usize>() > self.budget {
            return Ok(false);
        }
        let _guard = Guard::new();
        for capability in [0x0B71,0x0B44,0x0B90,0x8DB9] { glDisable(capability); }
        let units = size as u64 * size as u64;
        let mut targets = vec![];
        let mut buffer = 0;
        // Preparation is deliberately outside the measured interval. No resource
        // is bigger than 512 squared RGBA8; resources stay owned until completion.
        if operation != "allocate" && operation != "upload" {
            targets.push(Target::new(size, size, 0x8058)?);
            glDisable(0x0C11); glColorMask(1,1,1,1);
            glClearColor(0.25,0.5,0.75,1.); glClear(0x4000);
        }
        if operation == "resolve" || operation == "composite" {
            let second = match Target::new(size, size, 0x8058) {
                Ok(target) => target,
                Err(error) => { for target in targets {target.delete();} return Err(error); }
            };
            targets.push(second);
            glViewport(0,0,size,size);
            glUseProgram(self.program);
            glUniform2f(self.size_loc,size as f32,size as f32);
            glUniform1i(self.resolve_loc,if operation=="resolve" {1} else {0});
            glBindVertexArray(self.vao);
            glBindBuffer(ARRAY,self.vbo);
            let mut vertices=[0_u8;80];
            for (i,(x,y,u,v)) in [(0.,0.,0.,1.),(size as f32,0.,1.,1.),
                (size as f32,size as f32,1.,0.),(0.,size as f32,0.,0.)].into_iter().enumerate() {
                for (j,f) in [x,y,u,v].into_iter().enumerate() {vertices[i*20+j*4..i*20+j*4+4].copy_from_slice(&f.to_ne_bytes());}
                vertices[i*20+16..i*20+20].fill(255);
            }
            glBufferData(ARRAY,80,vertices.as_ptr().cast(),0x88E0);
            // Replay always replaces these streaming contents before use.
            glBindBuffer(ELEMENT,self.ebo);
            let indices:[u32;6]=[0,1,2,0,2,3];
            glBufferData(ELEMENT,24,indices.as_ptr().cast(),0x88E0);
            glActiveTexture(0x84C0); glBindSampler(0,0);
            glBindTexture(TEX,targets[0].tex);
            if operation=="composite" {
                glEnable(0x0BE2); glBlendEquationSeparate(0x8006,0x8006);
                glBlendFuncSeparate(0x88F9,0x88FA,1,0x0303);
            } else {glDisable(0x0BE2);}
        }
        let data = if operation == "upload" {vec![0_u8; units as usize*4]} else {vec![]};
        if operation == "upload" {glGenBuffers(1,&mut buffer); glBindBuffer(ARRAY,buffer);}
        if operation == "transition" { glBindFramebuffer(FB,0); }
        self.sampler.begin(operation,0,if operation=="upload" {units*4} else {units});
        let result = match operation {
            "allocate" => match Target::new(size, size, 0x8058) {Ok(target)=>{targets.push(target);Ok(())},Err(error)=>Err(error)},
            "transition" => {glBindFramebuffer(FB,targets[0].fbo); Ok(())},
            "upload" => {glBufferData(ARRAY,data.len() as isize,data.as_ptr().cast(),0x88E0); Ok(())},
            "clear" => {glClear(0x4000); Ok(())},
            _ => {glDrawElementsBaseVertex(4,6,0x1405,ptr::null(),0); Ok(())},
        };
        let sample=self.sampler.pending.last_mut().unwrap();
        sample.targets=targets; sample.buffer=buffer;
        self.sampler.end(result.is_ok());
        result.map(|_|true)
    }
}
