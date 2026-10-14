import pickle
from unittest.mock import patch

import pytest

from meltygui.core.rendering.render_funcs import RenderFuncs


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_pickle_restores_original_render_func(protocol):
    selected = RenderFuncs.draw_text
    # A resolved wrapper may cache an unpicklable closure. Save only its reference.
    with patch.object(selected, "_fn", lambda: None):
        contents = pickle.dumps({"selected_render_func": selected}, protocol=protocol)
    restored = pickle.loads(contents)
    assert restored["selected_render_func"] is selected
