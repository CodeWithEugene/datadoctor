import pytest

from datadoctor.ask import Catalog, QueryPlan, ask, to_dimensions, validate
from datadoctor.config import load_config
from datadoctor.dhis2 import Table

CATALOG = Catalog(
    data={"fbfJHSPpUQD": "ANC 1st visit", "Uvn6LCg7dVU": "ANC 1 Coverage"},
    org_units={"ImspTQPwCqd": "Sierra Leone", "O6uvpzGd5pu": "Bo"},
)


def plan(**overrides):
    base = dict(data=["fbfJHSPpUQD"], periods=["LAST_6_MONTHS"], org_units=["O6uvpzGd5pu"],
                org_unit_level=None, explanation="ANC1 in Bo, last 6 months")
    return QueryPlan(**(base | overrides))


def test_valid_plan_becomes_analytics_dimensions():
    dims = to_dimensions(plan(org_unit_level=4))
    assert dims == ["dx:fbfJHSPpUQD", "pe:LAST_6_MONTHS", "ou:O6uvpzGd5pu;LEVEL-4"]


@pytest.mark.parametrize("bad", [
    {"data": ["madeUpUid00"]},                 # hallucinated data element
    {"org_units": ["O6uvpzGd5pu;LEVEL-2"]},    # smuggled extra dimension syntax
    {"periods": ["202610&filter=x"]},           # not a DHIS2 period
    {"data": []},
    {"org_unit_level": 9},
])
def test_validate_rejects_anything_not_in_the_instance(bad):
    with pytest.raises(ValueError):
        validate(plan(**bad), CATALOG)


@pytest.mark.parametrize("period", ["202610", "2026", "2026Q3", "LAST_12_MONTHS", "THIS_YEAR"])
def test_validate_accepts_real_dhis2_periods(period):
    validate(plan(periods=[period]), CATALOG)


class FakeDhis2:
    base_url = "https://dhis.example.org"

    def analytics(self, dimension, filter=()):
        self.dimension = dimension
        return Table([{"dx": "fbfJHSPpUQD", "pe": "202610", "ou": "O6uvpzGd5pu", "value": 812.0}],
                     {"fbfJHSPpUQD": "ANC 1st visit", "202610": "October 2026", "O6uvpzGd5pu": "Bo"})


def test_ask_runs_the_planned_query_and_names_everything():
    fake = FakeDhis2()
    answer = ask(fake, load_config("config/sierra-leone-demo.toml"), "ANC1 in Bo?",
                 catalog=CATALOG, planner=lambda question, catalog: plan())
    assert fake.dimension[0] == "dx:fbfJHSPpUQD"
    assert "ANC 1st visit" in answer.table_text and "Bo" in answer.table_text and "812" in answer.table_text
    assert "analytics.json?dimension=dx:fbfJHSPpUQD" in answer.query_url


def test_answer_table_sorts_places_by_name_and_months_by_date():
    from datadoctor.ask import Answer, _named_rows

    rows = [{"ou": "b", "pe": p, "value": 1.0} for p in ("202610", "202608", "202609")] + [{"ou": "a", "pe": "202608", "value": 2.0}]
    names = {"a": "Bo", "b": "Kono", "202608": "August 2026", "202609": "September 2026", "202610": "October 2026"}
    text = Answer("", *_named_rows(rows, names), "").table_text
    places_months = [line.split()[:2] for line in text.splitlines()[1:]]
    assert places_months == [["Bo", "August"], ["Kono", "August"], ["Kono", "September"], ["Kono", "October"]]


def test_missing_claude_credentials_become_a_clear_not_configured_error(monkeypatch):
    import datadoctor.ask as ask_module

    def no_credentials(system, question):  # what the SDK raises on a server with no key at all
        raise TypeError('"Could not resolve authentication method. Expected one of api_key, auth_token..."')

    monkeypatch.setattr(ask_module, "_call_claude", no_credentials)
    with pytest.raises(ask_module.NotConfigured, match="aren't set up"):
        ask_module.plan_query("ANC1 by district", CATALOG)


def test_other_type_errors_are_not_hidden(monkeypatch):
    import datadoctor.ask as ask_module

    monkeypatch.setattr(ask_module, "_call_claude", lambda s, q: (_ for _ in ()).throw(TypeError("a real bug")))
    with pytest.raises(TypeError, match="a real bug"):
        ask_module.plan_query("x", CATALOG)
