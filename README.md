# DataDoctor

**Finds data quality issues in DHIS2 and tells each district which facilities to follow up with.**

**Live demo:** <https://codewitheugene.github.io/datadoctor/>, rebuilt monthly from the public DHIS2 demo database.

DHIS2 is the health information system used by ministries of health in 75+ countries. Every month,
thousands of facilities report into it. Much of that data has quality issues (missing, late, or inconsistent),
and finding them is slow work. Meanwhile district teams wait on analysts for answers to simple questions.

DataDoctor does three things:

1. **Monthly data quality report per district.** It checks completeness, timeliness, unusual values, district
   trends, and numbers that don't add up, then ranks which facilities to follow up with first. Each district gets
   a short text version (sized for WhatsApp/SMS) and an HTML report, and there's an overview page for all districts.
2. **Delivery.** It emails and WhatsApps each district its own report, so the follow-up list reaches the people
   who act on it without anyone logging in.
3. **Ask in plain language.** For example, "ANC 1st visits by district for the last 6 months". Claude turns the
   question into a DHIS2 analytics query, DataDoctor checks every ID against your instance, runs the query, and
   shows the exact query it ran.

It works with any DHIS2 instance (2.38+), since they all share the same Web API. A new country is a new
config file, not new code.

### How it differs from the WHO Data Quality Tool

WHO's [Data Quality Tool](https://dhis2.org/who-dq/) is a DHIS2 app for analysts doing periodic data quality
reviews inside DHIS2. DataDoctor is built for the people who fix the data. Every month it pushes a ranked
follow-up list per district to the officers who make the calls, by email or WhatsApp. It also answers ad-hoc
questions in plain language. The two can be used together.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/). Out of the box, it runs against the public DHIS2 demo (Sierra Leone).

```bash
uv sync
uv run datadoctor report Bo          # one district, last complete month
uv run datadoctor report             # every district, plus reports/index.html overview
uv run datadoctor report Bo --period 202609
```

```text
DataDoctor: Bo, October 2026
- Reproductive Health: 85% reported, 85% on time
- Child Health: 84% reported, 84% on time
7 unusual values, 15 numbers that don't add up.
Follow up with these facilities first (10 of 39):
1. Bo Govt. Hosp.: no Reproductive Health report; no Child Health report
2. Bucksal Clinic: no Reproductive Health report; no Child Health report
3. Bum Kaku MCHP: ANC 4th or more visits = 15 (usually ~5.5); ANC 1st visit 5 < ANC 4th or more visits 15
...
Full report: reports/202610-bo.html
```

Plain-language questions need an Anthropic API key (`ANTHROPIC_API_KEY`, or `ant auth login`):

```bash
uv run datadoctor ask "Which districts had the most ANC 1st visits last quarter?"
```

## Send reports by email and WhatsApp

```bash
cp contacts.example.toml contacts.toml   # git-ignored: it holds personal data
export SMTP_HOST=... SMTP_USER=... SMTP_PASSWORD=... SMTP_FROM=...
export WHATSAPP_TOKEN=... WHATSAPP_PHONE_ID=...
uv run datadoctor report --send contacts.toml
```

Every contact is validated before anything is fetched or sent. One failed recipient doesn't stop the others,
and failures are listed at the end (with a non-zero exit code, so cron or CI can alert you).
WhatsApp uses Meta's Cloud API. Free-text messages only reach people who messaged your number in the
last 24 hours, so for monthly sends to new recipients, register a message template with Meta.

## Use your own DHIS2

```bash
export DHIS2_URL=https://dhis2.example.org DHIS2_USER=... DHIS2_PASSWORD=...
uv run datadoctor --config config/my-country.toml report
```

Copy [`config/sierra-leone-demo.toml`](config/sierra-leone-demo.toml) and set your org unit levels, the
datasets to track, the data elements to watch, and the consistency rules
(for example, "ANC 1st visit >= ANC 4th visit"). Use a read-only DHIS2 account.

### Self-host with Docker

To keep data in-country, run DataDoctor next to your DHIS2:

```bash
docker build -t datadoctor .
docker run --rm -e DHIS2_URL=... -e DHIS2_USER=... -e DHIS2_PASSWORD=... \
  -v "$PWD/config:/app/config" -v "$PWD/reports:/app/reports" \
  datadoctor --config config/my-country.toml report
```

Schedule it monthly with cron, or with a GitHub Actions workflow like
[`.github/workflows/demo.yml`](.github/workflows/demo.yml), which builds the live demo.

## How the checks work

These follow the WHO Data Quality Review dimensions.

| Check | Method |
|---|---|
| Completeness | Reports received ÷ reports expected, per dataset, per facility |
| Timeliness | Reports received on time ÷ reports expected |
| Unusual values | Modified z-score > 3.5 against the facility's previous 11 months (median/MAD, robust to past outliers; needs 6+ months of history) |
| District trend | District total vs the average of the previous 11 months, flagged beyond ±33%. Counts only facilities that reported this month, so missing reports aren't double-counted |
| Consistency | Configured `more >= less` rules, checked per facility |

## Code layout

| Module | Job | Tested by |
|---|---|---|
| `checks.py` | Pure data quality maths, no I/O | `tests/test_checks.py` |
| `dhis2.py` | Thin DHIS2 Web API client | `tests/test_dhis2.py` (fake HTTP transport) |
| `report.py` | Fetch → check → rank → render text, HTML, overview | `tests/test_report.py` (fake DHIS2) |
| `deliver.py` | Contacts, email (SMTP), WhatsApp (Cloud API) | `tests/test_deliver.py` (fake SMTP and HTTP) |
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

- WhatsApp message templates for business-initiated sends
- Comparison with population denominators (WHO DQR external consistency)
- Interactive web app with the `ask` box
