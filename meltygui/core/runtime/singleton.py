"""Torch-free ``singleton`` decorator.

Lives in its own module so GUI code (custom_views) can use it without importing
``src.lsd.train.lsd_utils``, which pulls in torch.optim at load time.
"""


def singleton(cls):
    instances = {}

    def get_instance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]

    return get_instance
