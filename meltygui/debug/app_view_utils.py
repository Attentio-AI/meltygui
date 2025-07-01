

def should_exclude(name):
    exclude = name.startswith('_') or name.endswith('_h') or name.startswith('p_') or name in ['id', 'name', 'tint',
                                                                                               'hash', 'type',
                                                                                               'is_root',
                                                                                               'view_settings',
                                                                                               'expanded',
                                                                                               'content_size',
                                                                                               'content_pos', 'visible',
                                                                                               'enabled', 'label_indent',
                                                                                               'child_dict_expanded']
    return exclude
