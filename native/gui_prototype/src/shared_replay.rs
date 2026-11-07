//! Ordered shared-pass expansion of retained packets. Logical child references
//! remain stable whether they expand to commands or sample a raster texture.
use super::*;

type Clip = (f32,f32,f32,f32);
fn intersect(a: Clip,b: Clip)->Clip {(a.0.max(b.0),a.1.max(b.1),a.2.min(b.2),a.3.min(b.3))}
fn shifted(c:Clip,o:(f32,f32))->Clip {(c.0+o.0,c.1+o.1,c.2+o.0,c.3+o.1)}
fn visible(c:Clip)->bool {c.2>c.0 && c.3>c.1}

impl Packet {
    fn empty()->Self {Self {vertices:vec![],indices:vec![],commands:vec![],spans:vec![]}}
    fn quad(&mut self,texture:u32,origin:(f32,f32),size:(f32,f32),clip:Clip,color:(f32,f32,f32,f32)) {
        if !visible(clip) {return;}
        let base=(self.vertices.len()/20) as i32;
        let offset=self.indices.len()/4;
        let rgba=[color.0,color.1,color.2,color.3].map(|v|(v*255.).round().clamp(0.,255.) as u8);
        for (x,y,u,v) in [(origin.0,origin.1,0_f32,1_f32),
            (origin.0+size.0,origin.1,1.,1.),
            (origin.0+size.0,origin.1+size.1,1.,0.),
            (origin.0,origin.1+size.1,0.,0.)] {
            for value in [x,y,u,v] {self.vertices.extend_from_slice(&value.to_ne_bytes());}
            self.vertices.extend_from_slice(&rgba);
        }
        for index in [0_u32,1,2,0,2,3] {self.indices.extend_from_slice(&index.to_ne_bytes());}
        self.commands.push((texture,6,clip,offset,base));
    }
}
impl TextureCache {
    pub(super) fn flatten(&self,id:u64,origin:(f32,f32),clip:Clip,background:bool)->PyResult<Packet> {
        let mut output=Packet::empty();
        self.expand(id,origin,clip,background,&mut output)?;
        Ok(output)
    }
    fn expand(&self,id:u64,origin:(f32,f32),clip:Clip,background:bool,output:&mut Packet)->PyResult<()> {
        let target=self.targets.get(&id).ok_or_else(||PyRuntimeError::new_err("missing retained reference"))?;
        let size=(target.w as f32,target.h as f32);
        let clip=intersect(clip,(origin.0,origin.1,origin.0+size.0,origin.1+size.1));
        if !visible(clip) {return Ok(());}
        let start=output.commands.len();
        if background {
            if target.fbo!=0 {
                output.quad(target.tex,origin,size,clip,(1.,1.,1.,1.));
                output.spans.push((id,start,output.commands.len()));
                return Ok(());
            }
            let color=self.backgrounds.get(&id).copied().unwrap_or((0.,0.,0.,0.));
            if color.3>0. {output.quad(0,origin,size,clip,color);}
        }
        for packet in self.packets.get(&id).ok_or_else(||PyRuntimeError::new_err("missing command packet"))? {
            let vertex_base=(output.vertices.len()/20) as i32;
            let index_base=output.indices.len()/4;
            for vertex in packet.vertices.chunks_exact(20) {
                let x=f32::from_ne_bytes(vertex[0..4].try_into().unwrap())+origin.0;
                let y=f32::from_ne_bytes(vertex[4..8].try_into().unwrap())+origin.1;
                output.vertices.extend_from_slice(&x.to_ne_bytes());
                output.vertices.extend_from_slice(&y.to_ne_bytes());
                output.vertices.extend_from_slice(&vertex[8..20]);
            }
            output.indices.extend_from_slice(&packet.indices);
            for (texture,count,local_clip,offset,base) in &packet.commands {
                let clip=intersect(clip,shifted(*local_clip,origin));
                if !visible(clip) || *count==0 {continue;}
                if let Some(child)=self.references.get(texture) {
                    if *count!=6 {return Err(PyRuntimeError::new_err("retained child requires one framework image quad"));}
                    let index=u32::from_ne_bytes(packet.indices[offset*4..offset*4+4].try_into().unwrap()) as usize+*base as usize;
                    let vertex=packet.vertices.get(index*20..index*20+20).ok_or_else(||PyRuntimeError::new_err("invalid retained child vertex"))?;
                    let position=(f32::from_ne_bytes(vertex[0..4].try_into().unwrap())+origin.0,
                                  f32::from_ne_bytes(vertex[4..8].try_into().unwrap())+origin.1);
                    self.expand(*child,position,clip,true,output)?;
                } else {
                    output.commands.push((*texture,*count,clip,index_base+offset,vertex_base+base));
                }
            }
        }
        if let Some(children)=self.children.get(&id) {
            for (child,x,y,w,h) in children {
                let position=(origin.0+(x+0.5).floor(),origin.1+(y+0.5).floor());
                let bounds=(position.0,position.1,origin.0+(x+w+0.5).floor(),origin.1+(y+h+0.5).floor());
                self.expand(*child,position,intersect(clip,bounds),true,output)?;
            }
        }
        if output.commands.len()>start {output.spans.push((id,start,output.commands.len()));}
        Ok(())
    }
}
