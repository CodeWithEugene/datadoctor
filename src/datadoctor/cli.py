"""Command line: `datadoctor report` and `datadoctor ask`."""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from .config import load_config
from .dhis2 import Dhis2, Dhis2Error
from .report import build_report, render_html, render_text

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
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for uid, name in districts(dhis2, config.district_level, args.district):
        report = build_report(dhis2, config, uid, period)
        path = out / f"{period}-{name.lower().replace(' ', '-')}.html"
        path.write_text(render_html(report), encoding="utf-8")
        print(render_text(report), f"\nFull report: {path}\n", sep="\n")


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
    parser = argparse.ArgumentParser(prog="datadoctor", description="Find bad health data in DHIS2 and say what to fix.")
    parser.add_argument("--config", default="config/sierra-leone-demo.toml", help="instance settings (TOML)")
    sub = parser.add_subparsers(required=True)

    rep = sub.add_parser("report", help="monthly data quality report per district")
    rep.add_argument("district", nargs="*", help="district names or uids (default: all)")
    rep.add_argument("--period", help="month as YYYYMM (default: last complete month)")
    rep.add_argument("--out", default="reports", help="folder for the HTML reports")
    rep.set_defaults(func=cmd_report)

    ask = sub.add_parser("ask", help="ask a question about the data in plain language")
    ask.add_argument("question")
    ask.set_defaults(func=cmd_ask)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (Dhis2Error, ValueError, FileNotFoundError) as e:
        sys.exit(f"Error: {e}")
