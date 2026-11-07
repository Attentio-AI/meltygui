//! Isolated cohesive retained declaration prototype. Python execution stays a hook.
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError, PyNotImplementedError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use crate::{NativeState, identity::Identities, retained::{RetainedGraph, same_argument}};

fn required<'py>(dict: &Bound<'py,PyDict>, key:&Bound<'py,PyString>) -> PyResult<Bound<'py,PyAny>> {
    dict.get_item(key)?.ok_or_else(||PyRuntimeError::new_err("missing declaration plan field"))
}
fn pop<'py>(dict:&Bound<'py,PyDict>, key:&Bound<'py,PyString>, default:Bound<'py,PyAny>) -> PyResult<Bound<'py,PyAny>> {
    if let Some(value)=dict.get_item(key)? { dict.del_item(key)?;Ok(value) } else {Ok(default)}
}
fn number(value:&Bound<'_,PyAny>) -> PyResult<f64> { value.extract::<f64>() }
fn extent(width:f64,height:f64) -> PyResult<()> {
    if !width.is_finite() || width<=0. || width>8192. || !height.is_finite() || height<0. {
        return Err(PyValueError::new_err("cached boundaries require positive width and nonnegative height"));
    }
    Ok(())
}

#[pyclass(unsendable)]
pub struct RetainedDeclaration {
    cache:Option<Py<PyAny>>,
    function:Option<Py<PyAny>>,
    cold_bind:Option<Py<PyAny>>,
    alias:Option<Py<PyAny>>,
    path_type:Option<Py<PyAny>>,
    options:Py<PyDict>,
    renderer_id:u128,
}

#[pymethods]
impl RetainedDeclaration {
    #[new]
    fn new(_py:Python<'_>,cache:Py<PyAny>,function:Py<PyAny>,cold_bind:Py<PyAny>,
           alias:Py<PyAny>,path_type:Py<PyAny>,renderer_id:u128,options:&Bound<'_,PyDict>) -> PyResult<Self> {
        Ok(Self {cache:Some(cache),function:Some(function),cold_bind:Some(cold_bind),
                 alias:Some(alias),path_type:Some(path_type),options:options.copy()?.unbind(),renderer_id})
    }
    fn attach(&self,py:Python<'_>,wrapper:Py<PyAny>) -> PyResult<()> {
        self.options.bind(py).set_item("wrapper",wrapper)
    }
    fn positions(&self,py:Python<'_>,left:Py<PyAny>,top:Py<PyAny>) -> PyResult<()> {
        // No mutable declaration borrow: refresh may happen during a recursive call.
        self.options.bind(py).set_item("left",left)?;
        self.options.bind(py).set_item("top",top)
    }
    fn __traverse__(&self,visit:PyVisit<'_>) -> Result<(),PyTraverseError> {
        visit.call(&self.options)?;
        for value in [&self.cache,&self.function,&self.cold_bind,&self.alias,&self.path_type] {
            if let Some(value)=value {visit.call(value)?;}
        }
        Ok(())
    }
    fn __clear__(&mut self,py:Python<'_>) {
        self.cache=None;self.function=None;self.cold_bind=None;self.alias=None;self.path_type=None;
        self.options.bind(py).clear();
    }
    // Internal entry consumes the fresh kwargs dict owned by the public Python wrapper.
    fn __call__(&self,py:Python<'_>,input_value:Py<PyAny>,kwargs:&Bound<'_,PyDict>) -> PyResult<Py<PyAny>> {
        let cache=self.cache.as_ref().ok_or_else(||PyRuntimeError::new_err("retired declaration"))?.bind(py);
        let options=self.options.bind(py);
        let decoration=required(options,pyo3::intern!(py,"decoration"))?.downcast_into::<PyDict>()?;
        // Snapshot plan references before any callback can refresh this plan.
        let left_default=required(options,pyo3::intern!(py,"left"))?;let top_default=required(options,pyo3::intern!(py,"top"))?;
        let width_default=required(options,pyo3::intern!(py,"width"))?;let height_default=required(options,pyo3::intern!(py,"height"))?;
        let min_default=required(options,pyo3::intern!(py,"min_height"))?;let max_default=required(options,pyo3::intern!(py,"max_height"))?;
        let scroll_default=required(options,pyo3::intern!(py,"scroll"))?;
        if kwargs.contains(pyo3::intern!(py,"window_pos"))? {
            return Err(PyTypeError::new_err("@gui uses relative left and top, not window_pos"));
        }
        let initial_check=kwargs.get_item(pyo3::intern!(py,"initial"))?;
        let invalid_initial=if let Some(value)=initial_check {value.is_truthy()? && value.contains("window_pos")?}else{false};
        if invalid_initial {
            return Err(PyTypeError::new_err("@gui uses relative left and top, not window_pos"));
        }
        if cache.getattr(pyo3::intern!(py,"closed"))?.is_truthy()? {
            return Err(PyRuntimeError::new_err("retained runtime is closed"));
        }
        let identities:Py<Identities>=cache.getattr(pyo3::intern!(py,"identities"))?.extract()?;
        let graph:Py<RetainedGraph>=cache.getattr(pyo3::intern!(py,"graph"))?.extract()?;
        if cache.getattr(pyo3::intern!(py,"frame_roots"))?.is_none() && !identities.borrow(py).active() {
            identities.borrow_mut(py).begin_frame();
        }
        let keyed=kwargs.contains(pyo3::intern!(py,"key"))? || decoration.contains(pyo3::intern!(py,"key"))?;
        let key=pop(kwargs,pyo3::intern!(py,"key"),decoration.get_item("key")?.unwrap_or_else(||py.None().into_bound(py)))?;
        let name=kwargs.get_item(pyo3::intern!(py,"name"))?.or(decoration.get_item("name")?);
        let handle=identities.borrow_mut(py).handle(self.renderer_id,Some(&key),name.as_ref(),keyed)?;
        let portal_value=pop(kwargs,pyo3::intern!(py,"melty_window"),false.into_pyobject(py)?.to_owned().into_any())?;
        let portal=portal_value.is_truthy()?;
        let scroll_value=pop(kwargs,pyo3::intern!(py,"scroll"),scroll_default)?;
        let bindings=cache.getattr(pyo3::intern!(py,"bindings"))?.downcast_into::<PyDict>()?;
        if !bindings.contains(handle)? {
            // No graph/state/identity borrow survives this arbitrary cold Python hook.
            let binding=self.cold_bind.as_ref().unwrap().bind(py).call1((handle,))?;
            bindings.set_item(handle,binding)?;
        }
        let binding=bindings.get_item(handle)?.unwrap().downcast_into::<PyTuple>()?;
        let runtime=binding.get_item(0)?;
        let renderer=binding.get_item(1)?;
        let state_object=binding.get_item(3)?;
        let state:Py<NativeState>=state_object.extract()?;
        let unique=binding.get_item(4)?;

        // Preserve caller key order and values, without the temporary unfiltered copy.
        let caller=PyDict::new(py);
        for (key,value) in kwargs.iter() {
            let name=key.downcast::<PyString>()?.to_str()?;
            if value.is_none() && matches!(name,"left"|"top"|"width"|"height") {continue;}
            caller.set_item(key,value)?;
        }
        if pop(kwargs,pyo3::intern!(py,"glfw_window"),false.into_pyobject(py)?.to_owned().into_any())?.is_truthy()? {
            return Err(PyNotImplementedError::new_err("This experiment implements internal windows; native child surfaces are not connected yet"));
        }
        let initial=pop(kwargs,pyo3::intern!(py,"initial"),decoration.get_item("initial")?.unwrap_or_else(||PyDict::new(py).into_any()))?;
        let extras=pop(kwargs,pyo3::intern!(py,"return_extras"),false.into_pyobject(py)?.to_owned().into_any())?.is_truthy()?;
        let mut width=pop(kwargs,pyo3::intern!(py,"width"),width_default)?;
        let mut height=pop(kwargs,pyo3::intern!(py,"height"),height_default)?;
        let minimum_value=pop(kwargs,pyo3::intern!(py,"min_height"),min_default)?;
        let maximum_value=pop(kwargs,pyo3::intern!(py,"max_height"),max_default)?;
        let invalid_bounds=||PyValueError::new_err("height bounds must satisfy 0 <= min_height <= max_height <= 8192");
        let minimum=number(&minimum_value)?;if !minimum.is_finite(){return Err(invalid_bounds());}
        let maximum=number(&maximum_value)?;if !maximum.is_finite(){return Err(invalid_bounds());}
        let primitive=|v:&Bound<'_,PyAny>|v.is_exact_instance_of::<PyInt>() || v.is_exact_instance_of::<PyFloat>() || v.is_exact_instance_of::<PyBool>();
        let (minimum,maximum)=if primitive(&minimum_value) && primitive(&maximum_value) {
            if minimum<0. || maximum<minimum || maximum>8192. {return Err(invalid_bounds());}
            (minimum.ceil() as i64,maximum.ceil() as i64)
        } else {
            if !minimum_value.ge(0)? || !minimum_value.le(&maximum_value)? || !maximum_value.le(8192)? {return Err(invalid_bounds());}
            let math=PyModule::import(py,"math")?;
            (math.call_method1("ceil",(&minimum_value,))?.extract::<i64>()?,
             math.call_method1("ceil",(&maximum_value,))?.extract::<i64>()?)
        };
        let stack=cache.getattr(pyo3::intern!(py,"stack"))?.downcast_into::<PyList>()?;
        let parent=if stack.is_empty(){0}else{stack.get_item(stack.len()-1)?.extract::<u64>()?};
        let geometry=cache.getattr(pyo3::intern!(py,"geometry"))?;
        let allocation=if portal {py.None().into_bound(py)}else{geometry.call_method1(pyo3::intern!(py,"allocation"),(parent,))?};
        let graphics=cache.getattr(pyo3::intern!(py,"graphics"))?.is_truthy()?;
        let imgui=if graphics {Some(PyModule::import(py,"meltygui_imgui")?)}else{None};
        let mut position=(0.,0.);
        if graphics && (parent!=0 || cache.getattr(pyo3::intern!(py,"auto_layout"))?.is_truthy()?) && !portal {
            position=imgui.as_ref().unwrap().call_method0(pyo3::intern!(py,"get_cursor_screen_pos"))?.extract::<(f64,f64)>()?;
        }
        if !allocation.is_none() {
            let (x,y,w,h)=allocation.extract::<(f64,f64,f64,f64)>()?;
            let (px,pyy)=cache.call_method1(pyo3::intern!(py,"_position"),(parent,))?.extract::<(f64,f64)>()?;
            position=(x-px,y-pyy);width=w.into_pyobject(py)?.into_any();height=h.into_pyobject(py)?.into_any();
        }
        if width.is_none() {
            let w=if parent!=0 {graph.borrow_mut(py).constraints(py,parent).0-position.0}
                else if let Some(imgui)=&imgui {imgui.call_method0(pyo3::intern!(py,"get_content_region_available"))?.extract::<(f64,f64)>()?.0}
                else{return Err(PyValueError::new_err("width=None requires a parent or graphics host"));};
            width=w.into_pyobject(py)?.into_any();
        }
        let auto_height=height.is_none();
        if auto_height {height=state.borrow(py).number("height",0.).into_pyobject(py)?.into_any();}
        let w=number(&width)?;let h=number(&height)?;extent(w,h)?;
        if !allocation.is_none() {
            caller.set_item(pyo3::intern!(py,"width"),w)?;caller.set_item(pyo3::intern!(py,"height"),h)?;
            caller.set_item(pyo3::intern!(py,"left"),position.0)?;caller.set_item(pyo3::intern!(py,"top"),position.1)?;
        }
        let defaults=PyDict::new(py);
        defaults.set_item(pyo3::intern!(py,"width"),width)?;defaults.set_item(pyo3::intern!(py,"height"),height)?;
        let left=left_default;let top=top_default;
        defaults.set_item(pyo3::intern!(py,"left"),if left.is_none(){position.0.into_pyobject(py)?.into_any()}else{left})?;
        defaults.set_item(pyo3::intern!(py,"top"),if top.is_none(){position.1.into_pyobject(py)?.into_any()}else{top})?;
        defaults.set_item(pyo3::intern!(py,"scroll_offset"),(0.,0.))?;
        defaults.set_item(pyo3::intern!(py,"content_offset"),(0.,if portal{28.}else{0.}))?;
        // Keep the existing state conversion boundary; no graph borrow surrounds it.
        state_object.call_method1(pyo3::intern!(py,"configure"),(&defaults,&caller,&initial))?;
        let (x,y,w,h)={let s=state.borrow(py);(s.number("left",0.),s.number("top",0.),s.number("width",0.),s.number("height",0.))};
        extent(w,h)?;let h=h.max(minimum as f64).min(maximum as f64);
        let arguments=decoration.copy()?;arguments.update(kwargs.as_mapping())?;
        arguments.set_item(pyo3::intern!(py,"width"),w)?;
        arguments.set_item(pyo3::intern!(py,"height"),if auto_height{py.None().into_bound(py)}else{h.into_pyobject(py)?.into_any()})?;
        arguments.set_item(pyo3::intern!(py,"min_height"),minimum)?;arguments.set_item(pyo3::intern!(py,"max_height"),maximum)?;
        // Match the existing native flag contract, before taking its mutable borrow.
        let native_portal=portal_value.extract::<bool>()?;let scroll=scroll_value.extract::<bool>()?;
        let node=graph.borrow_mut(py).declare(py,parent,0,handle.into_pyobject(py)?.into_any().unbind(),input_value.clone_ref(py),
            arguments.clone().unbind(),(x,y,w,if auto_height{-1.}else{h}),native_portal,Some(state.clone_ref(py)),scroll)?;
        let invocations=cache.getattr(pyo3::intern!(py,"invocations"))?.downcast_into::<PyDict>()?;
        let previous=invocations.get_item(node)?;
        let pending=graph.borrow(py).pending(node);
        let mut input_same=false;
        if let Some(previous)=&previous {
            let previous=previous.downcast::<PyTuple>()?;let old_input=previous.get_item(0)?;
            let old_args=previous.get_item(1)?.downcast_into::<PyDict>()?;
            input_same=old_input.is(input_value.bind(py));
            let changed_input=!pending && !same_argument(&old_input,input_value.bind(py))?;
            let mut changed=if changed_input {
                let result=graph.borrow_mut(py).result(py,node,false)?.1;
                !same_argument(result.bind(py),input_value.bind(py))?
            }else{false};
            if !changed {
                changed=old_args.len()!=arguments.len();
                for (key,value) in arguments.iter() {
                    let old=old_args.get_item(&key)?;
                    if old.is_none() {changed=true;break;}
                    let key=key.downcast::<PyString>()?.to_str()?;
                    if !matches!(key,"left"|"top"|"width"|"height"|"scroll_offset"|"content_offset")
                        && !same_argument(&old.unwrap(),&value)? {changed=true;break;}
                }
            }
            if changed {graph.borrow_mut(py).invalidate_node(node);}
            if !pending && !input_same {
                let alias=self.alias.as_ref().unwrap().bind(py).call1((&old_input,))?.extract::<String>()?;
                graph.borrow_mut(py).dissociate(node,&alias);
            }
        }
        invocations.set_item(node,(input_value.clone_ref(py),arguments.copy()?))?;
        cache.getattr(pyo3::intern!(py,"caller_keys"))?.downcast::<PyDict>()?.set_item(node,PyTuple::new(py,caller.iter().map(|(key,_)|key))?)?;
        let records=cache.getattr(pyo3::intern!(py,"records"))?.downcast_into::<PyDict>()?;
        if !records.contains(node)? {
            let function=self.function.as_ref().unwrap().bind(py);
            cache.getattr(pyo3::intern!(py,"uniques"))?.downcast::<PyDict>()?.set_item(node,&unique)?;
            records.set_item(node,(&runtime,&renderer,function))?;
            cache.getattr(pyo3::intern!(py,"states"))?.downcast::<PyDict>()?.set_item(node,&state_object)?;
            for owner in [function.clone(),required(options,pyo3::intern!(py,"wrapper"))?] {
                let alias=self.alias.as_ref().unwrap().bind(py).call1((&owner,))?.extract::<String>()?;
                graph.borrow_mut(py).associate(node,alias,owner.unbind());
            }
            graph.borrow_mut(py).associate(node,format!("renderer:{}",self.renderer_id),function.clone().unbind());
        }
        let path=input_value.bind(py).is_instance_of::<PyString>() || input_value.bind(py).is_instance(self.path_type.as_ref().unwrap().bind(py))?;
        if previous.is_none() || !input_same || path {
            let alias=self.alias.as_ref().unwrap().bind(py).call1((input_value.bind(py),))?.extract::<String>()?;
            graph.borrow_mut(py).associate(node,alias,input_value.clone_ref(py));
        }
        if portal {
            let flags=PyDict::new(py);flags.set_item("portal",true)?;
            geometry.call_method(pyo3::intern!(py,"bind"),(node,parent),Some(&flags))?;
        } else {geometry.call_method1(pyo3::intern!(py,"bind"),(node,parent))?;}
        if parent==0 {
            cache.getattr(pyo3::intern!(py,"root_ids"))?.call_method1("add",(node,))?;
            let roots=cache.getattr(pyo3::intern!(py,"frame_roots"))?;
            if !roots.is_none(){roots.call_method1("add",(node,))?;}
        }
        let dirty=graph.borrow(py).dirty(node);
        let frozen=geometry.getattr(pyo3::intern!(py,"frozen"))?.contains(node)?;
        if dirty && !pending && !frozen {
            let execute=parent==0 || !graph.borrow(py).is_scrollable(parent)
                || (auto_height && !cache.getattr(pyo3::intern!(py,"backgrounds"))?.contains(node)?)
                || graph.borrow_mut(py).visible(py,node);
            if execute {
                let flags=PyDict::new(py);flags.set_item("independent",false)?;
                cache.call_method(pyo3::intern!(py,"_execute"),(node,),Some(&flags))?;
            }
        }
        if parent==0 {cache.call_method0(pyo3::intern!(py,"flush"))?;}
        let (_,_,w,h)=graph.borrow_mut(py).rect(py,node);
        if let Some(imgui)=imgui {
            if parent!=0 && !portal {
                imgui.call_method1(pyo3::intern!(py,"set_cursor_screen_pos"),((x,y),))?;
                if !graph.borrow(py).is_scrollable(parent) {
                    let draw_list=imgui.call_method0(pyo3::intern!(py,"get_window_draw_list"))?;
                    cache.call_method1(pyo3::intern!(py,"_draw_image"),(draw_list,node,x,y,w,h))?;
                }
                imgui.call_method1(pyo3::intern!(py,"dummy"),(w,h))?;
            } else if parent==0 && !portal && cache.getattr(pyo3::intern!(py,"auto_layout"))?.is_truthy()? {
                imgui.call_method1(pyo3::intern!(py,"set_cursor_screen_pos"),((x,y),))?;
                imgui.call_method1(pyo3::intern!(py,"dummy"),(w,h))?;
            }
        }
        let (changed,value)=graph.borrow_mut(py).result(py,node,true)?;
        if changed && parent!=0 {graph.borrow_mut(py).invalidate_node(parent);}
        if extras {
            let state=cache.getattr(pyo3::intern!(py,"states"))?.get_item(node)?;
            Ok((changed,value,state).into_pyobject(py)?.into_any().unbind())
        } else {Ok((changed,value).into_pyobject(py)?.into_any().unbind())}
    }
}
