# DataDoctor

**Finds data quality issues in DHIS2 and tells each district which facilities to follow up with.**

DHIS2 is the health information system used by ministries of health in 75+ countries. Every month,
thousands of facilities report into it. Much of that data has quality issues (missing, late, or inconsistent), and finding them
is slow manual work. Meanwhile district teams wait on analysts for answers to simple questions.

DataDoctor does two things:

1. **Monthly data quality report per district.** It checks reporting completeness, timeliness, unusual
   values, and numbers that don't add up, then gives a ranked list of which facilities to follow up with first.
   You get a short text version (sized for WhatsApp/SMS) and an HTML report.
2. **Ask in plain language.** For example, "ANC 1st visits by district for the last 6 months". Claude turns the
   question into a DHIS2 analytics query, DataDoctor checks every ID against your instance, runs the query, and
   shows the exact query it ran.

It works with any DHIS2 instance (2.38+), since they all share the same Web API. A new country is a new
config file, not new code.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/). Out of the box, it runs against the public DHIS2 demo (Sierra Leone).

```bash
uv sync
uv run datadoctor report Bo          # one district, last complete month
uv run datadoctor report             # every district
uv run datadoctor report Bo --period 202609
```

```text
DataDoctor: Bo, October 2026
- Reproductive Health: 85% reported, 85% on time
- Child Health: 84% reported, 84% on time
7 unusual values, 15 numbers that don't add up.
Follow up with these facilities first (10 of 39):
1. Bo Govt. Hosp.: no Reproductive Health report; no Child Health report
3. Bum Kaku MCHP: ANC 4th or more visits = 15 (usually ~5.5); ANC 1st visit 5 < ANC 4th or more visits 15
...
Full report: reports/202610-bo.html
```

Plain-language questions need an Anthropic API key (`ANTHROPIC_API_KEY`, or `ant auth login`):

```bash
uv run datadoctor ask "Which districts had the most ANC 1st visits last quarter?"
```

## Use your own DHIS2

```bash
export DHIS2_URL=https://dhis2.example.org DHIS2_USER=... DHIS2_PASSWORD=...
uv run datadoctor --config config/my-country.toml report
```

Copy [`config/sierra-leone-demo.toml`](config/sierra-leone-demo.toml) and set your org unit levels, the
datasets to track, the data elements to watch for outliers, and the consistency rules
(for example, "ANC 1st visit >= ANC 4th visit").

## How the checks work

| Check | Method |
|---|---|
| Completeness | Reports received ÷ reports expected, per dataset, per facility |
| Timeliness | Reports received on time ÷ reports expected |
| Unusual values | Modified z-score > 3.5 against the facility's previous 11 months (median/MAD, robust to past outliers; needs 6+ months of history) |
| Consistency | Configured `more >= less` rules, checked per facility |

These follow the WHO Data Quality Review dimensions.

## Code layout

| Module | Job | Tested by |
|---|---|---|
| `checks.py` | Pure data quality maths, no I/O | `tests/test_checks.py` |
| `dhis2.py` | Thin DHIS2 Web API client | `tests/test_dhis2.py` (fake HTTP transport) |
| `report.py` | Fetch → check → rank → render | `tests/test_report.py` (fake DHIS2) |
| `ask.py` | Question → validated query plan → table | `tests/test_ask.py` (fake planner) |
| `config.py`, `cli.py` | Settings and command line | |

```bash
uv run pytest
```

**Safety:** Claude only *plans* the query. The plan has to pass validation against the instance's own metadata
(known data element, indicator, and org unit IDs, plus well-formed periods) before anything runs, and the exact
query URL is always shown. Health data never goes to Claude: only the question and the metadata
catalog (names and IDs) do.

## Roadmap

- Scheduled delivery to district officers (email, WhatsApp)
- Self-hosted deployment for ministries that keep data in-country
- More WHO DQR checks (trend consistency, comparison with population denominators)
- Web dashboard
