//! One authoritative retained bookkeeping table. Python values stay Python values.
use std::{cell::RefCell, collections::HashMap, rc::Rc};
use pyo3::{prelude::*, class::gc::{PyVisit,PyTraverseError}, exceptions::PyKeyError, types::{PyDict,PyTuple,PyString}};
use crate::retained::same_argument;

pub struct Snapshot {pub input:Py<PyAny>,pub args:Vec<(Py<PyString>,Py<PyAny>)>}
impl Snapshot {
    pub fn new(input:Py<PyAny>,args:&Bound<'_,PyDict>)->PyResult<Self>{
        Ok(Self{input,args:args.iter().map(|(k,v)|Ok((k.downcast_into::<PyString>()?.unbind(),v.unbind()))).collect::<PyResult<_>>()?})
    }
    pub fn compare(&self,py:Python<'_>,input:&Bound<'_,PyAny>,args:&Bound<'_,PyDict>)->PyResult<(bool,bool)>{
        if self.args.len()!=args.len(){return Ok((true,false))}
        let mut changed=false;let mut reusable=self.input.bind(py).is(input);
        for ((key,old),(new_key,value)) in self.args.iter().zip(args.iter()) {
            // Ordered shape matches in the warm case; fall back to lookup on reordering.
            let ordered=key.bind(py).is(&new_key) || key.bind(py).as_any().eq(&new_key)?;
            if !ordered {reusable=false;}
            let value=if ordered {value}else{let Some(v)=args.get_item(key.bind(py))? else{return Ok((true,false))};v};
            let same=same_argument(old.bind(py),&value)?;
            if !old.bind(py).is(&value) && !matches!(key.bind(py).to_str()?,"width"|"height"|"min_height"|"max_height") {reusable=false;}
            if !same {
                reusable=false;
                if !matches!(key.bind(py).to_str()?,"left"|"top"|"width"|"height"|"scroll_offset"|"content_offset") {changed=true;}
            }
        }
        Ok((changed,reusable))
    }
    fn dict<'py>(&self,py:Python<'py>)->PyResult<Bound<'py,PyDict>>{let d=PyDict::new(py);for(k,v)in &self.args{d.set_item(k,v)?;}Ok(d)}
}
struct Record {execution:Py<PyTuple>,snapshot:Rc<Snapshot>}
#[pyclass(unsendable)]
pub struct RetainedRecords {rows:RefCell<HashMap<u64,Record>>}
impl RetainedRecords {
    pub fn snapshot(&self,id:u64)->Option<Rc<Snapshot>>{self.rows.borrow().get(&id).map(|r|r.snapshot.clone())}
    pub fn put(&self,py:Python<'_>,id:u64,snapshot:Rc<Snapshot>,runtime:&Bound<'_,PyAny>,renderer:&Bound<'_,PyAny>,function:&Bound<'_,PyAny>,state:&Bound<'_,PyAny>,unique:&Bound<'_,PyAny>,caller:&Bound<'_,PyDict>)->PyResult<bool>{
        let old=self.rows.borrow().get(&id).map(|r|r.execution.clone_ref(py));
        let execution=if let Some(old)=&old {
            let keys=old.bind(py).get_item(5)?.downcast_into::<PyTuple>()?;
            let mut same=keys.len()==caller.len();
            if same {for (a,(b,_)) in keys.iter().zip(caller.iter()){if !a.is(&b) && !a.eq(&b)? {same=false;break;}}}
            if same {old.clone_ref(py)} else {
                let mut values=old.bind(py).iter().map(|v|v.unbind()).collect::<Vec<_>>();
                values[5]=PyTuple::new(py,caller.iter().map(|(k,_)|k))?.into_any().unbind();PyTuple::new(py,values)?.unbind()
            }
        }else{PyTuple::new(py,[runtime.clone(),renderer.clone(),function.clone(),state.clone(),unique.clone(),PyTuple::new(py,caller.iter().map(|(k,_)|k))?.into_any()])?.unbind()};
        let retired=self.rows.borrow_mut().insert(id,Record{execution,snapshot});
        drop(retired);Ok(old.is_none())
    }
}
#[pymethods]
impl RetainedRecords {
    #[new] fn new()->Self{Self{rows:RefCell::new(HashMap::new())}}
    fn __len__(&self)->usize{self.rows.borrow().len()}
    fn __contains__(&self,id:Option<u64>)->bool{id.is_some_and(|id|self.rows.borrow().contains_key(&id))}
    fn ids(&self)->Vec<u64>{let mut ids=self.rows.borrow().keys().copied().collect::<Vec<_>>();ids.sort_unstable();ids}
    fn execution(&self,py:Python<'_>,id:u64)->PyResult<Py<PyTuple>>{self.rows.borrow().get(&id).map(|r|r.execution.clone_ref(py)).ok_or_else(||PyKeyError::new_err(id))}
    fn state(&self,py:Python<'_>,id:u64)->PyResult<Py<PyAny>>{Ok(self.execution(py,id)?.bind(py).get_item(3)?.unbind())}
    fn set_state(&self,py:Python<'_>,id:u64,state:Py<PyAny>)->PyResult<bool>{
        let old=self.execution(py,id)?;if old.bind(py).get_item(3)?.is(state.bind(py)){return Ok(false)}
        let mut values=old.bind(py).iter().map(|v|v.unbind()).collect::<Vec<_>>();values[3]=state;
        let tuple=PyTuple::new(py,values)?.unbind();let retired={let mut rows=self.rows.borrow_mut();std::mem::replace(&mut rows.get_mut(&id).ok_or_else(||PyKeyError::new_err(id))?.execution,tuple)};drop(retired);Ok(true)
    }
    fn invocation(&self,py:Python<'_>,id:u64)->PyResult<(Py<PyAny>,Py<PyDict>)>{let s=self.snapshot(id).ok_or_else(||PyKeyError::new_err(id))?;Ok((s.input.clone_ref(py),s.dict(py)?.unbind()))}
    fn argument(&self,py:Python<'_>,id:u64,key:&str,default:Py<PyAny>)->PyResult<Py<PyAny>>{let s=self.snapshot(id).ok_or_else(||PyKeyError::new_err(id))?;for(k,v)in &s.args{if k.bind(py).to_str()?==key{return Ok(v.clone_ref(py))}}Ok(default)}
    fn resize_snapshot(&self,py:Python<'_>,id:u64,width:Py<PyAny>,height:Py<PyAny>)->PyResult<()>{
        let s=self.snapshot(id).ok_or_else(||PyKeyError::new_err(id))?;let args=s.dict(py)?;args.set_item("width",width)?;args.set_item("height",height)?;
        let next=Rc::new(Snapshot::new(s.input.clone_ref(py),&args)?);let retired={let mut rows=self.rows.borrow_mut();std::mem::replace(&mut rows.get_mut(&id).ok_or_else(||PyKeyError::new_err(id))?.snapshot,next)};drop(retired);Ok(())
    }
    fn remove(&self,id:u64){let retired=self.rows.borrow_mut().remove(&id);drop(retired);}
    fn clear(&self){let retired=std::mem::take(&mut *self.rows.borrow_mut());drop(retired);}
    fn __traverse__(&self,visit:PyVisit<'_>)->Result<(),PyTraverseError>{for r in self.rows.borrow().values(){visit.call(&r.execution)?;visit.call(&r.snapshot.input)?;for(k,v)in &r.snapshot.args{visit.call(k)?;visit.call(v)?;}}Ok(())}
    fn __clear__(&self){self.clear();}
}
