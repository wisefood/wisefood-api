"""The console's guide Excel export, as proxied by this gateway.

The workbook is built by FoodScholar, which nothing outside the cluster can
reach, so these routes are the only way to it. They are console features: the
gate is admin/expert, the download is bytes rather than an envelope, and
FoodScholar's "not finished yet" / "expired" must arrive as 409/404, not as the
500 an unmapped upstream error becomes.
"""
import asyncio
import inspect
import sys

import httpx
import pytest

PREFIX = "/api/v1/foodscholar/guidelines/export"
URN = "urn:guide:ie-healthy-eating"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _main():
    sys.path.insert(0, "src")
    import main

    return main


def _route(method, path):
    for route in _main().api.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route
    raise AssertionError(f"{method} {path} is not registered")


ROUTES = [
    ("POST", f"{PREFIX}/{{guide_urn}}"),
    ("GET", f"{PREFIX}/{{guide_urn}}"),
    ("GET", f"{PREFIX}/{{guide_urn}}/download"),
]


@pytest.mark.parametrize("verb,path", ROUTES)
def test_the_route_the_console_calls_is_registered(verb, path):
    _route(verb, path)


@pytest.mark.parametrize("verb,path", ROUTES)
def test_only_admins_and_experts_may_export(verb, path):
    required = [
        inspect.getclosurevars(dependency.call).nonlocals.get("required")
        for dependency in _route(verb, path).dependant.dependencies
        if "required" in inspect.getclosurevars(dependency.call).nonlocals
    ]
    assert required, f"{verb} {path} has no auth gate"
    assert all(roles and set(roles) == {"admin", "expert"} for roles in required), required


@pytest.mark.parametrize("method", [
    "enqueue_guide_export", "get_guide_export_status", "download_guide_export",
])
def test_the_client_exposes_the_method(method):
    from backend.foodscholar import FOODSCHOLAR

    assert hasattr(FOODSCHOLAR, method)


def _request(path):
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""})


def _upstream(status, **kwargs):
    request = httpx.Request("GET", "http://foodscholar/api/v1/guidelines/export")
    return httpx.Response(status, request=request, **kwargs)


def test_the_download_passes_the_workbook_through(monkeypatch):
    _main()
    from backend.foodscholar import FOODSCHOLAR
    from routers import foodscholar as router

    workbook = b"PK\x03\x04 not really a workbook"

    async def fake(guide_urn):
        assert guide_urn == URN
        return _upstream(200, content=workbook, headers={
            "content-type": XLSX,
            "content-disposition": 'attachment; filename="FSKG_Platform_ie-healthy-eating.xlsx"',
        })

    monkeypatch.setattr(FOODSCHOLAR, "download_guide_export", fake)
    response = asyncio.run(router.download_guide_export(request=_request(f"{PREFIX}/{URN}/download"), guide_urn=URN))

    assert response.body == workbook
    assert response.media_type == XLSX
    assert response.headers["content-disposition"].endswith('filename="FSKG_Platform_ie-healthy-eating.xlsx"')
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("status,expected", [(400, 400), (404, 404), (409, 409)])
def test_foodscholar_refusals_keep_their_status(monkeypatch, status, expected):
    _main()
    from backend.foodscholar import FOODSCHOLAR
    from exceptions import APIException
    from routers import foodscholar as router

    async def fake(guide_urn):
        response = _upstream(status, json={"detail": "the export has expired; run it again"})
        response.raise_for_status()

    monkeypatch.setattr(FOODSCHOLAR, "download_guide_export", fake)
    with pytest.raises(APIException) as raised:
        asyncio.run(router.download_guide_export(request=_request(f"{PREFIX}/{URN}/download"), guide_urn=URN))

    assert raised.value.status_code == expected
    assert raised.value.detail == "the export has expired; run it again"


def test_the_urn_reaches_foodscholar_readable(monkeypatch):
    from backend.foodscholar import FOODSCHOLAR

    seen = {}

    async def fake_get(endpoint, params=None, **kwargs):
        seen["endpoint"] = endpoint
        return {"status": "not_found"}

    monkeypatch.setattr(FOODSCHOLAR, "get", fake_get)
    asyncio.run(FOODSCHOLAR.get_guide_export_status(URN))
    assert seen["endpoint"] == f"/api/v1/guidelines/export/{URN}"
