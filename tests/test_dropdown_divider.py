"""DD_DIVIDER: a leaf value that draws as a rule and is never a choice."""
from meltygui.model.dropdown_model import DD_DIVIDER, _dd_path_for_value, _dd_visible_entries

ROWS = {"alpha": "/a", "divider:1": DD_DIVIDER, "Home": "/home", "divider:2": DD_DIVIDER, "Choose…": "@choose"}


def test_dividers_are_rows_until_a_search_filters():
    assert [row[0] for row in _dd_visible_entries(ROWS)] == list(ROWS)
    assert [row[0] for row in _dd_visible_entries(ROWS, "o")] == ["Home", "Choose…"]
    assert [row[0] for row in _dd_visible_entries(ROWS, "divider")] == []


def test_a_divider_is_a_leaf_with_no_label():
    rows = {row[0]: row for row in _dd_visible_entries(ROWS)}
    assert rows["divider:1"][3] is False and str(rows["divider:1"][1]) == ""
    assert _dd_path_for_value(ROWS, "/home") == ("Home",)
