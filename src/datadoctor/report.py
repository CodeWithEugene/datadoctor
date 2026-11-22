"""The monthly district data quality report: fetch, check, and render."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from html import escape
from statistics import median

from .checks import completeness, consistency_violations, outlier_score
from .config import Config

OUTLIER_THRESHOLD = 3.5  # modified z-score; the standard cut-off (Iglewicz & Hoaglin)
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


@dataclass
class Report:
    district: str
    period: str
    completeness: list[DatasetHealth]
    outliers: list[Outlier]
    violations: list[Violation]
    facilities_to_call: list[tuple[str, list[str]]] = field(default_factory=list)  # (facility, reasons), worst first


def months_ending(period: str, n: int) -> list[str]:
    """The n monthly periods (YYYYMM) ending at `period`, oldest first."""
    y, m = int(period[:4]), int(period[4:])
    out = []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


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

    report = Report(name(district), period, health, outliers, violations)
    report.facilities_to_call = _prioritise(report)
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
    if report.facilities_to_call:
        lines.append(f"Call these facilities first ({min(top, len(report.facilities_to_call))} of {len(report.facilities_to_call)}):")
        for i, (fac, why) in enumerate(report.facilities_to_call[:top], 1):
            lines.append(f"{i}. {fac}: {'; '.join(why[:2])}{' …' if len(why) > 2 else ''}")
    else:
        lines.append("No problems found. Nice work.")
    return "\n".join(lines)


def render_html(report: Report) -> str:
    e = escape
    title = f"{report.district}: data quality, {period_label(report.period)}"
    calls = "".join(f"<li><strong>{e(f)}</strong>: {e('; '.join(why))}</li>" for f, why in report.facilities_to_call)
    reporting = "".join(
        f"<tr><td>{e(h.dataset)}</td><td>{_pct(h.rate)}</td><td>{_pct(h.timely_rate)}</td>"
        f"<td>{e(', '.join(h.missing)) or '-'}</td></tr>" for h in report.completeness)
    outliers = "".join(
        f"<tr><td>{e(o.facility)}</td><td>{e(o.element)}</td><td>{o.value:g}</td><td>{o.typical:g}</td></tr>"
        for o in report.outliers)
    violations = "".join(
        f"<tr><td>{e(v.facility)}</td><td>{e(v.why)}</td><td>{e(v.more)}: {v.more_value:g}</td>"
        f"<td>{e(v.less)}: {v.less_value:g}</td></tr>" for v in report.violations)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, sans-serif; line-height: 1.5; }}
body {{ max-width: 60rem; margin: 0 auto; padding: 1rem; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid color-mix(in srgb, currentColor 20%, transparent); }}
caption {{ text-align: left; font-weight: 600; font-size: 1.1rem; padding: .5rem 0; }}
.wrap {{ overflow-x: auto; }}
</style></head><body>
<h1>{e(title)}</h1>
<h2>Call these facilities first</h2>
<ol>{calls or "<li>No problems found.</li>"}</ol>
<div class="wrap"><table><caption>Reporting</caption>
<tr><th>Dataset</th><th>Reported</th><th>On time</th><th>Missing</th></tr>{reporting}</table></div>
<div class="wrap"><table><caption>Unusual values ({len(report.outliers)})</caption>
<tr><th>Facility</th><th>Data element</th><th>This month</th><th>Usual</th></tr>{outliers}</table></div>
<div class="wrap"><table><caption>Numbers that don't add up ({len(report.violations)})</caption>
<tr><th>Facility</th><th>Problem</th><th>Should be higher</th><th>Should be lower</th></tr>{violations}</table></div>
</body></html>
"""
