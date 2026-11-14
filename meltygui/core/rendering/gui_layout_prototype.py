"""Retained rows/columns for @gui. Rust owns edge identity, limits and solving.

The bridge declares relationships and translates their solved bounds into view
allocations. Cached owners keep declarations; only successful executions retire
missing layouts. A cell can contain ordinary ImGui or a cached @gui child.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import math


class _Tracked(dict):
    """A geometry table that counts its own mutations in a shared counter.

    Every cached view execution opens a layout transaction. While the counter
    and the solver graphs are unchanged, consecutive transactions share one
    snapshot instead of copying every table and forking both graphs again.
    Reads stay plain dictionary operations.
    """
    __slots__ = ('changes',)

    def __init__(self, changes, *contents):
        super().__init__(*contents)
        self.changes = changes

    def __setitem__(self, key, value):
        self.changes[0] += 1
        super().__setitem__(key, value)

    def __delitem__(self, key):
        self.changes[0] += 1
        super().__delitem__(key)

    def pop(self, key, *default):
        if key in self:
            self.changes[0] += 1
        return super().pop(key, *default)

    def clear(self):
        if self:
            self.changes[0] += 1
        super().clear()

    def update(self, *contents, **entries):
        self.changes[0] += 1
        super().update(*contents, **entries)

    def setdefault(self, key, default=None):
        if key not in self:
            self.changes[0] += 1
        return super().setdefault(key, default)

    def popitem(self):
        self.changes[0] += 1
        return super().popitem()


@dataclass
class Layout:
    manager: object
    owner: int
    identity: tuple
    serial: int
    axis: int
    keys: tuple
    edges: list
    cross: tuple
    padding: float
    minima: tuple
    maxima: tuple
    raw: bool = False
    children: dict = field(default_factory=dict)

    @contextmanager
    def cell(self, key):
        if key not in self.keys:
            raise KeyError(key)
        index = self.keys.index(key)
        manager, cache = self.manager, self.manager.cache
        previous = manager.cell_scope
        manager.cell_scope = (self, index)
        count = len(self.children)
        before = 0
        if cache.graphics:
            import meltygui_imgui as imgui
            rect = manager.cell_rect(self, index)
            ox, oy = manager.position(self.owner)
            x, y, w, h = rect
            imgui.set_cursor_screen_pos((x-ox, y-oy))
            dl = imgui.get_window_draw_list()
            dl.push_clip_rect(x-ox, y-oy, x-ox+w, y-oy+h, True)
            before = dl.vtx_buffer_size
        try:
            yield manager.cell_rect(self, index)[2:]
        finally:
            if cache.graphics:
                dl.pop_clip_rect()
                # Cached child images contribute four vertices each. Other
                # geometry belongs to Python and must be recaptured on reflow.
                if dl.vtx_buffer_size-before > 4*(len(self.children)-count):
                    self.raw = True
                    manager.changes[0] += 1
            manager.cell_scope = previous

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.manager.cache.graphics and exc_type is None:
            import meltygui_imgui as imgui
            x, y, w, h = self.manager.layout_rect(self)
            ox, oy = self.manager.position(self.owner)
            imgui.set_cursor_screen_pos((x-ox, y-oy))
            imgui.dummy(w, h)


class GuiGeometry:
    def __init__(self, cache):
        from meltygui.core.rendering._gui_native import EdgeGraph
        self.cache = cache
        self.axes = (EdgeGraph(), EdgeGraph())
        # Counts mutations of the tables below and of Layout fields; with the
        # solver graphs' own counters it tells when a snapshot is still current.
        self.changes = [0]
        self.layouts = _Tracked(self.changes)
        self.owner_layouts = {}
        self.frames = _Tracked(self.changes)
        self.bindings = _Tracked(self.changes)
        self.staged = {}
        self.transactions = {}
        self._snapshot_stamp = None
        self._snapshot_value = None
        self.cell_scope = None
        self.serial = 1000000
        self.gesture = None
        self.sequence = 0
        self.last_error = None
        self.offset = (0.,0.)
        # Height of the window's title bar: the root's space starts under it
        # (gui_window_prototype.title_bar). 0 without one.
        self.inset = 0.
        self.native = None
        self.windows = _Tracked(self.changes)
        self.claim = (False,False)
        self.frozen = set()
        self.drag_parents = None

    def position(self, node):
        x,y=self.cache._position(node)
        return x+self.offset[0],y+self.offset[1]

    def projected_position(self,node):
        if not node:
            if self.native and self.native.pairs:
                return self.native.origin(tuple(g.position(pair[0]) for g,pair in zip(self.axes,self.native.pairs)))
            return self.offset
        if node in self.frames:return self.frame_rect(node)[:2]
        if node in self.bindings:return self.cell_rect(*self.bindings[node])[:2]
        graph=self.cache.graph
        parent=graph.parent(node)
        x,y=self.projected_position(parent)
        if parent:
            sx,sy=graph.state(parent).scroll_offset
            x,y=x-sx,y-sy
        state=graph.state(node)
        px,py=state.left,state.top;ox,oy=state.content_offset
        return x+px+ox,y+py+oy

    def carry_origin(self,node):
        parent=self.cache.graph.parent(node)
        position=self.projected_position(parent)
        if self.portal(parent) is None:
            # Native near-edge resize rebases local coordinates; the Rust
            # solver already owns the windows' screen positions. Carry only
            # actual layout reflow here, not the native frame's translation.
            origin=self.projected_position(0)
            return tuple(p-o for p,o in zip(position,origin))
        return position

    def translate_window(self,node,axis,delta):
        if not delta:return
        graph=self.axes[axis]
        owned=set()
        for child,(serial,_,_) in self.frames.items():
            if self.portal(child)==node:owned.update(graph.owner_edges(serial))
        for layout in self.layouts.values():
            if self.portal(layout.owner)==node:owned.update(graph.owner_edges(layout.serial))
        graph.set_positions({e:graph.position(e)+delta for e in owned})

    def carry_windows(self,baseline):
        for node in sorted(self.windows,key=self.depth):
            parent=self.cache.graph.parent(node)
            if not parent:continue
            old=baseline[node]
            new=self.carry_origin(node)
            for axis in range(2):
                delta=new[axis]-old[axis]
                self.translate_window(node,axis,delta)
                self.contain_carried(node,axis,delta)

    def contain_carried(self,node,axis,delta):
        if not delta or self.native is None:return
        graph=self.axes[axis]
        edge=self.windows[node][axis][1 if delta>0 else 0]
        target=graph.position(edge)
        graph.set_positions({edge:target-delta})
        graph.solve(edge,target,self.native.walls(axis))

    def _id(self):
        self.serial += 1
        return self.serial

    def window(self,node):
        self._open_transactions()
        pairs=self.bounds(node)
        serial=self.frames[node][0]
        args=self.cache.invocations[node][1]
        for axis,(graph,(near,far)) in enumerate(zip(self.axes,pairs)):
            lo=args.get('min_width' if axis==0 else 'min_height',0.)
            hi=args.get('max_width' if axis==0 else 'max_height',8192.)
            graph.replace_cells(serial,[(near,far,float(lo),float(hi))])
        if node not in self.windows:
            graph=self.axes[1]
            # The title strip is the window's content offset (0 for a popover).
            title=float(self.cache.title_height(node))
            top=graph.edge(serial,'title',graph.position(pairs[1][0])-title)
            graph.replace_cells(serial+10**9,[(top,pairs[1][0],title,title)])
            self.windows[node]=(pairs[0],(top,pairs[1][1]))
        if self.native is not None:self.native.connect(node)
        return self.windows[node]

    def walls(self,axis,node):
        if self.native is not None:
            return self.native.walls(axis)
        return list(self.bounds(self._frame_owner(node))[axis])

    def frame_rect(self,node):
        pairs=self.frames[node][1]
        values=[tuple(g.position(e) for e in pair) for g,pair in zip(self.axes,pairs)]
        return values[0][0],values[1][0],values[0][1]-values[0][0],values[1][1]-values[1][0]

    def bounds(self, node):
        self._open_transactions()
        binding = self.bindings.get(node)
        if node not in self.frames:
            x, y = self.position(node)
            _, _, w, h = self.cache.graph.rect(node)
            serial = self._id()
            pairs = tuple((g.edge(serial, 'near', p), g.edge(serial, 'far', p+s))
                          for g, p, s in zip(self.axes, (x,y), (w,h)))
            self.frames[node] = (serial, pairs, (x,y,w,h))
            if binding:
                layout, index = binding
                along = (layout.edges[index], layout.edges[index+1])
                outer = (along,layout.cross) if layout.axis==0 else (layout.cross,along)
                for graph, (a,b), (c,d) in zip(self.axes, outer, pairs):
                    p=layout.padding
                    graph.replace_cells(serial, [(a,c,p,p),(d,b,p,p),(c,d,0.,None)])
        if self.native is not None:
            self.native.connect(node)
        return self.frames[node][1]

    def sync_projection(self, node):
        owner=self._frame_owner(node)
        if owner not in self.frames:return
        current=self.frame_rect(owner)
        desired=self.position(owner)
        for axis,graph in enumerate(self.axes):
            delta=desired[axis]-current[axis]
            if not delta:continue
            edges={e for child,(serial,_,_) in self.frames.items() if self._frame_owner(child)==owner
                   for e in graph.owner_edges(serial)}
            edges.update(e for layout in self.layouts.values() if self._frame_owner(layout.owner)==owner
                         for e in graph.owner_edges(layout.serial))
            graph.set_positions({e:graph.position(e)+delta for e in edges})

    def projection_delta(self, node):
        live=self.position(node);projected=self.projected_position(node)
        return live[0]-projected[0],live[1]-projected[1]

    def _snapshot(self):
        """The current geometry, copied only when it changed since the last copy."""
        stamp = (self.changes[0], self.axes[0].mutations, self.axes[1].mutations)
        if stamp != self._snapshot_stamp:
            saved = {}
            for key, layout in self.layouts.items():
                state = layout.__dict__.copy()
                state['children'] = dict(layout.children)
                saved[key] = (layout,state)
            self._snapshot_value = (tuple(g.fork() for g in self.axes), saved,
                                    dict(self.frames), dict(self.bindings), dict(self.windows))
            self._snapshot_stamp = stamp
        return self._snapshot_value

    def _open_transactions(self):
        """Give every executing owner a transaction before the first mutation.

        An owner that began while no row, column or window had ever been
        declared skipped its snapshot. Nothing can have changed since it began,
        so the current geometry is exactly what it would have saved.
        """
        transactions = self.transactions
        for owner in self.cache.stack:
            if owner not in transactions:
                transactions[owner] = self._snapshot()
                self.staged[owner] = set()

    def begin(self, node):
        """Open the owner's layout transaction. Returns True when reconciling
        its frame moved views, so the caller re-reads the node's origin."""
        if not self.transactions and self.gesture is None:
            for window in self.windows:self.sync_projection(window)
        # Geometry commits with the declaration owner; failed bodies leave no
        # dividers. An unchanged layout shares its snapshot between owners.
        self.transactions[node] = self._snapshot()
        self.staged[node] = set()
        moved = False
        if node in self.frames and node not in self.bindings:
            serial, pairs, old = self.frames[node]
            old=self.frame_rect(node)
            x, y = self.position(node)
            _, _, w, h = self.cache.graph.rect(node)
            rect = (x,y,w,h)
            if rect != old:
                for axis, (graph, pair) in enumerate(zip(self.axes, pairs)):
                    near, far = pair
                    shift = rect[axis]-old[axis]
                    # Translate the entire connected allocation, including bound
                    # child frames. Leaving those frames at their old origin
                    # violates their fixed padding constraints; each subsequent
                    # capture then mistakes that offset for more content size.
                    ids = {e for l in self.layouts.values() if self._frame_owner(l.owner)==node
                           for e in graph.owner_edges(l.serial)}
                    ids.update(e for child,(owner,_,_) in self.frames.items()
                               if self._frame_owner(child)==node for e in graph.owner_edges(owner))
                    graph.set_positions({e:graph.position(e)+shift for e in ids})
                    graph.solve(far, rect[axis]+rect[axis+2], [near])
                self.frames[node] = (serial,pairs,rect)
                self.apply()
                moved = True
        return moved

    def captured(self,node):
        if node not in self.frames:return
        _,_,w,h=self.cache.graph.rect(node)
        for graph,(near,far),size in zip(self.axes,self.frames[node][1],(w,h)):
            graph.solve(far,graph.position(near)+size,[near])

    def commit(self, node):
        fresh = self.staged.pop(node, set())
        # Only an owner of layouts can have stale layouts or cell bindings.
        owned = self.owner_layouts.get(node)
        if owned:
            for key in sorted(owned-fresh, key=lambda key:self.layouts[key].serial):
                self.remove_layout(key)
            if node in self.owner_layouts:
                for child,(layout,index) in tuple(self.bindings.items()):
                    if layout.owner==node and child not in layout.children:
                        self.bindings.pop(child)
        if self.windows:
            graph = self.cache.graph
            for window in sorted((w for w in self.windows if graph.contains(w)),key=self.depth):
                parent=self.cache.graph.parent(window)
                if window!=node and self.descendant(parent,node):
                    desired=self.position(window)
                    current=self.frame_rect(window)
                    for axis in range(2):
                        delta=desired[axis]-current[axis]
                        self.translate_window(window,axis,delta)
                        self.contain_carried(window,axis,delta)
        self.apply()
        self.transactions.pop(node, None)

    def abort(self, node):
        self.staged.pop(node, None)
        snapshot = self.transactions.pop(node, None)
        if snapshot:
            # Enclosing transactions can share this snapshot: restore copies.
            axes, saved, frames, bindings, windows = snapshot
            changes = self.changes
            self.axes = tuple(g.fork() for g in axes)
            self.frames, self.bindings = _Tracked(changes, frames), _Tracked(changes, bindings)
            self.windows = _Tracked(changes, windows)
            self.layouts = _Tracked(changes, {key:layout for key,(layout,state) in saved.items()})
            self.owner_layouts = {}
            for key,layout in self.layouts.items():self.owner_layouts.setdefault(layout.owner,set()).add(key)
            for layout,state in saved.values():
                layout.__dict__.update(state)
                layout.children = _Tracked(changes, state['children'])
            changes[0] += 1

    def _frame_owner(self, node):
        while node in self.bindings:
            node = self.bindings[node][0].owner
        return node

    def declare(self, keys, *, axis, key, sizes=None, mins=None, maxes=None, fixed=None, padding=4.):
        if not self.cache.stack:
            raise RuntimeError('rows/columns require a cached @gui owner')
        self._open_transactions()
        owner = self.cache.current
        keys = tuple(range(keys)) if isinstance(keys, int) else tuple(keys)
        if not keys or len(set(keys)) != len(keys):
            raise ValueError('layout cell keys must be nonempty and unique')
        def values(value, default):
            if value is None: return (default,)*len(keys)
            if isinstance(value,(int,float)): return (value,)*len(keys)
            value = tuple(value)
            if len(value)!=len(keys): raise ValueError('one span value per cell required')
            return value
        minima = values(mins, 60. if axis==0 else 40.)
        maxima = values(maxes, None)
        fixed_spans=values(fixed,None)
        minima=tuple(lo if span is None else span for lo,span in zip(minima,fixed_spans))
        maxima=tuple(hi if span is None else span for hi,span in zip(maxima,fixed_spans))
        if any(not math.isfinite(lo) or lo<0 or (hi is not None and (not math.isfinite(hi) or hi<lo))
               for lo,hi in zip(minima,maxima)):
            raise ValueError('cell limits must be finite, nonnegative, and minimum <= maximum')
        if not math.isfinite(padding) or padding<0:
            raise ValueError('padding must be finite and nonnegative')
        identity = (owner, axis, key)
        if identity in self.staged[owner]:
            raise ValueError('layout keys must be unique within their owner')
        self.staged[owner].add(identity)
        if self.cell_scope is not None and self.cell_scope[0].owner==owner:
            outer,index = self.cell_scope
            along = (outer.edges[index],outer.edges[index+1])
            bounds = (along,outer.cross) if outer.axis==0 else (outer.cross,along)
        else:
            bounds = self.bounds(owner)
        pair, cross = bounds[axis], bounds[1-axis]
        graph = self.axes[axis]
        layout = self.layouts.get(identity)
        if layout is None or layout.keys!=keys or (layout.edges[0],layout.edges[-1])!=pair:
            if layout is not None: self.remove_layout(identity)
            serial = self._id()
            a, b = map(graph.position,pair)
            weights = values(sizes, None)
            explicit = sum(v for v in weights if v is not None)
            missing = sum(v is None for v in weights)
            widths = [max(0.,(b-a-explicit)/missing) if v is None else float(v) for v in weights]
            if any(not math.isfinite(v) or v<0 for v in widths):
                raise ValueError('initial sizes must be finite and nonnegative')
            if not missing and sum(widths)>0:
                widths = [v*(b-a)/sum(widths) for v in widths]
            edges = [pair[0]]
            at = a
            for index, width in enumerate(widths[:-1]):
                at += width
                edges.append(graph.edge(serial,str(keys[index]),at))
            edges.append(pair[1])
            layout = Layout(self,owner,identity,serial,axis,keys,edges,cross,float(padding),minima,maxima,
                            children=_Tracked(self.changes))
            self.layouts[identity] = layout
            self.owner_layouts.setdefault(owner,set()).add(identity)
        graph.replace_cells(layout.serial, [(a,b,float(lo),None if hi is None else float(hi))
                            for a,b,lo,hi in zip(layout.edges,layout.edges[1:],minima,maxima)])
        layout.cross, layout.padding = cross, float(padding)
        layout.minima, layout.maxima, layout.raw = minima, maxima, False
        layout.children.clear()
        self.changes[0] += 1
        # Normalize initial dividers with BOTH frame edges held. The requested
        # minima are never silently lowered to fit an impossible allocation.
        near, far = layout.edges[0], layout.edges[-1]
        available = graph.position(far)-graph.position(near)
        required = graph.span(near,far) or 0
        cap = graph.span(near,far,True)
        if available+1e-6 < required:
            graph.solve(far,graph.position(near)+required,self.walls(axis,owner))
        elif cap is not None and available-1e-6>cap:
            graph.solve(far,graph.position(near)+cap,self.walls(axis,owner))
        available=graph.position(far)-graph.position(near)
        if available+1e-6 < required or (cap is not None and available-1e-6>cap):
            raise ValueError(f'unsatisfied layout span: available={available:g}, minimum={required:g}, maximum={cap}')
        # Project the initial/requested widths into their allowed intervals.
        # Starting sizes are preferences; fixed spans and limits are contracts.
        lows=[graph.span(a,b) or 0. for a,b in zip(layout.edges,layout.edges[1:])]
        highs=[graph.span(a,b,True) for a,b in zip(layout.edges,layout.edges[1:])]
        widths=[max(lo,min(float('inf') if hi is None else hi,graph.position(b)-graph.position(a)))
                for a,b,lo,hi in zip(layout.edges,layout.edges[1:],lows,highs)]
        for _ in range(len(widths)+1):
            delta=available-sum(widths)
            if abs(delta)<1e-6:break
            eligible=[i for i,w in enumerate(widths) if w>lows[i]+1e-6] if delta<0 else [
                i for i,w in enumerate(widths) if highs[i] is None or w<highs[i]-1e-6]
            if not eligible:raise ValueError('unsatisfied shared layout constraints')
            for i in eligible:
                widths[i]=max(lows[i],min(float('inf') if highs[i] is None else highs[i],widths[i]+delta/len(eligible)))
        at=graph.position(near)
        for edge,size in zip(layout.edges[1:-1],widths):
            at+=size
            graph.solve(edge,at,[near,far])
        for i, edge in enumerate(layout.edges[1:-1],1):
            low = graph.position(near)+(graph.span(near,edge) or 0.)
            high = graph.position(far)-(graph.span(edge,far) or 0.)
            graph.solve(edge, max(low,min(high,graph.position(edge))), [near,far])
        return layout

    def cell_rect(self, layout, index):
        along = self.axes[layout.axis]
        cross = self.axes[1-layout.axis]
        a,b = (along.position(e) for e in layout.edges[index:index+2])
        c,d = (cross.position(e) for e in layout.cross)
        x,y,w,h = (a,c,b-a,d-c) if layout.axis==0 else (c,a,d-c,b-a)
        p=layout.padding
        return x+p,y+p,max(0.,w-2*p),max(0.,h-2*p)

    def layout_rect(self, layout):
        a,b = (self.axes[layout.axis].position(e) for e in (layout.edges[0],layout.edges[-1]))
        c,d = (self.axes[1-layout.axis].position(e) for e in layout.cross)
        return (a,c,b-a,d-c) if layout.axis==0 else (c,a,d-c,b-a)

    def allocation(self, parent):
        scope = self.cell_scope
        if scope and scope[0].owner==parent:
            x,y,w,h=self.cell_rect(*scope)
            return x-self.offset[0],y-self.offset[1],w,h
        return None

    def bind(self, node, parent, *, portal=False):
        scope=self.cell_scope
        if scope and scope[0].owner==parent and not portal:
            self._open_transactions()
            previous=self.bindings.get(node)
            if node in self.frames and (previous is None or previous[0] is not scope[0] or previous[1]!=scope[1]):
                self.drop_frame(node)
                self.cache.graph.invalidate_node(node)
            self.bindings[node]=scope
            scope[0].children[node]=scope[1]
        else:
            self.bindings.pop(node, None)
        if portal:
            self.window(node)

    def apply(self):
        cache = self.cache
        if self.native is not None:
            self.native.commit()
        if not self.windows and not self.bindings:
            return
        # Commit placement parents before their descendants. Portal rectangles
        # remain parent-relative even across ordinary intermediate views.
        graph = cache.graph
        for node in sorted((n for n in self.windows if graph.contains(n)),key=lambda n:self.depth(n)):
            if node not in cache.records:continue
            x,y,w,h=self.frame_rect(node)
            parent=cache.graph.parent(node)
            ox,oy=self.position(parent) if parent else self.offset
            sx,sy=cache.graph.state(parent).scroll_offset if parent else (0.,0.)
            cx,cy=cache.graph.state(node).content_offset
            rect=(x-ox+sx-cx,y-oy+sy-cy,max(1.,w),max(0.,h))
            cache.graph.allocate(node,rect)
        for node,(layout,index) in tuple(self.bindings.items()):
            if not graph.contains(node) or not graph.contains(layout.owner): continue
            x,y,w,h=self.cell_rect(layout,index)
            dx,dy=self.projection_delta(layout.owner)
            x,y=x+dx,y+dy
            ox,oy=self.position(layout.owner)
            sx,sy=cache.graph.state(layout.owner).scroll_offset
            cx,cy=cache.graph.state(node).content_offset
            rect=(x-ox+sx-cx,y-oy+sy-cy,max(1.,w),h)
            freeze=(self.gesture and cache.invocations[node][1].get('freeze_resize',False)
                    and (node in self.frozen or not cache.graph.dirty(node))
                    and not cache.graph.pending(node) and node not in cache.events
                    and node not in cache.fresh)
            if cache.graph.allocate(node,rect):
                if freeze:self.frozen.add(node)
                cache._last_pointer=None
            if cache.graphics and cache.gpu and node in cache.backgrounds:
                cache.gpu.place_child(layout.owner,node,rect,(x-ox,y-oy,x-ox+w,y-oy+h))

    def depth(self,node):
        depth=0
        while node:
            depth+=1
            node=self.cache.graph.parent(node)
        return depth

    def drag(self, layout, index, displacement, *, token=1):
        if self.gesture is None:
            for window in self.windows:self.sync_projection(window)
            self.sync_projection(layout.owner)
        graph=self.axes[layout.axis]
        root=self._frame_owner(layout.owner)
        key=(token,layout.serial,index,tuple(g.stats()[2] for g in self.axes))
        if self.drag_parents is None or self.drag_parents[0]!=key:
            self.drag_parents=(key,{node:self.carry_origin(node)
                                    for node in self.windows})
        changed=graph.drag(token,layout.edges[index],displacement,self.walls(layout.axis,root))
        self.carry_windows(self.drag_parents[1])
        if changed:
            for other in self.layouts.values():
                if other.raw and self._frame_owner(other.owner)==root:
                    self.cache.graph.invalidate_node(other.owner)
            self.apply()
        return changed

    def portal(self,node):
        return self.cache.graph.portal_owner(node)

    def hit(self,x,y):
        portal,hits=self.cache.graph.hits(x-self.offset[0],y-self.offset[1])
        layouts=[]
        for node,_,_ in hits:
            for key in reversed(sorted(self.owner_layouts.get(node,()),key=lambda k:self.layouts[k].serial)):
                layout=self.layouts[key]
                dx,dy=self.projection_delta(node)
                rx,ry,w,h=self.layout_rect(layout)
                if rx+dx<=x<rx+dx+w and ry+dy<=y<ry+dy+h:layouts.append(layout)
        return portal,layouts

    def pointer(self, position, *, down=False, pressed=False, released=False,
                right_down=False, right_pressed=False, right_released=False,
                reverse=False, widget_claim=(False,False), drag_position=None):
        point=tuple(position[i]-self.cache.origin[i]+self.offset[i] for i in range(2))
        # Hit testing uses proposed local allocations; drag displacement uses
        # the actually applied OS origin so delayed moves cannot add motion.
        if drag_position is None:
            pointer_origin=self.native.pointer_origin() if self.native else self.offset
            pointer=tuple(position[i]-self.cache.origin[i]+pointer_origin[i] for i in range(2))
        else:
            # The surface input adapter already removes native window movement.
            # Adding the moving OS origin again feeds our resize back into itself.
            pointer=tuple(drag_position)
        if self.gesture and self.gesture['versions']!=tuple(g.stats()[2] for g in self.axes):
            self.end_gesture()
        portal,layouts=self.hit(*point)
        if not self.gesture and (pressed or right_pressed):
            for window in self.windows:self.sync_projection(window)
            for layout in layouts:self.sync_projection(layout.owner)
        # Unclaimed native background belongs to the host's WM move gesture.
        # Driving it through os_frame queues a next-frame surface move and mixes
        # asynchronous native origins with local pointer samples. Only retained
        # portals/dividers own app-driven left drags here.
        targets={}; divider=None
        for layout in layouts:
            graph=self.axes[layout.axis]
            for index,edge in enumerate(layout.edges[1:-1],1):
                if abs(point[layout.axis]-graph.position(edge))<=4:
                    divider=(layout,index);break
            if divider:break
        self.claim=(bool(divider) or portal is not None,
                    bool(layouts) or portal is not None)
        if not self.gesture:
            move=False
            if pressed and divider:
                layout,index=divider
                targets[layout.axis]=(layout.edges[index],None,layout.owner)
            elif pressed and portal and not widget_claim[0] and self.cache.invocations[portal][1].get('movable',True):
                move=True
                targets={axis:(pair[0],pair[1],portal) for axis,pair in enumerate(self.windows[portal])}
            elif right_pressed and not widget_claim[1] and (layouts or portal) and (
                    not portal or self.cache.invocations[portal][1].get('resizable',True)):
                for layout in layouts:
                    axis=layout.axis
                    if axis in targets:continue
                    graph=self.axes[axis]
                    for index,(a,b) in enumerate(zip(layout.edges,layout.edges[1:])):
                        if graph.position(a)<=point[axis]<graph.position(b):
                            targets[axis]=(a,b,layout.owner) if reverse else (b,a,layout.owner)
                            break
                for axis in range(2):
                    if axis in targets:continue
                    pair=self.windows[portal][axis] if portal else self.native.pairs[axis] if self.native else None
                    if pair:
                        targets[axis]=(pair[0],pair[1],portal) if reverse else (pair[1],pair[0],portal)
            if targets:
                self.sequence+=1
                self.gesture={'targets':targets,'start':pointer,'token':self.sequence,
                              'button':1 if right_pressed else 0,'move':move,
                              'base':tuple(g.values() for g in self.axes),
                              'parents':{node:self.carry_origin(node)
                                         for node in self.windows}}
                if portal:
                    for node in self.cache.graph.windows():
                        if self.descendant(node,portal):self.cache.graph.raise_window(node)
                if move:
                    for axis,(a,b,owner) in targets.items():
                        graph=self.axes[axis];span=graph.position(b)-graph.position(a)
                        graph.replace_cells(4,[(a,b,span,span)])
                    if self.native:self.native.move_policy(True)
                self.gesture['versions']=tuple(g.stats()[2] for g in self.axes)
        if not self.gesture:return any(self.claim)
        gesture=self.gesture
        before=tuple(g.values() for g in self.axes)
        self.claim=(gesture['button']==0,gesture['button']==1)
        for axis,(edge,opposite,owner) in gesture['targets'].items():
            graph=self.axes[axis]
            delta=pointer[axis]-gesture['start'][axis]
            walls=self.walls(axis,owner) if owner else self.native.walls(axis)
            if gesture['move']:
                graph.set_positions(gesture['base'][axis])
                # Move each placement descendant exactly once. Shared/borrowed
                # boundaries are represented by their original edge identity.
                owned=set()
                for node,(serial,pairs,_) in self.frames.items():
                    if self.descendant(node,owner):owned.update(graph.owner_edges(serial))
                for layout in self.layouts.values():
                    if self.descendant(layout.owner,owner):owned.update(graph.owner_edges(layout.serial))
                owned.discard(edge);owned.discard(opposite)
                graph.set_positions({e:gesture['base'][axis][e]+delta for e in owned})
                graph.solve(edge,gesture['base'][axis][edge]+delta,walls)
                actual=graph.position(edge)-gesture['base'][axis][edge]
                graph.set_positions({e:gesture['base'][axis][e]+actual for e in owned})
                for node in sorted(self.windows,key=self.depth):
                    if node!=owner and self.descendant(node,owner):self.contain_carried(node,axis,actual)
            else:
                graph.drag(gesture['token'],edge,delta,walls,opposite)
        if not gesture['move']:self.carry_windows(gesture['parents'])
        if before!=tuple(g.values() for g in self.axes):
            self.invalidate_raw()
            self.apply()
        button_down=right_down if gesture['button'] else down
        button_up=right_released if gesture['button'] else released
        if button_up or not button_down:self.end_gesture()
        return True

    def descendant(self,node,ancestor):
        while node:
            if node==ancestor:return True
            node=self.cache.graph.parent(node)
        return False

    def invalidate_raw(self):
        for layout in self.layouts.values():
            if layout.raw:self.cache.graph.invalidate_node(layout.owner)

    def end_gesture(self):
        for graph in self.axes:
            graph.end_drag();graph.remove(4)
        self.gesture=None
        self.drag_parents=None
        self.frozen.clear()
        if self.native:
            self.native.move_policy(False)
            self.native.release()

    def paint(self,draw_list,origin,portal=None):
        shift=[origin[i]-self.offset[i] for i in range(2)]
        if portal:
            position=self.cache.window_rect(portal)[:2]
            for axis in range(2):
                at=position[axis]+origin[axis]
                shift[axis]+=math.floor(at+.5)-at
        if not self.layouts:return
        for node in self.cache.graph.visible_nodes():
            if self.portal(node)!=portal:continue
            keys=self.owner_layouts.get(node,())
            if not keys:continue
            clip=self.cache.graph.clip_rect(node)
            if clip[2]<=clip[0] or clip[3]<=clip[1]:continue
            dx,dy=self.projection_delta(node)
            draw_list.push_clip_rect(clip[0]+origin[0],clip[1]+origin[1],clip[2]+origin[0],clip[3]+origin[1],True)
            try:
                for key in keys:
                    layout=self.layouts[key]
                    rx,ry,w,h=self.layout_rect(layout)
                    rx+=shift[0]+dx;ry+=shift[1]+dy
                    left,top,right,bottom=(math.floor(v+.5) for v in (rx,ry,rx+w,ry+h))
                    for edge in layout.edges[1:-1]:
                        at=math.floor(self.axes[layout.axis].position(edge)+shift[layout.axis]+(dx,dy)[layout.axis]+.5)
                        if layout.axis==0:draw_list.add_rect_filled(at-1,top,at+1,bottom,0xFF777777)
                        else:draw_list.add_rect_filled(left,at-1,right,at+1,0xFF777777)
            finally:draw_list.pop_clip_rect()

    def remove_layout(self, identity):
        layout=self.layouts.pop(identity,None)
        if layout is None:return
        self.owner_layouts[layout.owner].discard(identity)
        if not self.owner_layouts[layout.owner]:self.owner_layouts.pop(layout.owner)
        dependents=self.axes[layout.axis].remove(layout.serial)
        for key,other in tuple(self.layouts.items()):
            if other.serial in dependents:self.remove_layout(key)
        for node,frame in tuple(self.frames.items()):
            if frame[0] in dependents:
                self.drop_frame(node)
                if node in self.cache.graph.nodes():self.cache.graph.invalidate_node(node)
        for node,binding in tuple(self.bindings.items()):
            if binding[0] is layout:self.bindings.pop(node,None)
        if self.gesture:self.end_gesture()

    def retire(self,node):
        self.frozen.discard(node)
        for key,l in tuple(self.layouts.items()):
            if l.owner==node:self.remove_layout(key)
        self.bindings.pop(node,None)
        self.windows.pop(node,None)
        self.drop_frame(node)

    def drop_frame(self,node):
        for key,layout in tuple(self.layouts.items()):
            if layout.owner==node:self.remove_layout(key)
        frame=self.frames.pop(node,None)
        if frame:
            for graph in self.axes:
                graph.remove(frame[0]+2*10**9)
                graph.remove(frame[0]+10**9)
                graph.remove(frame[0])


def columns(keys, *, key='columns', sizes=None, mins=None, maxes=None, fixed=None, padding=4.):
    from meltygui.core.rendering.gui_prototype import _window
    window=_window.get()
    if window is None:raise RuntimeError('columns require a @gui window')
    return window.cache.geometry.declare(keys,axis=0,key=key,sizes=sizes,mins=mins,maxes=maxes,fixed=fixed,padding=padding)


def rows(keys, *, key='rows', sizes=None, mins=None, maxes=None, fixed=None, padding=4.):
    from meltygui.core.rendering.gui_prototype import _window
    window=_window.get()
    if window is None:raise RuntimeError('rows require a @gui window')
    return window.cache.geometry.declare(keys,axis=1,key=key,sizes=sizes,mins=mins,maxes=maxes,fixed=fixed,padding=padding)
