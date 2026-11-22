"""`datadoctor serve`: the interactive web app (district overview, full reports, plain-language questions).

Server-rendered HTML with plain forms and no JavaScript, so it works on slow connections and old phones.
"""

import hmac
import logging
import re
import threading
import time
from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor
from html import escape
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route

from .ask import NOT_CONFIGURED, NotConfigured
from .config import Config
from .dhis2 import Dhis2Error, districts
from .report import build_report, html_page, html_table, last_complete_month, period_label, render_html, render_index

log = logging.getLogger("datadoctor")
PERIOD = re.compile(r"^(\d{4})-?(0[1-9]|1[0-2])$")  # 202610 or 2026-10 (what <input type=month> sends)
MAX_QUESTION = 500
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
EXAMPLES = [
    "ANC 1st visits by district for the last 6 months",
    "Which districts had the lowest Penta3 doses given last quarter?",
    "Measles doses given in Bo by month this year",
]


def create_app(dhis2, config: Config, password: str | None = None, *, builder=build_report, asker=None,
               cache_seconds: int = 3600, workers: int = 6) -> Starlette:
    if asker is None:
        from .ask import ask as asker
    cache: dict[tuple[str, str], tuple[float, object]] = {}
    lock = threading.Lock()
    pool = ThreadPoolExecutor(workers)  # few workers: be gentle with the DHIS2 server
    known: dict[str, str] = {}

    def district_names() -> dict[str, str]:
        if not known:
            known.update(districts(dhis2, config.district_level))
        return known

    def report_for(uid: str, period: str):
        with lock:
            hit = cache.get((uid, period))
        if hit and hit[0] > time.monotonic():
            return hit[1]
        report = builder(dhis2, config, uid, period)
        with lock:
            now = time.monotonic()
            for key in [k for k, (expires, _) in cache.items() if expires <= now]:
                del cache[key]  # keep memory bounded however many months people browse
            cache[(uid, period)] = (now + cache_seconds, report)
        return report

    def nav(request: Request) -> str:
        root = request.scope.get("root_path", "") + "/"
        return f'<nav aria-label="Main"><a href="{root}">Districts</a><a href="{root}ask">Ask a question</a></nav>'

    def period_of(request: Request) -> str:
        raw = request.query_params.get("period")
        if raw is None:
            return last_complete_month()
        match = PERIOD.match(raw)
        if not match:
            raise HTTPException(400, "period must be a month like 2026-10")
        return match[1] + match[2]

    async def overview(request: Request) -> Response:
        period = period_of(request)
        names = await run_in_threadpool(district_names)
        reports = await run_in_threadpool(lambda: list(pool.map(lambda uid: report_for(uid, period), names)))
        rows = [(r, f"district/{uid}?period={period}") for uid, r in zip(names, reports)]
        picker = (f'<form method="get"><div><label for="period">Month</label>'
                  f'<input type="month" id="period" name="period" value="{period[:4]}-{period[4:]}"></div>'
                  f"<button>Show</button></form>")
        return HTMLResponse(render_index(rows, config.name, nav(request), picker))

    async def district(request: Request) -> Response:
        uid, period = request.path_params["uid"], period_of(request)
        if uid not in await run_in_threadpool(district_names):
            raise HTTPException(404, "Unknown district")
        report = await run_in_threadpool(report_for, uid, period)
        return HTMLResponse(render_html(report, nav(request)))

    def ask_page(request: Request, question: str = "", result: str = "", status: int = 200) -> Response:
        examples = "".join(f"<li>{escape(q)}</li>" for q in EXAMPLES)
        form = (f'<form method="post" action="{request.scope.get("root_path", "")}/ask"><div>'
                f'<label for="q">Your question</label>'
                f'<input type="text" id="q" name="q" required maxlength="{MAX_QUESTION}" autocomplete="off" '
                f'value="{escape(question)}"></div><button>Ask</button></form>')
        intro = ("<p class=lede>Ask about any data in this DHIS2 instance. Claude picks the data, places and months; "
                 "DataDoctor checks them against DHIS2 and shows the exact query. Only your question and data "
                 "element names are sent to Claude, never health data. Answers take a few seconds.</p>")
        body = intro + form + result + f'<section class="examples"><h2>Examples</h2><ul>{examples}</ul></section>'
        return HTMLResponse(html_page("Ask a question", body, nav(request)), status_code=status)

    async def ask_get(request: Request) -> Response:
        return ask_page(request)

    async def ask_post(request: Request) -> Response:
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            raise HTTPException(403, "Cross-site requests are not allowed")
        question = parse_qs((await request.body()).decode(errors="replace")).get("q", [""])[0].strip()
        if not question or len(question) > MAX_QUESTION:
            return ask_page(request, question, f'<p class="error" role="alert">Ask a question of up to {MAX_QUESTION} characters.</p>', 400)
        import anthropic

        try:
            answer = await run_in_threadpool(asker, dhis2, config, question)
        except NotConfigured:
            return ask_page(request, question, f'<p class="error" role="alert">{NOT_CONFIGURED}</p>', 503)
        except (ValueError, Dhis2Error) as e:
            return ask_page(request, question, f'<p class="error" role="alert">{escape(str(e))}</p>', 422)
        except anthropic.AnthropicError as e:
            log.warning("ask failed talking to Claude: %s", e)  # details stay in the server log
            message = (NOT_CONFIGURED
                       if isinstance(e, (anthropic.CredentialsError, anthropic.AuthenticationError))
                       else "Couldn't reach Claude just now. Please try again in a minute.")
            return ask_page(request, question, f'<p class="error" role="alert">{message}</p>', 503)
        table = (html_table("answer", "Answer", answer.headers, [[escape(c) for c in r] for r in answer.rows])
                 if answer.rows else "<p>No data for that question.</p>")
        result = (f"<h2>{escape(answer.explanation)}</h2>{table}"
                  f'<p>Exact query: <a href="{escape(answer.query_url)}">{escape(answer.query_url)}</a></p>')
        return ask_page(request, question, result)

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def guard(request: Request, call_next) -> Response:
        if password and request.url.path != "/healthz" and not _authorised(request, password):
            return PlainTextResponse("Password required", 401, {"WWW-Authenticate": 'Basic realm="DataDoctor"'})
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    return Starlette(
        routes=[
            Route("/", overview),
            Route("/district/{uid}", district),
            Route("/ask", ask_get, methods=["GET"]),
            Route("/ask", ask_post, methods=["POST"]),
            Route("/healthz", healthz),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=guard)],
    )


def _authorised(request: Request, password: str) -> bool:
    """HTTP Basic auth; any username, the shared password. Serve behind HTTPS in production."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        _, _, given = b64decode(value).decode().partition(":")
    except ValueError:
        return False
    return hmac.compare_digest(given.encode(), password.encode())
