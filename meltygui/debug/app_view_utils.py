

def should_exclude(name, root=None):

    global_exclude = root.global_style.excluded_names

    exclude = name.startswith('_') or name.endswith('_h') or name in global_exclude or name.startswith('p_')
    return exclude
