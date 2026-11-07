"""Cost selection uses reuse and independent clocks, never body command count alone."""
from dataclasses import replace
from meltygui.core.rendering.gui_cache_policy import RasterPolicy
from meltygui.core.rendering.gui_calibration import DEFAULT_COSTS, Estimate, Terms
from meltygui.core.runtime.toggles import Toggles


def decisions(*, commands=1, payload=104, dirty=False, actual=None, costs=DEFAULT_COSTS, covered=False):
    policy = RasterPolicy()
    raster = False
    history = []
    for frame in range(1, 16):
        selected = policy.choose((1, frame if dirty else 1, frame, 1 if dirty else frame, payload, commands),
            costs, actual, pixels=4096, raster=raster, covered=covered)
        if selected != raster: policy.applied(1,selected)
        raster=selected
        history.append(raster)
    return policy, history


def test_default_global_and_cheap_or_continuously_dirty_content():
    assert Toggles.Rust.auto_cache is True
    assert not any(decisions()[1])
    assert not any(decisions(commands=300, dirty=True)[1])
    assert not any(decisions(actual=Estimate(1000, 2_000_000, True, True), dirty=True)[1])


def test_actual_single_command_gpu_cost_can_promote_with_hysteresis():
    policy, history = decisions(actual=Estimate(1000, 2_000_000, True, True))
    assert history[:2] == [False, False]
    assert history[-1]
    assert policy.promotions == 1
    assert not any(decisions(actual=Estimate(1000, 2_000_000, True, True), covered=True)[1])


def test_calibrated_costs_can_change_selection_without_summing_clocks():
    # A cheap measured composite can make reusable moderate work worthwhile.
    calibrated = replace(DEFAULT_COSTS, operations=tuple(replace(cost,
        cpu=Terms(100., 0., 6), gpu=Terms(100., 0., 6)) for cost in DEFAULT_COSTS.operations), revision=1)
    actual = Estimate(3000., None, True, False)
    assert not any(decisions(actual=actual)[1])
    assert decisions(actual=actual, costs=calibrated)[1][-1]


def test_selected_view_demotes_when_it_becomes_continuously_dirty():
    policy, history = decisions(commands=300)
    assert history[-1]
    raster=True
    for frame in range(16,30):
        selected=policy.choose((1,frame-14,frame,1,104,300),DEFAULT_COSTS,None,
            pixels=4096,raster=raster)
        if selected != raster: policy.applied(1,selected)
        raster=selected
    assert not raster and policy.demotions == 1


def test_gpu_saving_cannot_hide_overwhelming_cpu_cost():
    expensive_cpu=replace(DEFAULT_COSTS,operations=tuple(replace(cost,
        cpu=Terms(50_000_000.,0.,6),gpu=Terms(100.,0.,6)) for cost in DEFAULT_COSTS.operations))
    assert not any(decisions(actual=Estimate(1000.,2_000_000.,True,True),costs=expensive_cpu)[1])
