from datadoctor.config import load_config
from datadoctor.dhis2 import Table
from datadoctor.report import build_report, months_ending, render_html, render_text

CONFIG = load_config("config/sierra-leone-demo.toml")
ANC1, ANC4, DS = "fbfJHSPpUQD", "hfdmMSPBgLG", "QX4ZTUbOt3a"
NAMES = {"dist": "Bo", "f1": "Bucksal Clinic", "f2": "Hima <MCHP>", ANC1: "ANC 1st visit", ANC4: "ANC 4th or more visits"}


class FakeDhis2:
    """Answers the two analytics questions the report asks: reporting and values."""

    def __init__(self, months):
        self.months = months

    def get(self, path, **params):
        return {"dataSets": [{"id": DS, "name": "Reproductive Health"}, {"id": "BfMAe6Itzgt", "name": "Child Health"}]}

    def analytics(self, dimension, filter=()):
        dx = dimension[0]
        if "EXPECTED_REPORTS" in dx:
            rows = [{"dx": f"{ds}.{m}", "ou": ou, "value": 1.0}
                    for ds in CONFIG.datasets for ou in ("f1", "f2") for m in ("EXPECTED_REPORTS",)]
            rows += [{"dx": f"{ds}.ACTUAL_REPORTS", "ou": "f1", "value": 1.0} for ds in CONFIG.datasets]
            rows += [{"dx": f"{ds}.ACTUAL_REPORTS", "ou": "f2", "value": 0.0} for ds in CONFIG.datasets]
            return Table(rows, NAMES)
        rows = []
        for i, pe in enumerate(self.months):
            last = i == len(self.months) - 1
            rows.append({"dx": ANC1, "pe": pe, "ou": "f1", "value": 400.0 if last else 40.0 + i % 3})
            rows.append({"dx": ANC4, "pe": pe, "ou": "f1", "value": 20.0})
            rows.append({"dx": ANC1, "pe": pe, "ou": "f2", "value": 5.0})
            rows.append({"dx": ANC4, "pe": pe, "ou": "f2", "value": 30.0 if last else 3.0})
        return Table(rows, NAMES)


def report():
    return build_report(FakeDhis2(months_ending("202610", 12)), CONFIG, "dist", "202610")


def test_months_ending_crosses_year_boundary():
    assert months_ending("202602", 4) == ["202511", "202512", "202601", "202602"]


def test_report_finds_spike_missing_report_and_broken_rule():
    r = report()
    assert r.district == "Bo"
    assert {(o.facility, o.element, o.value) for o in r.outliers} == {
        ("Bucksal Clinic", "ANC 1st visit", 400.0),
        ("Hima <MCHP>", "ANC 4th or more visits", 30.0),
    }
    assert r.completeness[0].missing == ["Hima <MCHP>"]
    assert [(v.facility, v.more_value, v.less_value) for v in r.violations] == [("Hima <MCHP>", 5.0, 30.0)]


def test_follow_up_puts_the_most_problems_first():
    calls = report().follow_up
    assert calls[0][0] == "Hima <MCHP>"  # 2 missing reports + 1 broken rule (+ its own ANC4 spike)
    assert {name for name, _ in calls} == {"Hima <MCHP>", "Bucksal Clinic"}


def test_text_summary_is_short_and_names_who_to_call():
    text = render_text(report())
    assert "Bo" in text and "Hima <MCHP>" in text
    assert "Reproductive Health: 50% reported" in text
    assert len(text) < 1600  # fits a WhatsApp message comfortably


def test_html_escapes_facility_names():
    html = render_html(report())
    assert "Hima &lt;MCHP&gt;" in html and "Hima <MCHP>" not in html


def test_district_trend_flags_big_swings_like_for_like():
    trends = {t.element: t for t in report().trends}
    assert trends["ANC 1st visit"].ratio > 1.33  # f1 jumped from ~41 to 400
    assert "ANC 1st visit" in render_text(report())


def test_overview_links_every_district_and_shows_its_numbers():
    from datadoctor.report import render_index

    html = render_index([(report(), "202610-bo.html")], "Sierra Leone (DHIS2 demo)")
    assert 'href="202610-bo.html"' in html and ">Bo<" in html
    assert "50%" in html  # Reproductive Health completeness


def test_email_body_is_email_safe_and_links_the_full_report():
    from datadoctor.report import render_email

    html = render_email(report(), "https://reports.example.org/202610-bo.html")
    assert "<style" not in html  # many email clients drop <style>; everything is inline
    assert "Hima &lt;MCHP&gt;" in html
    assert 'href="https://reports.example.org/202610-bo.html"' in html
    assert "attached" in render_email(report(), None)


def test_follow_up_list_is_a_table_worst_first():
    html = render_html(report())
    assert '<caption id="follow-up-caption">Follow up with these facilities first (2, most issues first)</caption>' in html
    first_row = html.split('aria-labelledby="follow-up-caption"')[1].split("<tbody>")[1].split("</tr>")[0]
    assert "Hima &lt;MCHP&gt;" in first_row and "<br>" in first_row
