from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.utils.custom_views import generate_id


class DynamicObj(DictConversion):
    """Simplified class that mirrors driver dict keys as attributes in real-time."""

    def __init__(self, driver_dict=None):
        super().__init__()
        self.driver = None
        self._root = None

        # Store driver using super().__setattr__ to avoid recursion
        super().__setattr__('driver', driver_dict if driver_dict is not None else {})

    def __getattr__(self, name):
        if name == 'driver' or name == '_root':
            # Return driver or root directly
            return None

        # Only called when attribute doesn't exist
        # Check if it's a driver key
        driver = self._root.get(self.driver)

        if hasattr(self, 'driver') and name in driver:
            real_dict = object.__getattribute__(self, '__dict__')
            if name not in real_dict:
                # Create the attribute with None value
                return None
            else:
                return real_dict[name]

        # If not in driver, raise AttributeError
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    # has attrib check
    def __hasattr__(self, name):
        return True

    def __getattribute__(self, name):
        if name == 'driver' or name == '_root':
            # Return driver or root directly
            return super().__getattribute__(name)

        # Intercept __dict__ access
        if self.driver is not None and self._root is not None:
            driver = self._root.get(self.driver)
        else:
            driver = {}

        if name == '__dict__':
            # Get real dict without recursion
            real_dict = object.__getattribute__(self, '__dict__')

            for key in driver:
                if key not in real_dict:
                    setattr(self, key, None)


        # Only called when attribute doesn't exist
        # Check if it's a driver key

        # if name != 'driver' and name != '_root' and name in driver:
        #     # Create the attribute with None value
        #     setattr(self, name, None)
        #     return None

        # For everything else, use default behavior
        return super().__getattribute__(name)
