from meltygui.core.styling.global_style import GlobalStyle


def should_exclude(name, root=None):

    global_exclude = GlobalStyle.excluded_names

    exclude = name.startswith('_') or name.endswith('_h') or name in global_exclude or name.startswith('p_')
    return exclude
