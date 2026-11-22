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

    def __init__(self, host, port, timeout):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def starttls(self):
        pass

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
