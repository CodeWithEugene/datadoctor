from datadoctor.checks import outlier_score

STEADY = [40, 42, 38, 41, 39, 40, 43, 37, 41, 40, 39]


def test_typo_sized_jump_is_an_outlier():
    assert outlier_score(STEADY, 400) > 3.5


def test_normal_month_is_not_an_outlier():
    assert outlier_score(STEADY, 41) < 3.5


def test_flat_history_still_catches_a_jump():
    assert outlier_score([10] * 11, 400) > 3.5


def test_flat_history_same_value_is_fine():
    assert outlier_score([10] * 11, 10) == 0


from datadoctor.checks import completeness, consistency_violations


def test_completeness_names_missing_and_late_facilities():
    result = completeness(expected={"a", "b", "c", "d"}, reported={"a", "b", "c"}, on_time={"a"})
    assert result.rate == 0.75
    assert result.timely_rate == 0.25
    assert result.missing == ["d"]
    assert result.late == ["b", "c"]


def test_completeness_ignores_reports_nobody_expected():
    result = completeness(expected={"a"}, reported={"a", "zzz"}, on_time={"a", "zzz"})
    assert result.rate == 1.0 and result.timely_rate == 1.0


def test_completeness_with_nothing_expected_is_none():
    assert completeness(expected=set(), reported=set(), on_time=set()).rate is None


def test_consistency_flags_facilities_where_rule_breaks():
    values = {("f1", "anc1"): 50, ("f1", "anc4"): 20, ("f2", "anc1"): 10, ("f2", "anc4"): 30}
    assert consistency_violations(values, [("anc1", "anc4")]) == [("f2", "anc1", "anc4", 10, 30)]


def test_consistency_treats_missing_side_as_zero():
    # Reporting ANC4 visits with no ANC1 at all is a violation; the reverse is not.
    values = {("f1", "anc4"): 5, ("f2", "anc1"): 5}
    assert consistency_violations(values, [("anc1", "anc4")]) == [("f1", "anc1", "anc4", 0, 5)]
