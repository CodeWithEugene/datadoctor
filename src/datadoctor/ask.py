"""Plain-language questions -> a validated DHIS2 analytics query -> a table.

Claude only *plans* the query (which data, periods, places). The plan is checked against the
instance's own metadata before anything runs, and the exact query is always shown to the user.
"""

import re
from dataclasses import dataclass
from urllib.parse import quote

import anthropic
from pydantic import BaseModel, Field

from .config import Config
from .dhis2 import Dhis2

MODEL = "claude-opus-5-5"
PERIOD = re.compile(r"^(\d{4}|\d{6}|\d{4}Q[1-4]|\d{4}W\d{1,2}|[A-Z_0-9]+)$")  # fixed or relative DHIS2 periods


class NotConfigured(ValueError):
    """No Anthropic credentials on this machine or server."""


NOT_CONFIGURED = "Questions aren't set up on this server yet (no Anthropic API key)."


class QueryPlan(BaseModel):
    data: list[str] = Field(description="uids of data elements or indicators from the catalog")
    periods: list[str] = Field(description="DHIS2 periods: YYYYMM, YYYY, YYYYQn, or relative like LAST_6_MONTHS")
    org_units: list[str] = Field(description="uids of organisation units from the catalog")
    org_unit_level: int | None = Field(description="break results down to this level below the org units, or null")
    explanation: str = Field(description="one plain sentence saying what this query shows")


@dataclass
class Catalog:
    data: dict[str, str]  # uid -> name: aggregate data elements and indicators
    org_units: dict[str, str]  # uid -> name: national and district units


@dataclass
class Answer:
    explanation: str
    headers: list[str]
    rows: list[list[str]]  # display names and formatted values, sorted
    query_url: str

    @property
    def table_text(self) -> str:
        if not self.rows:
            return "No data for that question."
        lines = [self.headers] + self.rows
        widths = [max(len(line[i]) for line in lines) for i in range(len(self.headers))]
        return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip() for line in lines)


def load_catalog(dhis2: Dhis2, config: Config) -> Catalog:
    data = {}
    for kind, extra in (("dataElements", {"filter": "domainType:eq:AGGREGATE"}), ("indicators", {})):
        items = dhis2.get(kind, fields="id,displayName", paging="false", **extra)[kind]
        data.update({i["id"]: i["displayName"] for i in items})
    units = dhis2.get("organisationUnits", filter=f"level:le:{config.district_level}", fields="id,displayName",
                      paging="false")["organisationUnits"]
    return Catalog(data, {u["id"]: u["displayName"] for u in units})


def validate(plan: QueryPlan, catalog: Catalog) -> None:
    """Trust boundary: the plan comes from a model, so every value must exist in this instance."""
    if not plan.data or not plan.periods or not plan.org_units:
        raise ValueError("The question needs at least one data item, period and place.")
    unknown = [u for u in plan.data if u not in catalog.data] + [u for u in plan.org_units if u not in catalog.org_units]
    if unknown:
        raise ValueError(f"Query referred to things not in this DHIS2 instance: {unknown}")
    bad = [p for p in plan.periods if not PERIOD.match(p)]
    if bad:
        raise ValueError(f"Not valid DHIS2 periods: {bad}")
    if plan.org_unit_level is not None and not 1 <= plan.org_unit_level <= 8:
        raise ValueError(f"Not a valid organisation unit level: {plan.org_unit_level}")


def to_dimensions(plan: QueryPlan) -> list[str]:
    ou = ";".join(plan.org_units) + (f";LEVEL-{plan.org_unit_level}" if plan.org_unit_level else "")
    return [f"dx:{';'.join(plan.data)}", f"pe:{';'.join(plan.periods)}", f"ou:{ou}"]


def plan_query(question: str, catalog: Catalog) -> QueryPlan:
    catalog_text = "\n".join(
        ["DATA (uid: name):"] + [f"{k}: {v}" for k, v in sorted(catalog.data.items(), key=lambda kv: kv[1])]
        + ["", "PLACES (uid: name):"] + [f"{k}: {v}" for k, v in sorted(catalog.org_units.items(), key=lambda kv: kv[1])]
    )
    system = [
        {"type": "text", "text": (
            "You turn a health worker's question into a DHIS2 analytics query plan. "
            "Use only uids from the catalog below. Prefer indicators for rates and coverage, data elements for counts. "
            "For 'which facilities' or 'by district' questions, set org_unit_level (2 = district, 4 = facility). "
            "Default to LAST_12_MONTHS when no time is given. Keep the explanation to one plain sentence.")},
        # The catalog is large and identical for every question on an instance, so cache it.
        {"type": "text", "text": catalog_text, "cache_control": {"type": "ephemeral"}},
    ]
    try:
        response = _call_claude(system, question)
    except anthropic.CredentialsError as e:
        raise NotConfigured(NOT_CONFIGURED) from e
    except TypeError as e:  # how the SDK reports "no credentials at all" (e.g. on a fresh server)
        if "authentication method" not in str(e):
            raise
        raise NotConfigured(NOT_CONFIGURED) from e
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise ValueError(f"Could not plan a query for that question (stop reason: {response.stop_reason}).")
    return response.parsed_output


def _call_claude(system: list[dict], question: str):
    return anthropic.Anthropic().beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": question}],
        output_format=QueryPlan,
        output_config={"effort": "low"},  # mapping words to uids is a lookup, not deep reasoning
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )


def _named_rows(rows: list[dict], names: dict[str, str]) -> tuple[list[str], list[list[str]]]:
    if not rows:
        return [], []
    cols = [c for c in ("dx", "ou", "pe") if c in rows[0]]
    label = {"dx": "Data", "ou": "Place", "pe": "Period"}

    def sort_key(r):  # names alphabetically, periods chronologically (their uids sort by date)
        return [r[c] if c == "pe" else names.get(r[c], r[c]) for c in cols]

    body = [[names.get(r[c], r[c]) for c in cols] + [f"{r['value']:g}"] for r in sorted(rows, key=sort_key)]
    return [label[c] for c in cols] + ["Value"], body


def ask(dhis2: Dhis2, config: Config, question: str, catalog: Catalog | None = None, planner=plan_query) -> Answer:
    catalog = catalog or load_catalog(dhis2, config)
    plan = planner(question, catalog)
    validate(plan, catalog)
    dims = to_dimensions(plan)
    table = dhis2.analytics(dimension=dims)
    url = f"{dhis2.base_url}/api/analytics.json?" + "&".join(f"dimension={quote(d, safe=':;-_')}" for d in dims)
    return Answer(plan.explanation, *_named_rows(table.rows, table.names), url)
