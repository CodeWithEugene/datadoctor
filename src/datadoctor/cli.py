"""Command line: `datadoctor report` and `datadoctor ask`."""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from .config import load_config
from .dhis2 import Dhis2, Dhis2Error
from .deliver import Sender, load_contacts
from .report import build_report, period_label, render_email, render_html, render_index, render_text

# The public DHIS2 demo and its published demo login. Override with env vars for a real instance.
DEMO_URL = "https://play.im.dhis2.org/stable-2-42-7-1"


def last_complete_month(today: date | None = None) -> str:
    today = today or date.today()
    y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return f"{y}{m:02d}"


def connect() -> Dhis2:
    return Dhis2(
        os.environ.get("DHIS2_URL", DEMO_URL),
        os.environ.get("DHIS2_USER", "admin"),
        os.environ.get("DHIS2_PASSWORD", "district"),
    )


def districts(dhis2: Dhis2, level: int, wanted: list[str]) -> list[tuple[str, str]]:
    """(uid, name) for the requested districts (by name or uid, case-insensitive), or all of them."""
    units = dhis2.get("organisationUnits", filter=f"level:eq:{level}", fields="id,name", paging="false")
    units = sorted(((u["id"], u["name"]) for u in units["organisationUnits"]), key=lambda u: u[1])
    if not wanted:
        return units
    keys = {w.lower() for w in wanted}
    found = [(uid, name) for uid, name in units if uid.lower() in keys or name.lower() in keys]
    unknown = keys - {k.lower() for u in found for k in u}
    if unknown:
        raise SystemExit(f"Unknown district(s): {', '.join(sorted(unknown))}. Known: {', '.join(n for _, n in units)}")
    return found


def cmd_report(args) -> None:
    config = load_config(args.config)
    dhis2 = connect()
    period = args.period or last_complete_month()
    contacts = load_contacts(args.send) if args.send else {}  # validate before any DHIS2 work
    sender = Sender.from_env() if args.send else None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    done, failures = [], []
    for uid, name in districts(dhis2, config.district_level, args.district):
        report = build_report(dhis2, config, uid, period)
        path = out / f"{period}-{name.lower().replace(' ', '-')}.html"
        html, text = render_html(report), render_text(report)
        path.write_text(html, encoding="utf-8")
        done.append((report, path.name))
        print(text, f"\nFull report: {path}\n", sep="\n")
        if sender:
            people = contacts.get(name.lower()) or contacts.get(uid.lower()) or []
            if not people:
                print(f"(no contacts for {name}; not sent)\n")
            # Optional: where the HTML reports are published (e.g. GitHub Pages), so emails can link to them.
            base = os.environ.get("DATADOCTOR_REPORT_URL")
            url = f"{base.rstrip('/')}/{path.name}" if base else None
            failures += sender.send(people, f"DataDoctor: {name}, {period_label(period)}", text,
                                    render_email(report, url), attachment=(path.name, html))
    index = out / "index.html"
    index.write_text(render_index(done, config.name), encoding="utf-8")
    print(f"Overview: {index}")
    if failures:
        sys.exit("Some messages were not sent:\n" + "\n".join(failures))


def cmd_ask(args) -> None:
    import anthropic

    from .ask import ask

    config = load_config(args.config)
    try:
        answer = ask(connect(), config, args.question)
    except anthropic.AnthropicError as e:
        sys.exit(f"Error talking to Claude: {e}\nSet ANTHROPIC_API_KEY (or run `ant auth login`) to use `ask`.")
    print(answer.explanation, "\n")
    print(answer.table_text)
    print(f"\nQuery: {answer.query_url}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="datadoctor", description="Finds data quality issues in DHIS2 and tells each district which facilities to follow up with.")
    parser.add_argument("--config", default="config/sierra-leone-demo.toml", help="instance settings (TOML)")
    sub = parser.add_subparsers(required=True)

    rep = sub.add_parser("report", help="monthly data quality report per district")
    rep.add_argument("district", nargs="*", help="district names or uids (default: all)")
    rep.add_argument("--period", help="month as YYYYMM (default: last complete month)")
    rep.add_argument("--out", default="reports", help="folder for the HTML reports")
    rep.add_argument("--send", metavar="CONTACTS", help="contacts TOML; email/WhatsApp each district its report")
    rep.set_defaults(func=cmd_report)

    ask = sub.add_parser("ask", help="ask a question about the data in plain language")
    ask.add_argument("question")
    ask.set_defaults(func=cmd_ask)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (Dhis2Error, ValueError, FileNotFoundError) as e:
        sys.exit(f"Error: {e}")
