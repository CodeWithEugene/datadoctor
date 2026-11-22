"""The monthly district data quality report: fetch, check, and render."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
import re
from html import escape
from importlib.resources import files
from statistics import median
from urllib.parse import quote

from .checks import completeness, consistency_violations, outlier_score, trend_ratio
from .config import Config

OUTLIER_THRESHOLD = 3.5  # modified z-score; the standard cut-off (Iglewicz & Hoaglin)
TREND_LIMIT = 0.33  # WHO DQR flags a change of more than ±33% against the usual average
MIN_HISTORY = 6  # months of history needed before we judge a value
REPORT_METRICS = ("EXPECTED_REPORTS", "ACTUAL_REPORTS", "ACTUAL_REPORTS_ON_TIME")


@dataclass(frozen=True)
class DatasetHealth:
    dataset: str  # display name
    rate: float | None
    timely_rate: float | None
    missing: list[str]  # facility names
    late: list[str]


@dataclass(frozen=True)
class Outlier:
    facility: str
    element: str
    value: float
    typical: float  # median of the previous months
    score: float


@dataclass(frozen=True)
class Violation:
    facility: str
    more: str
    less: str
    more_value: float
    less_value: float
    why: str


@dataclass(frozen=True)
class Trend:
    element: str
    value: float  # district total this month, facilities that reported only
    average: float  # same facilities, average of the previous months
    ratio: float


@dataclass
class Report:
    district: str
    period: str
    completeness: list[DatasetHealth]
    outliers: list[Outlier]
    violations: list[Violation]
    trends: list[Trend] = field(default_factory=list)
    follow_up: list[tuple[str, list[str]]] = field(default_factory=list)  # (facility, reasons), worst first


def months_ending(period: str, n: int) -> list[str]:
    """The n monthly periods (YYYYMM) ending at `period`, oldest first."""
    y, m = int(period[:4]), int(period[4:])
    out = []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def last_complete_month(today: date | None = None) -> str:
    today = today or date.today()
    y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return f"{y}{m:02d}"


def period_label(period: str) -> str:
    return date(int(period[:4]), int(period[4:]), 1).strftime("%B %Y")


def build_report(dhis2, config: Config, district: str, period: str) -> Report:
    ou = f"ou:{district};LEVEL-{config.facility_level}"

    # 1. Reporting completeness and timeliness, per dataset, for this month.
    dx = ";".join(f"{ds}.{m}" for ds in config.datasets for m in REPORT_METRICS)
    rep = dhis2.analytics(dimension=[f"dx:{dx}", ou], filter=[f"pe:{period}"])
    names = dict(rep.names)
    reported = defaultdict(set)  # "ds.METRIC" -> facility uids with value > 0
    for row in rep.rows:
        if row["value"] > 0:
            reported[row["dx"]].add(row["ou"])

    meta = dhis2.get("dataSets", filter=f"id:in:[{','.join(config.datasets)}]", fields="id,name", paging="false")
    names.update({d["id"]: d["name"] for d in meta["dataSets"]})

    def name(uid: str) -> str:
        return names.get(uid, uid)

    health = []
    for ds in config.datasets:
        c = completeness(reported[f"{ds}.EXPECTED_REPORTS"], reported[f"{ds}.ACTUAL_REPORTS"],
                         reported[f"{ds}.ACTUAL_REPORTS_ON_TIME"])
        health.append(DatasetHealth(name(ds), c.rate, c.timely_rate, [name(f) for f in c.missing], [name(f) for f in c.late]))

    # 2. Values for the last 12 months: outliers this month, and consistency rules.
    elements = list(dict.fromkeys(config.watch + [x for r in config.rules for x in (r.more, r.less)]))
    vals = dhis2.analytics(dimension=[f"dx:{';'.join(elements)}", f"pe:{';'.join(months_ending(period, 12))}", ou])
    names.update(vals.names)
    series = defaultdict(dict)  # (facility, element) -> {period: value}
    for row in vals.rows:
        series[(row["ou"], row["dx"])][row["pe"]] = row["value"]

    outliers = []
    for (fac, de), by_period in series.items():
        if de not in config.watch or period not in by_period:
            continue  # a missing report is caught by completeness, not here
        history = [v for pe, v in by_period.items() if pe != period]
        if len(history) < MIN_HISTORY:
            continue
        score = outlier_score(history, by_period[period])
        if score > OUTLIER_THRESHOLD:
            outliers.append(Outlier(name(fac), name(de), by_period[period], median(history), score))
    outliers.sort(key=lambda o: -o.score)

    current = {key: by_period[period] for key, by_period in series.items() if period in by_period}
    why = {(r.more, r.less): r.why for r in config.rules}
    violations = [
        Violation(name(fac), name(a), name(b), va, vb, why[(a, b)])
        for fac, a, b, va, vb in consistency_violations(current, [(r.more, r.less) for r in config.rules])
    ]

    # 3. District trend per watched element, like-for-like: only facilities that reported this month,
    # so a missing report shows up under completeness rather than as a fake drop.
    trends = []
    for de in config.watch:
        now, before = 0.0, defaultdict(float)
        for (fac, d), by_period in series.items():
            if d != de or period not in by_period:
                continue
            now += by_period[period]
            for pe, v in by_period.items():
                if pe != period:
                    before[pe] += v
        history = list(before.values())
        ratio = trend_ratio(history, now) if len(history) >= MIN_HISTORY else None
        if ratio is not None and abs(ratio - 1) > TREND_LIMIT:
            trends.append(Trend(name(de), now, sum(history) / len(history), ratio))
    trends.sort(key=lambda t: -abs(t.ratio - 1))

    report = Report(name(district), period, health, outliers, violations, trends)
    report.follow_up = _prioritise(report)
    return report


def _prioritise(report: Report) -> list[tuple[str, list[str]]]:
    reasons = defaultdict(list)
    for h in report.completeness:
        for f in h.missing:
            reasons[f].append(f"no {h.dataset} report")
        for f in h.late:
            reasons[f].append(f"{h.dataset} report late")
    for o in report.outliers:
        reasons[o.facility].append(f"{o.element} = {o.value:g} (usually ~{o.typical:g})")
    for v in report.violations:
        reasons[v.facility].append(f"{v.more} {v.more_value:g} < {v.less} {v.less_value:g}")
    return sorted(reasons.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def render_text(report: Report, top: int = 10) -> str:
    """Short plain-text summary, sized for WhatsApp / SMS / email preview."""
    lines = [f"DataDoctor: {report.district}, {period_label(report.period)}"]
    for h in report.completeness:
        lines.append(f"- {h.dataset}: {_pct(h.rate)} reported, {_pct(h.timely_rate)} on time")
    lines.append(f"{len(report.outliers)} unusual values, {len(report.violations)} numbers that don't add up.")
    for t in report.trends[:3]:
        lines.append(f"Trend: {t.element} {t.ratio - 1:+.0%} vs usual ({t.value:g} vs ~{t.average:.0f})")
    if report.follow_up:
        lines.append(f"Follow up with these facilities first ({min(top, len(report.follow_up))} of {len(report.follow_up)}):")
        for i, (fac, why) in enumerate(report.follow_up[:top], 1):
            lines.append(f"{i}. {fac}: {'; '.join(why[:2])}{' …' if len(why) > 2 else ''}")
    else:
        lines.append("No problems found. Nice work.")
    return "\n".join(lines)


# One logo file: the favicon on every page (inline, so it also works in static and emailed reports)
# and the mark on the sign-in page.
LOGO_SVG = re.sub(r"<title>.*?</title>\s*", "", files("datadoctor").joinpath("logo.svg").read_text())
# The favicon is the heartbeat line alone, cropped square: the "DHIS2" label is unreadable at 16px.
_ICON = re.sub(r"<text.*?</text>\s*", "", LOGO_SVG, flags=re.S).replace('viewBox="0 0 80 40" width="80" height="40"', 'viewBox="0 -11 64 64" width="64" height="64"', 1)
FAVICON = f'<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,{quote(" ".join(_ICON.split()))}">'

_STYLE = """<style>
:root { color-scheme: light dark; font-family: system-ui, sans-serif; line-height: 1.5; }
body { max-inline-size: 110rem; margin-inline: auto; padding: 1rem clamp(1rem, 4vw, 3rem); background: Canvas; color: CanvasText; }
.wrap { overflow: auto; max-block-size: min(32rem, 80vh); margin-block-end: 2rem; }
table { border-collapse: separate; border-spacing: 0; inline-size: 100%; }
caption { text-align: center; font-weight: 600; font-size: 1.1rem; padding-block: .5rem; }
th, td { text-align: start; vertical-align: top; padding: .4rem .6rem; border-block-end: 1px solid color-mix(in srgb, CanvasText 20%, transparent); }
thead th { position: sticky; inset-block-start: 0; z-index: 1; background: Canvas; }
tbody th { font-weight: 500; }
th.num, td.num { text-align: center; font-variant-numeric: tabular-nums; }
.wrap:focus-visible { outline: 2px solid AccentColor; outline-offset: 2px; }
nav { position: sticky; inset-block-start: 0; z-index: 2; background: Canvas; justify-content: center;
      padding-block: .75rem; margin-block-end: .5rem; border-block-end: 1px solid color-mix(in srgb, CanvasText 15%, transparent);
      display: flex; gap: 1.25rem; flex-wrap: wrap; }
