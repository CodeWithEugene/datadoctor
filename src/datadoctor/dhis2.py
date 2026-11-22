"""Thin adapter over the DHIS2 Web API. Same API on every DHIS2 instance (2.38+)."""

from dataclasses import dataclass

import httpx


class Dhis2Error(RuntimeError):
    pass


@dataclass
class Table:
    rows: list[dict]  # one dict per row, keyed by header name; "value" is a float
    names: dict[str, str]  # uid -> display name, for every uid in the response


class Dhis2:
    def __init__(self, base_url: str, user: str, password: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/",
            auth=(user, password),
            timeout=120,  # analytics on big instances is slow
            transport=transport,
        )

    def get(self, path: str, **params) -> dict:
        try:
            response = self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise Dhis2Error(f"Could not reach DHIS2: {e}") from e
        if response.is_error:
            raise Dhis2Error(f"DHIS2 returned {response.status_code} for {path}: {response.text[:300]}")
        return response.json()

    def analytics(self, dimension: list[str], filter: list[str] = ()) -> Table:
        data = self.get("analytics.json", dimension=dimension, filter=list(filter))
        headers = [h["name"] for h in data["headers"]]
        rows = []
        for raw in data.get("rows", []):
            row = dict(zip(headers, raw))
            row["value"] = float(row["value"])
            rows.append(row)
        names = {uid: item["name"] for uid, item in data.get("metaData", {}).get("items", {}).items()}
        return Table(rows, names)
