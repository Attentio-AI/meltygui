import imgui
from src.lsd.gl_gui.hdr_color import pack_color
from imgui.core import _DrawList


class Box:
    def __init__(self, window_pos, width, height, priority=0):
        self.window_pos = window_pos
        self.width = width
        self.height = height
        self.priority = priority




class Collisions:
    tracked_objects_by_id = {}

    @classmethod
    def register(cls, obj):
        cls.tracked_objects_by_id[id(obj)] = obj

    @classmethod
    def handle_collisions(cls):

        for obj in cls.tracked_objects_by_id.values():
            draw_list: _DrawList = imgui.get_overlay_draw_list()
            # draw_list.add_rect(upper_left_x=obj.abs_left, upper_left_y=obj.abs_top,
            #                      lower_right_x=obj.abs_left + obj.width, lower_right_y=obj.abs_top + obj.height,
            #                      col=pack_color(1, 0, 0, 0.5), thickness=1.0)




    @classmethod
    def resolve_collisions(cls, box=None,
                           prev_x: float = None, prev_y: float = None):
        """
        Check for collisions and cascade pushes through multiple windows.
        Uses iterative approach to avoid deep recursion.
        """
        screen_width, screen_height = imgui.get_main_viewport().size


        window_name = id(box)

        # Stack of windows to process: (window, id, prev_x, prev_y)
        to_process = [(box, window_name, prev_x, prev_y)]
        processed = set()

        # Collect all movements
        movements = {}  # window_id: (new_x, new_y)

        max_collisions = 10  # Prevent infinite loops in case of complex collisions
        collision_count = 0
        (mouse_pos_x, mouse_pos_y) = imgui.get_mouse_pos()

        while to_process and collision_count < max_collisions:
            collision_count += 1
            current_window, current_id, current_prev_x, current_prev_y = to_process.pop(0)

            if current_window is None:  # Indicates mouse pointer collision

                curr_x = mouse_pos_x
                curr_y = mouse_pos_y

                cur_width = 10
                cur_height = 10
            else:
                # Get current window's effective position
                curr_x = movements.get(current_id, (current_window.pos_x, current_window.pos_y))[0]
                curr_y = movements.get(current_id, (current_window.pos_x, current_window.pos_y))[1]

                cur_width = current_window.width
                cur_height = current_window.height

            processed.add(current_id)

            for other in cls.tracked_objects_by_id.values():
                other_id = id(other)

                other_pos_x, other_pos_y = other.window_pos
                # Get the effective position (either from pending movement or current)
                other_x = movements.get(other_id, (other_pos_x, other_pos_y))[0]
                other_y = movements.get(other_id, (other_pos_y, other_pos_y))[1]

                # IMPORTANT: Check if there was already a collision at the ORIGINAL position
                # not the current_prev_x/y which might be from a cascaded push
                margin = 8

                # For the original moving window, use its actual previous position
                # For cascaded windows, check against their original position before ANY movement
                if current_id == window_name:
                    check_prev_x = current_prev_x
                    check_prev_y = current_prev_y
                else:
                    # Use the window's original position before any cascading
                    check_prev_x = current_window.pos_x
                    check_prev_y = current_window.pos_y

                was_colliding = (check_prev_x < other_pos_x + other.width - margin and
                                 check_prev_x + cur_width > other_pos_x + margin and
                                 check_prev_y < other_pos_y + other.height - margin and
                                 check_prev_y + cur_height > other_pos_y + margin)

                if was_colliding:
                    continue

                # Check current collision with pending positions
                is_colliding_x = (curr_x < other_x + other.width and
                                  curr_x + cur_width > other_x)
                is_colliding_y = (curr_y < other_y + other.height and
                                  curr_y + cur_height > other_y)

                if not (is_colliding_x and is_colliding_y):
                    continue

                # Determine collision axis using the check positions
                was_colliding_x = (check_prev_x < other_pos_x + other.width - margin and
                                   check_prev_x + cur_width > other_pos_x + margin)
                was_colliding_y = (check_prev_y < other_pos_y + other.height - margin and
                                   check_prev_y + cur_height > other_pos_y + margin)

                # Calculate new position
                new_x, new_y = other_x, other_y

                # Only push if this window hasn't been pushed yet
                if other_id not in movements and other_id not in processed:
                    if not was_colliding_x and is_colliding_x:
                        if curr_x > check_prev_x:  # Moving right
                            new_x = curr_x + cur_width
                        else:  # Moving left
                            new_x = curr_x - other.width
                    elif not was_colliding_y and is_colliding_y:
                        if curr_y > check_prev_y:  # Moving down
                            new_y = curr_y + cur_height
                        else:  # Moving up
                            new_y = curr_y - other.height

                    # Only store movement if position actually changed
                    if new_x != other_x or new_y != other_y:
                        movements[other_id] = (new_x, new_y)
                        # Use the other window's ORIGINAL position as its "previous"
                        to_process.append((other, other_id, other_pos_x, other_pos_y))

        # Apply all movements
        for obj_id, (new_x, new_y) in movements.items():
            new_obj = cls.tracked_objects_by_id[obj_id]

            # Constrain to screen bounds
            # Don't make dramatic changes to the position
            max_movement_x = new_obj.width / 2.0
            max_movement_y = new_obj.height / 2.0
            if (abs(new_obj.window_pos[0] - new_x) > max_movement_x or
                    abs(new_obj.window_pos[1] - new_y) > max_movement_y):
                continue

            max_additional_space_x = max_movement_x
            max_additional_space_y = max_movement_y

            new_x = max(-max_additional_space_x,
                        min(screen_width + max_additional_space_x - new_obj.width, new_x))
            new_y = max(-max_additional_space_y,
                        min(screen_height + max_additional_space_y - new_obj.height, new_y))
            new_obj.window_pos = (new_x, new_y)
