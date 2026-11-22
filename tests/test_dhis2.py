import httpx
import pytest

from datadoctor.dhis2 import Dhis2, Dhis2Error

ANALYTICS = {
    "headers": [{"name": "dx"}, {"name": "ou"}, {"name": "value"}],
    "metaData": {"items": {"f1": {"name": "Bucksal Clinic"}, "de1": {"name": "ANC 1st visit"}}},
    "rows": [["de1", "f1", "12"]],
}


def client(handler):
    return Dhis2("https://dhis.example.org", "u", "p", transport=httpx.MockTransport(handler))


def test_analytics_returns_rows_with_numeric_values_and_names():
    seen = {}

    def handler(request):
        seen["url"] = request.url
        return httpx.Response(200, json=ANALYTICS)

    table = client(handler).analytics(dimension=["dx:de1", "ou:f1"], filter=["pe:202610"])
    assert table.rows == [{"dx": "de1", "ou": "f1", "value": 12.0}]
    assert table.names["f1"] == "Bucksal Clinic"
    assert seen["url"].path == "/api/analytics.json"
    assert seen["url"].params.get_list("dimension") == ["dx:de1", "ou:f1"]


def test_bad_credentials_raise_a_readable_error():
    with pytest.raises(Dhis2Error, match="401"):
        client(lambda r: httpx.Response(401, json={"message": "Unauthorized"})).analytics(dimension=["dx:x"])
