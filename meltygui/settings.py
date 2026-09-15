"""Runtime settings access independent of any settings-search UI."""
def toggle_setting(path):
    from meltygui.extensions import get
    provider = get('toggle_setting')
    if provider is not None:
        return provider(path)
    from meltygui.toggles import Toggles
    owner = Toggles
    parts = path.split('.')
    for part in parts[:-1]:
        owner = getattr(owner, part)
    value = not getattr(owner, parts[-1])
    setattr(owner, parts[-1], value)
    return value
