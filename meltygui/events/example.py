"""
Example: InputHandler with various backends.
"""
from meltygui.events.input_handler import InputHandler


def demo_basic():
    """Core API demo without any backend dependencies."""
    handler = InputHandler()
    
    print("=== Click Demo ===")
    handler.clear_pending()
    handler.register_hovered("button", 0, ["left_mouse_clicked", "left_mouse_double_clicked"])
    handler.register_hovered("panel", 1, ["left_mouse_down"])
    
    handler.feed_down("left_mouse", 100, 100)
    handler.feed_up("left_mouse", 100, 100)
    
    for view_id, evts in handler.process_frame().items():
        for e in evts:
            print(f"  {view_id}: {e.input_id}:{e.action}")
    
    print("\n=== Drag Demo ===")
    handler.clear_pending()
    handler.register_hovered("panel", 0, ["left_mouse_dragged", "left_mouse_up"])
    
    handler.feed_down("left_mouse", 100, 100)
    handler.feed_move(120, 110)
    handler.feed_move(140, 120)
    handler.feed_up("left_mouse", 140, 120)
    
    for view_id, evts in handler.process_frame().items():
        for e in evts:
            print(f"  {view_id}: {e.input_id}:{e.action} dx={e.dx:.0f} dy={e.dy:.0f}")
    
    print("\n=== Keyboard Demo ===")
    handler.clear_pending()
    handler.register_hovered("text", 0, ["a_down", "space_down", "enter_down"])
    
    handler.feed_down("a")
    handler.feed_down("space")
    handler.feed_down("enter")
    
    for view_id, evts in handler.process_frame().items():
        for e in evts:
            print(f"  {view_id}: {e.input_id}:{e.action}")


def demo_json_backend():
    """Demo using JsonBackend for toolkit integration."""
    from meltygui.events.pynput_backend import JsonBackend
    
    handler = InputHandler()
    backend = JsonBackend(handler)
    
    print("\n=== JsonBackend Demo ===")
    handler.clear_pending()
    handler.register_hovered("panel", 0, [
        "left_mouse_down", "left_mouse_up", "left_mouse_dragged", "cursor_moved"
    ])
    
    # Simulate drag
    backend.push_down("left_mouse", 100, 100)
    backend.push_move(110, 105)
    backend.push_move(120, 110)
    backend.push_up("left_mouse", 120, 110)
    
    backend.pump()
    
    for view_id, evts in handler.process_frame().items():
        for e in evts:
            print(f"  {view_id}: {e.input_id}:{e.action}")


def demo_pynput():
    """Demo with real hardware (requires pynput)."""
    try:
        from meltygui.events.pynput_backend import PynputBackend, HAS_PYNPUT
        if not HAS_PYNPUT:
            raise ImportError()
    except ImportError:
        print("\n=== Pynput Demo ===")
        print("  Skipped (pip install pynput)")
        return
    
    print("\n=== Pynput Demo ===")
    print("  Move mouse and click for 2 seconds...")
    
    handler = InputHandler()
    backend = PynputBackend(handler)
    backend.start()
    
    import time
    start = time.time()
    
    while time.time() - start < 2.0:
        handler.clear_pending()
        handler.register_hovered("root", 0, [
            "left_mouse_down", "left_mouse_up", "left_mouse_clicked",
            "cursor_moved", "left_mouse_dragged"
        ])
        
        backend.pump()
        events = handler.process_frame()
        
        for evts in events.values():
            for e in evts:
                print(f"  {e.input_id}:{e.action} @ ({e.x:.0f}, {e.y:.0f})")
        
        time.sleep(0.016)
    
    backend.stop()


if __name__ == "__main__":
    # demo_basic()
    # demo_json_backend()
    demo_pynput()
