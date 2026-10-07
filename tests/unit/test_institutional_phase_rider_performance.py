"""Architecture checks replacing an invalid, data-dependent profit contract.

Profitability is an empirical outcome, not a unit-test invariant. The previous
version required arbitrary win-rate/expectancy thresholds and contained
unclosed Markdown outside Python code; these checks preserve the intended
five-set research contract without asserting a profitable result.
"""

from forex_platform.fractal_engine.timeframes import TIMEFRAME_SETS


def test_five_canonical_sets_are_preserved_as_overlapping_windows() -> None:
    assert tuple(TIMEFRAME_SETS) == ("SET_1", "SET_2", "SET_3", "SET_4", "SET_5")
    assert {
        key: tuple(tf.value for tf in value)
        for key, value in TIMEFRAME_SETS.items()
    } == {
        "SET_1": ("1M", "1W", "1D"),
        "SET_2": ("1W", "1D", "4H"),
        "SET_3": ("1D", "4H", "1H"),
        "SET_4": ("4H", "1H", "15M"),
        "SET_5": ("1H", "15M", "3M"),
    }


def test_adjacent_set_roles_share_the_middle_timeframe() -> None:
    sets = {key: tuple(tf.value for tf in value) for key, value in TIMEFRAME_SETS.items()}
    assert sets["SET_1"][1:] == sets["SET_2"][:2]
    assert sets["SET_2"][1:] == sets["SET_3"][:2]
    assert sets["SET_3"][1:] == sets["SET_4"][:2]
    assert sets["SET_4"][1:] == sets["SET_5"][:2]
