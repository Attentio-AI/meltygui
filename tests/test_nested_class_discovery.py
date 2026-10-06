from meltygui.core.conversion.dict_conversion_util import find_nested_classes


def test_class_cycles_stop_without_losing_other_alias_paths():
    class Root:
        class Child:
            pass
    Root.Child.parent = Root
    Root.alias = Root.Child
    assert find_nested_classes(Root, 'Root') == [
        ('Root.Child', Root.Child), ('Root.alias', Root.Child)]


def test_standard_library_class_cycles():
    from urllib.parse import DefragResult
    found = find_nested_classes(DefragResult, 'urllib.parse.DefragResult')
    assert len(found) < 10
