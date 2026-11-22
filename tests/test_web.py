import base64

import anthropic

import pytest
from starlette.testclient import TestClient

from datadoctor.ask import Answer
from datadoctor.config import load_config
from datadoctor.report import Report
from datadoctor.web import create_app

CONFIG = load_config("config/sierra-leone-demo.toml")
DISTRICTS = {"O6uvpzGd5pu": "Bo", "Vth0fbpFcsO": "Kono"}


class FakeDhis2:
    def get(self, path, **params):
        return {"organisationUnits": [{"id": k, "name": v} for k, v in DISTRICTS.items()]}


def fake_report(dhis2, config, uid, period):
    return Report(DISTRICTS[uid], period, [], [], [], [], [("Bucksal Clinic", ["no report"])])


def fake_ask(dhis2, config, question):
    if "unconfigured" in question:
        from datadoctor.ask import NOT_CONFIGURED, NotConfigured
        raise NotConfigured(NOT_CONFIGURED)
    if "no key" in question:
        raise anthropic.CredentialsError("Credentials file not found at /home/secret/path")
    if "fail" in question:
        raise ValueError("Query referred to things not in this DHIS2 instance")
    return Answer("ANC 1st visits by district", ["Place", "Value"], [["Bo", "812"]], "https://dhis/api/analytics.json?x")


def client(password=None):
    app = create_app(FakeDhis2(), CONFIG, password=password, builder=fake_report, asker=fake_ask)
    return TestClient(app)


def auth(password):
    return {"Authorization": "Basic " + base64.b64encode(f"anyone:{password}".encode()).decode()}


def test_overview_lists_every_district_with_a_link():
    page = client().get("/?period=2026-10").text
    assert 'href="district/O6uvpzGd5pu?period=202610"' in page
    assert "Kono" in page and "October 2026" in page


def test_district_page_shows_its_report():
    page = client().get("/district/O6uvpzGd5pu?period=202610").text
    assert "Bo: data quality, October 2026" in page and "Bucksal Clinic" in page


@pytest.mark.parametrize("url, status", [
    ("/district/notADistrict?period=202610", 404),
    ("/district/O6uvpzGd5pu?period=2026-13", 400),
    ("/?period=yesterday", 400),
])
def test_bad_input_is_refused(url, status):
    assert client().get(url).status_code == status


def test_password_protects_everything_but_the_health_check():
    c = client(password="s3cret")
    assert c.get("/").status_code == 401
    assert c.get("/", headers=auth("wrong")).status_code == 401
    assert c.get("/", headers=auth("s3cret")).status_code == 200
    assert c.get("/healthz").status_code == 200


def test_ask_shows_the_answer_and_the_exact_query():
    page = client().post("/ask", data={"q": "ANC1 by district"}, headers={"Sec-Fetch-Site": "same-origin"}).text
    assert "ANC 1st visits by district" in page and "<td" in page and "812" in page
    assert "https://dhis/api/analytics.json?x" in page


def test_ask_errors_are_shown_not_crashed():
    page = client().post("/ask", data={"q": "please fail"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert page.status_code == 422 and "not in this DHIS2 instance" in page.text


def test_ask_refuses_cross_site_posts():
    # A logged-in browser auto-sends Basic auth; another site must not be able to spend your API credits.
    r = client().post("/ask", data={"q": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_claude_setup_errors_are_friendly_and_leak_nothing():
    r = client().post("/ask", data={"q": "no key"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 503 and "aren't set up" in r.text and "/home/secret" not in r.text


def test_server_without_claude_key_says_so_with_503():
    r = client().post("/ask", data={"q": "unconfigured"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 503 and "aren't set up" in r.text
