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


def client(password=None, username="admin"):
    app = create_app(FakeDhis2(), CONFIG, password=password, username=username, builder=fake_report, asker=fake_ask)
    return TestClient(app, follow_redirects=False)


def auth(password, username="admin"):
    return {"Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()}


def logged_in(password="s3cret"):
    c = client(password=password)
    assert c.post("/login", data={"username": "admin", "password": password}).status_code == 303
    return c


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


def test_visitors_are_sent_to_the_login_screen_and_back_after_signing_in():
    c = client(password="s3cret")
    r = c.get("/district/O6uvpzGd5pu?period=202610")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/district/O6uvpzGd5pu%3Fperiod%3D202610"
    page = c.get("/login").text
    assert '<input type="text" id="username" name="username"' in page and 'type="password"' in page
    r = c.post("/login?next=/district/O6uvpzGd5pu%3Fperiod%3D202610", data={"username": "admin", "password": "s3cret"})
    assert r.status_code == 303 and r.headers["location"] == "/district/O6uvpzGd5pu?period=202610"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert c.get("/district/O6uvpzGd5pu?period=202610").status_code == 200


@pytest.mark.parametrize("username, password", [("admin", "wrong"), ("root", "s3cret"), ("", "")])
def test_wrong_username_or_password_shows_an_error_on_the_login_screen(username, password):
    r = client(password="s3cret").post("/login", data={"username": username, "password": password})
    assert r.status_code == 401 and "Wrong username or password" in r.text and "set-cookie" not in r.headers


def test_login_never_redirects_to_another_site():
    r = client(password="s3cret").post("/login?next=//evil.example/x", data={"username": "admin", "password": "s3cret"})
    assert r.headers["location"] == "/"


def test_log_out_ends_the_session():
    c = logged_in()
    assert "Log out" in c.get("/").text
    assert c.post("/logout").status_code == 303
    assert c.get("/").status_code == 303


def test_scripts_can_still_use_basic_auth_and_health_check_is_open():
    c = client(password="s3cret")
    assert c.get("/", headers=auth("s3cret")).status_code == 200
    assert c.get("/", headers=auth("s3cret", username="root")).status_code == 303
    assert c.get("/healthz").status_code == 200


def test_a_forged_session_cookie_is_refused():
    c = client(password="s3cret")
    c.cookies.set("dd_session", "admin.0000")
    assert c.get("/").status_code == 303


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


def test_login_screen_shows_the_logo():
    page = client(password="s3cret").get("/login").text
    login = page.split('<div class="login">')[1]
    assert login.startswith('<svg') and 'aria-hidden="true"' in login.split(">")[0]
    assert "<title>" not in login.split("</svg>")[0]  # decorative here: the heading already names the app


def test_csp_allows_only_the_inline_favicon_image():
    csp = client().get("/healthz").headers["content-security-policy"]
    assert "img-src data:" in csp and "default-src 'none'" in csp
