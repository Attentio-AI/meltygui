"""Native observation/application bridge for the experimental Rust geometry.

The existing platform adapter owns asynchronous acknowledgements and capability
changes. Only this bridge writes the prototype's native constraint solution.
"""
class NativeCollision:
    def __init__(self, geometry):
        self.geometry=geometry
        self.pairs=[]; self.screen=[]
        self.mode=None; self.window_id=None
        self.os_frame=None
        self.last=None
        self.drag_total=[0.,0.]
        self.drag_index=[None,None]
        self.drag_token=[0,0]
        self.token=0
        self.contact=False

    def begin(self, adapter):
        self.os_frame=adapter
        self.observe(adapter.prototype_snapshot())

    def observe(self, snapshot):
        geometry=self.geometry
        pairs,screen,mode,window_id,pending=snapshot
        changed_mode=(self.mode,self.window_id)!=(mode,window_id)
        if changed_mode:
            self.cancel()
        self.mode,self.window_id=mode,window_id
        geometry.offset=tuple(pair[0] for pair in pairs)
        if not self.pairs:
            for axis,graph in enumerate(geometry.axes):
                # Existing local declarations become screen-space geometry once.
                graph.set_positions({e:p+geometry.offset[axis] for e,p in graph.values().items()})
                self.pairs.append(tuple(graph.edge(1,str(i),p) for i,p in enumerate(pairs[axis])))
                self.screen.append(tuple(graph.edge(2,str(i),p) for i,p in enumerate(screen[axis])))
        else:
            for axis,graph in enumerate(geometry.axes):
                near,far=self.pairs[axis]
                shift=pairs[axis][0]-graph.position(near)
                if shift:
                    self.cancel()
                    graph.set_positions({e:p+shift for e,p in graph.values().items()
                                         if e not in self.screen[axis]})
                    graph.end_drag()
                graph.set_positions(dict(zip(self.screen[axis],screen[axis])))
                if abs(graph.position(far)-pairs[axis][1])>1e-6:
                    self.contacts(True)
                    graph.solve(far,pairs[axis][1],[near])
                    if mode=='walls' and abs(graph.position(far)-pairs[axis][1])>1e-6:
                        raise ValueError('fixed native allocation cannot satisfy the retained minimum spans')
                    self.contacts(False)
                    graph.end_drag()
        for axis,graph in enumerate(geometry.axes):
            near,far=self.pairs[axis];sn,sf=self.screen[axis]
            graph.replace_cells(1,[(near,far,1.,8192.)])
            graph.replace_cells(2,[(sn,near,0.,None),(far,sf,0.,None)] if mode!='walls' else [])
        for node in tuple(geometry.frames):self.connect(node)
        self.last=pairs
        for axis,items in enumerate(pending):
            for index,amount in items:
                if self.drag_index[axis]!=index:
                    self.token+=1;self.drag_total[axis]=0.;self.drag_index[axis]=index
                    self.drag_token[axis]=self.token
                    if not self.contact:self.contacts(True)
                self.drag_total[axis]+=amount
                graph=geometry.axes[axis];pair=self.pairs[axis]
                graph.drag(self.drag_token[axis],pair[index],self.drag_total[axis],self.walls(axis),pair[1-index])
        geometry.apply()

    def connect(self,node):
        geometry=self.geometry
        if not self.pairs or node not in geometry.frames:return
        info=geometry.cache.graph.info(node)
        serial,pairs,_=geometry.frames[node]
        if not info['parent'] and not info['portal']:
            # The hosted root fills the surface below its native title strip.
            for axis,(graph,(n,f),(a,b)) in enumerate(zip(geometry.axes,self.pairs,pairs)):
                gap=0. if axis==0 else geometry.cache.graph.info(node)['rect'][1]
                graph.replace_cells(serial+2*10**9,[(n,a,gap,gap),(b,f,0.,0.)])
        elif info['portal']:
            outer=geometry.windows.get(node)
            if outer:
                for graph,(n,f),(a,b) in zip(geometry.axes,self.pairs,outer):
                    graph.replace_cells(serial+2*10**9,[(n,a,0.,None),(b,f,0.,None)])

    def walls(self,axis):
        return list(self.screen[axis] if self.mode!='walls' else self.pairs[axis])

    def pointer_origin(self):
        if self.os_frame is not None:
            return tuple(self.os_frame.applied_origin(axis) for axis in ('x','y'))
        return self.geometry.offset

    def move_policy(self,moving):
        for axis,graph in enumerate(self.geometry.axes):
            n,f=self.pairs[axis];sn,sf=self.screen[axis]
            if self.mode=='walls':cells=[]
            elif moving:cells=[(sn,n,0.,None)] if axis==1 else []
            else:cells=[(sn,n,0.,None),(f,sf,0.,None)]
            graph.replace_cells(2,cells)

    def contacts(self,enabled):
        """Only native containment adds sibling contacts, in individual-edge order."""
        self.contact=enabled
        for axis,graph in enumerate(self.geometry.axes):
            cells=[]
            if enabled:
                edges=[]
                for node,pairs in self.geometry.windows.items():
                    a,b=pairs[axis]
                    edges.extend([(graph.position(a),a,0),(graph.position(b),b,1)])
                    # Under external containment a window can shrink, not inflate
                    # merely because a neighbour's gap is being consumed.
                    cells.append((a,b,0.,graph.position(b)-graph.position(a)))
                edges.sort()
                for (pa,a,ra),(pb,b,rb) in zip(edges,edges[1:]):
                    floor=0. if (ra,rb)==(1,0) else min(12.,max(0.,pb-pa))
                    cells.append((a,b,floor,None))
            graph.replace_cells(3,cells)

    def commit(self):
        if not self.pairs:return
        geometry=self.geometry
        pairs=tuple(tuple(g.position(e) for e in p) for g,p in zip(geometry.axes,self.pairs))
        geometry.offset=tuple(p[0] for p in pairs)
        geometry.cache._positions.clear()
        for node in geometry.cache.root_ids:
            if node in geometry.frames and not geometry.cache.graph.info(node)['portal']:
                x,y,w,h=geometry.frame_rect(node)
                rect=(x-geometry.offset[0],y-geometry.offset[1],max(1.,w),max(0.,h))
                geometry.cache.graph.allocate(node,rect)
                value,args=geometry.cache.invocations[node]
                geometry.cache.invocations[node]=(value,{**args,'width':rect[2],'height':rect[3]})
        if self.os_frame is not None:self.os_frame.prototype_commit(pairs)
        self.last=pairs

    def cancel(self):
        for graph in self.geometry.axes:
            graph.end_drag();graph.remove(4)
        self.drag_total=[0.,0.];self.drag_index=[None,None]
        self.geometry.gesture=None
        self.geometry.frozen.clear()
        if self.contact:self.contacts(False)

    def release(self):
        self.cancel()
