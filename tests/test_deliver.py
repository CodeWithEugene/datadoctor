import json

import httpx
import pytest

from datadoctor.deliver import Contact, Sender, load_contacts


def write(tmp_path, text):
    p = tmp_path / "contacts.toml"
    p.write_text(text)
    return str(p)


def test_contacts_are_grouped_by_district_case_insensitively(tmp_path):
    contacts = load_contacts(write(tmp_path, """
[[Bo]]
name = "DHIO Bo"
email = "dhio.bo@example.org"
whatsapp = "+23276000000"
"""))
    assert contacts["bo"] == [Contact("DHIO Bo", "dhio.bo@example.org", "+23276000000")]


@pytest.mark.parametrize("bad", ['email = "not-an-email"', 'whatsapp = "call me"', 'name = "nobody"'])
def test_bad_contacts_are_rejected_up_front(tmp_path, bad):
    with pytest.raises(ValueError):
        load_contacts(write(tmp_path, f"[[Bo]]\nname = 'x'\n{bad}\n" if "name" not in bad else f"[[Bo]]\n{bad}\n"))


class FakeSMTP:
    sent = []
    tls = []

    def __init__(self, host, port, timeout):
        self.host = host
        FakeSMTP.tls.append("plain")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def starttls(self):
        FakeSMTP.tls[-1] = "starttls"

    def login(self, user, password):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_sends_email_and_whatsapp_and_keeps_going_after_a_failure():
    posted = []

    def graph(request):
        posted.append(json.loads(request.content))
        return httpx.Response(400 if posted[-1]["to"] == "999" else 200, json={})

    sender = Sender(
        smtp={"host": "smtp.example.org", "port": 587, "user": "u", "password": "p", "from": "dd@example.org"},
        whatsapp={"token": "t", "phone_id": "123", "version": "v23.0"},
        smtp_factory=FakeSMTP,
        http=httpx.Client(transport=httpx.MockTransport(graph)),
    )
    contacts = [Contact("A", "a@example.org", "254700000000"), Contact("B", None, "999")]
    failures = sender.send(contacts, subject="Bo data quality", text="Call Bucksal Clinic", html="<p>hi</p>")

    assert FakeSMTP.sent[-1]["To"] == "a@example.org" and FakeSMTP.sent[-1]["Subject"] == "Bo data quality"
    assert [p["to"] for p in posted] == ["254700000000", "999"]
    assert posted[0]["text"]["body"] == "Call Bucksal Clinic"
    assert len(failures) == 1 and "999" in failures[0]


def test_channel_without_settings_is_reported_not_crashed():
    sender = Sender(smtp=None, whatsapp=None)
    failures = sender.send([Contact("A", "a@example.org", None)], subject="s", text="t", html="h")
    assert failures == ["A (a@example.org): email not configured (set SMTP_HOST)"]


class FakeSMTPSSL(FakeSMTP):
    def __init__(self, host, port, timeout):
        super().__init__(host, port, timeout)
        FakeSMTP.tls[-1] = "ssl"


def email_sender(port=587, security=None):
    smtp = {"host": "smtp.example.org", "port": port, "user": "u", "password": "p", "from": "dd@example.org"}
    if security:
        smtp["security"] = security
    return Sender(smtp=smtp, whatsapp=None, smtp_factory=FakeSMTP, smtp_ssl_factory=FakeSMTPSSL)


def test_email_carries_text_html_and_the_full_report_attached():
    email_sender().send([Contact("A", "a@example.org", None)], subject="s", text="plain", html="<p>email</p>",
                        attachment=("202610-bo.html", "<html>full</html>"))
    msg = FakeSMTP.sent[-1]
    body = msg.get_body(preferencelist=("plain",)), msg.get_body(preferencelist=("html",))
    assert body[0].get_content().strip() == "plain"
    assert "<p>email</p>" in body[1].get_content()
    attached = [p for p in msg.iter_attachments()]
    assert [a.get_filename() for a in attached] == ["202610-bo.html"]


@pytest.mark.parametrize("port, security, expected", [
    (587, None, "starttls"),   # the usual submission port
    (465, None, "ssl"),        # implicit TLS (Gmail, many providers)
    (25, "none", "plain"),     # internal relay or local test server
])
def test_smtp_connection_security(port, security, expected):
    email_sender(port, security).send([Contact("A", "a@example.org", None)], subject="s", text="t", html="h")
    assert FakeSMTP.tls[-1] == expected


def test_unknown_smtp_security_is_rejected():
    with pytest.raises(ValueError):
        email_sender(587, "maybe").send([Contact("A", "a@example.org", None)], subject="s", text="t", html="h")
