"""Load the real desktop GL binding only when a desktop GPU operation runs.

Resource ownership and tile bookkeeping import on native hosts too; importing
those Python definitions must not initialize PyOpenGL's platform loader.
"""
def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    from OpenGL import GL
    value = getattr(GL, name)
    # Keep the original wrapper identity: live GL error toggles and in-place
    # hotswaps still reach it, while warm draws use ordinary module lookups.
    globals()[name] = value
    return value
