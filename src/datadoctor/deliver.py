"""Send each district's report to the people who act on it: email (SMTP) and WhatsApp (Meta Cloud API).

Contacts live in their own TOML file, kept out of git because it holds personal data:

    [[Bo]]
    name = "District Health Information Officer, Bo"
    email = "dhio.bo@example.org"
    whatsapp = "+23276000000"
"""

import os
import re
import smtplib
import tomllib
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE = re.compile(r"^\+?\d{6,15}$")  # E.164 digits


@dataclass(frozen=True)
class Contact:
    name: str
    email: str | None
    whatsapp: str | None


def load_contacts(path: str) -> dict[str, list[Contact]]:
    """District name (lower-case) -> contacts. Fails on the first bad entry so nothing is half-sent."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    out = {}
    for district, people in raw.items():
        contacts = []
        for p in people:
            c = Contact(p.get("name", "?"), p.get("email"), p.get("whatsapp"))
            if not (c.email or c.whatsapp):
                raise ValueError(f"{path}: {district}/{c.name} has no email or whatsapp")
            if c.email and not EMAIL.match(c.email):
                raise ValueError(f"{path}: {district}/{c.name} has an invalid email: {c.email!r}")
            if c.whatsapp and not PHONE.match(c.whatsapp.replace(" ", "")):
                raise ValueError(f"{path}: {district}/{c.name} has an invalid WhatsApp number: {c.whatsapp!r}")
            contacts.append(c)
        out[district.lower()] = contacts
    return out


class Sender:
    def __init__(self, smtp: dict | None, whatsapp: dict | None, smtp_factory=smtplib.SMTP,
                 smtp_ssl_factory=smtplib.SMTP_SSL, http: httpx.Client | None = None):
        if smtp:
            # 465 = implicit TLS; 587 = STARTTLS; "none" only for an internal relay or a local test server.
            smtp = {**smtp, "security": smtp.get("security") or ("ssl" if smtp["port"] == 465 else "starttls")}
            if smtp["security"] not in ("ssl", "starttls", "none"):
                raise ValueError(f"SMTP_SECURITY must be ssl, starttls or none, not {smtp['security']!r}")
        self.smtp, self.whatsapp = smtp, whatsapp
        self._smtp_factory, self._smtp_ssl_factory = smtp_factory, smtp_ssl_factory
        self._http = http or httpx.Client(timeout=30)

    @classmethod
    def from_env(cls) -> "Sender":
        env = os.environ.get
        smtp = None
        if env("SMTP_HOST"):
            smtp = {"host": env("SMTP_HOST"), "port": int(env("SMTP_PORT", "587")), "user": env("SMTP_USER"),
                    "password": env("SMTP_PASSWORD"), "from": env("SMTP_FROM", env("SMTP_USER", "")),
                    "security": env("SMTP_SECURITY")}
        whatsapp = None
        if env("WHATSAPP_TOKEN"):
            whatsapp = {"token": env("WHATSAPP_TOKEN"), "phone_id": env("WHATSAPP_PHONE_ID"),
                        "version": env("WHATSAPP_API_VERSION", "v23.0")}
        return cls(smtp, whatsapp)

    def send(self, contacts: list[Contact], subject: str, text: str, html: str,
             attachment: tuple[str, str] | None = None) -> list[str]:
        """Send to everyone; return one line per failure instead of stopping at the first."""
        failures = []
        for c in contacts:
            for address, channel in ((c.email, self._email), (c.whatsapp, self._whatsapp)):
                if not address:
                    continue
                try:
                    channel(address, subject, text, html, attachment)
                except Exception as e:  # one bad recipient must not block the rest of the district
                    failures.append(f"{c.name} ({address}): {e}")
        return failures

    def _email(self, to: str, subject: str, text: str, html: str, attachment: tuple[str, str] | None) -> None:
        if not self.smtp:
            raise RuntimeError("email not configured (set SMTP_HOST)")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.smtp["from"], to, subject
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        if attachment:
            filename, content = attachment
            msg.add_attachment(content, subtype="html", filename=filename)
        factory = self._smtp_ssl_factory if self.smtp["security"] == "ssl" else self._smtp_factory
        with factory(self.smtp["host"], self.smtp["port"], timeout=30) as server:
            if self.smtp["security"] == "starttls":
                server.starttls()
            if self.smtp.get("user"):
                server.login(self.smtp["user"], self.smtp["password"])
            server.send_message(msg)

    def _whatsapp(self, to: str, subject: str, text: str, html: str, attachment=None) -> None:
        if not self.whatsapp:
            raise RuntimeError("WhatsApp not configured (set WHATSAPP_TOKEN and WHATSAPP_PHONE_ID)")
        w = self.whatsapp
        # shortcut: free-text messages only reach people who messaged the number in the last 24h;
        # for cold monthly sends, register a Meta message template and send type "template" instead.
        response = self._http.post(
            f"https://graph.facebook.com/{w['version']}/{w['phone_id']}/messages",
            headers={"Authorization": f"Bearer {w['token']}"},
            json={"messaging_product": "whatsapp", "to": to.lstrip("+").replace(" ", ""), "type": "text",
                  "text": {"body": text}},
        )
        if response.is_error:
            raise RuntimeError(f"WhatsApp API returned {response.status_code}: {response.text[:200]}")