form { display: flex; gap: .5rem; flex-wrap: wrap; align-items: end; justify-content: center; margin-block: 1rem 1.5rem; }
label { display: block; font-weight: 600; }
input, button { font: inherit; padding: .45rem .7rem; }
input[type=text] { inline-size: min(36rem, 80vw); }
h1, h2 { text-align: center; text-wrap: balance; }
.examples ul { list-style: none; padding: 0; text-align: center; }
.examples li { margin-block: .25rem; }
.lede { text-align: center; max-inline-size: 48rem; margin-inline: auto; text-wrap: pretty; }
.login { max-inline-size: 22rem; margin: 2rem auto; padding: 1.5rem; border-radius: .75rem;
         border: 1px solid color-mix(in srgb, CanvasText 20%, transparent); }
.login .logo { display: block; inline-size: 9rem; block-size: auto; margin: 0 auto 1.25rem; }
.login form { flex-direction: column; align-items: stretch; margin: 0; gap: .9rem; }
.login input { inline-size: 100%; box-sizing: border-box; }
nav .logout { margin: 0; }
nav .logout button { font: inherit; padding: 0; border: 0; background: none; color: LinkText; text-decoration: underline; cursor: pointer; }
.error { border-inline-start: 4px solid #c62828; padding: .5rem 1rem; }
</style>"""


def html_page(title: str, body: str, nav: str = "") -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><title>{escape(title)}</title>{FAVICON}{_STYLE}</head>
<body>{nav}<main><h1>{escape(title)}</h1>
{body}
</main></body></html>
"""


def html_table(key: str, caption: str, headers: list[str], rows: list[list[str]]) -> str:
    """Accessible data table. Cells are HTML (escape before passing); the first cell is the row header.
    Number cells (starting with a digit, +, - or n/a) are centered, and so are the headers of number columns."""
    def is_num(c: str) -> bool:
        return c[:1].isdigit() or c[:1] in "+-" and c[1:2].isdigit() or c == "n/a"

    num_cols = {i for i, c in enumerate(rows[0]) if i and is_num(c)} if rows else set()
    head = "".join(f'<th scope="col"{" class=num" if i in num_cols else ""}>{escape(h)}</th>' for i, h in enumerate(headers))
    body = "".join(f'<tr><th scope="row">{r[0]}</th>'
                   + "".join(f'<td class="num">{c}</td>' if is_num(c) else f"<td>{c}</td>" for c in r[1:]) + "</tr>"
                   for r in rows) \
        or f'<tr><td colspan="{len(headers)}">None this month.</td></tr>'
    # tabindex + role=region: keyboard users can scroll wide tables, and screen readers announce them by caption.
    return (f'<div class="wrap" tabindex="0" role="region" aria-labelledby="{key}-caption">'
            f'<table><caption id="{key}-caption">{escape(caption)}</caption>'
            f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")


def render_html(report: Report, nav: str = "") -> str:
    e = escape
    body = (
        html_table("follow-up", f"Follow up with these facilities first ({len(report.follow_up)}, most issues first)",
                   ["Facility", "Issues", "What to check"],
                   [[e(f), str(len(why)), "<br>".join(e(w) for w in why)] for f, why in report.follow_up])
        + html_table("reporting", "Reporting", ["Dataset", "Reported", "On time", "Missing"],
                 [[e(h.dataset), _pct(h.rate), _pct(h.timely_rate), e(", ".join(h.missing)) or "None"]
                  for h in report.completeness])
        + html_table("outliers", f"Unusual values ({len(report.outliers)})", ["Facility", "Data element", "This month", "Usual"],
                 [[e(o.facility), e(o.element), f"{o.value:g}", f"{o.typical:g}"] for o in report.outliers])
        + html_table("trends", f"District trends beyond ±33% ({len(report.trends)})",
                 ["Data element", "This month", "Usual", "Change"],
                 [[e(t.element), f"{t.value:g}", f"{t.average:.0f}", f"{t.ratio - 1:+.0%}"] for t in report.trends])
        + html_table("violations", f"Numbers that don't add up ({len(report.violations)})",
                 ["Facility", "Problem", "Should be higher", "Should be lower"],
                 [[e(v.facility), e(v.why), f"{e(v.more)}: {v.more_value:g}", f"{e(v.less)}: {v.less_value:g}"]
                  for v in report.violations])
    )
    return html_page(f"{report.district}: data quality, {period_label(report.period)}", body, nav)


def render_index(reports: list[tuple[Report, str]], instance: str, nav: str = "", before_table: str = "") -> str:
    """Overview page: one row per district, linking to its full report. Static, so it can be hosted anywhere."""
    e = escape
    if not reports:
        return html_page(f"{instance}: data quality", "<p>No reports.</p>")
    first = reports[0][0]
    headers = (["District"] + [f"{h.dataset} reported" for h in first.completeness]
               + ["Unusual values", "Don't add up", "Trends", "Facilities to follow up"])
    rows = [[f'<a href="{e(path)}">{e(r.district)}</a>'] + [_pct(h.rate) for h in r.completeness]
            + [str(len(r.outliers)), str(len(r.violations)), str(len(r.trends)), str(len(r.follow_up))]
            for r, path in reports]
    intro = ('<p class="lede">Generated by <a href="https://github.com/CodeWithEugene/datadoctor">DataDoctor</a> from '
             f"{e(instance)}. Pick a district for its full report.</p>")
    return html_page(f"{instance}: data quality, {period_label(first.period)}",
                 intro + before_table + html_table("districts", f"Districts ({len(reports)})", headers, rows), nav)


def render_email(report: Report, report_url: str | None, top: int = 15) -> str:
    """Email body. Email clients drop <style> and modern CSS, so: inline styles, plain tables, 600px wide."""
    e = escape
    td = 'style="padding:6px 8px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top"'
    th = 'style="padding:6px 8px;border-bottom:2px solid #999;text-align:left"'
    reporting = "".join(
        f"<tr><td {td}>{e(h.dataset)}</td><td {td}>{_pct(h.rate)}</td><td {td}>{_pct(h.timely_rate)}</td></tr>"
        for h in report.completeness)
    follow = "".join(f"<li style=\"margin-bottom:6px\"><strong>{e(f)}</strong>: {e('; '.join(why))}</li>"
                     for f, why in report.follow_up[:top])
    more = len(report.follow_up) - top
    trends = "".join(f"<li>{e(t.element)}: {t.ratio - 1:+.0%} vs usual ({t.value:g} vs ~{t.average:.0f})</li>"
                     for t in report.trends)
    full = (f'<a href="{e(report_url)}" style="color:#0b5cad">Open the full report</a>' if report_url
            else "The full report is attached.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{e(report.district)}</title></head>
<body style="margin:0;padding:16px;background:#ffffff;color:#1a1a1a;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5">
<div style="max-width:600px;margin:0 auto">
<h1 style="font-size:20px;margin:0 0 4px">{e(report.district)}: data quality, {period_label(report.period)}</h1>
<p style="margin:0 0 16px;color:#555">{len(report.outliers)} unusual values, {len(report.violations)} numbers that
don't add up, {len(report.follow_up)} facilities to follow up.</p>
<table style="border-collapse:collapse;width:100%;margin-bottom:16px">
<tr><th {th}>Dataset</th><th {th}>Reported</th><th {th}>On time</th></tr>{reporting}</table>
<h2 style="font-size:17px;margin:16px 0 8px">Follow up with these facilities first</h2>
<ol style="padding-left:20px;margin:0">{follow or "<li>No problems found. Nice work.</li>"}</ol>
{f'<p style="color:#555">…and {more} more in the full report.</p>' if more > 0 else ""}
{f'<h2 style="font-size:17px;margin:16px 0 8px">District trends beyond ±33%</h2><ul style="padding-left:20px;margin:0">{trends}</ul>' if trends else ""}
<p style="margin-top:20px">{full}</p>
<p style="margin-top:24px;font-size:12px;color:#777">Sent by DataDoctor. Checks follow the WHO Data Quality Review.</p>
</div></body></html>
"""
