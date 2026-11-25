"""Contract tests: real requests/responses from the running app are validated against
openapi.yaml. If the implementation drifts from the spec (a renamed field, an
undocumented status code, a wrong content type), these fail."""

import json
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from openapi_core import Config, OpenAPI
from openapi_core.testing import MockRequest, MockResponse

from tests.conftest import FINANCE, SUPPORT

# openapi-core only parses application/json out of the box; our errors are problem+json.
SPEC = OpenAPI.from_file_path(
    str(Path(__file__).resolve().parents[2] / "openapi.yaml"),
    config=Config(extra_media_type_deserializers={"application/problem+json": json.loads}),
)


def _check(response, path_pattern: str, view_args: dict | None = None):
    req = response.request
    url = urlsplit(str(req.url))
    mock_req = MockRequest(
        "http://localhost:8080", req.method, url.path, path_pattern=path_pattern,
        args=dict(parse_qsl(url.query)), view_args=view_args or {},
        headers=dict(req.headers), data=req.content or None,
        content_type=req.headers.get("content-type", "application/json"),
    )
    mock_resp = MockResponse(response.content, status_code=response.status_code,
                             headers=dict(response.headers),
                             content_type=response.headers.get("content-type", "application/json"))
    SPEC.validate_response(mock_req, mock_resp)


def test_spec_itself_is_valid():
    assert SPEC.spec is not None


@pytest.mark.parametrize("scopes,customer", [(SUPPORT, "ACC-007"), (FINANCE, "ACC-077"), (SUPPORT, "ACC-999"),
                                              (SUPPORT, "NOPE")])
async def test_get_customer_matches_contract(client, auth, scopes, customer):
    r = await client.get(f"/v1/customers/{customer}", headers=await auth(scopes))
    _check(r, "/v1/customers/{customerId}", {"customerId": customer})


async def test_get_customer_degraded_matches_contract(client, auth, upstreams):
    upstreams.FAULTS["warehouse"] = {"mode": "error"}
    upstreams.FAULTS["casesys"] = {"mode": "error"}
    r = await client.get("/v1/customers/ACC-007", headers=await auth())
    assert r.json()["usage"] is None and r.json()["openCases"] is None
    _check(r, "/v1/customers/{customerId}", {"customerId": "ACC-007"})


@pytest.mark.parametrize("params", [
    {}, {"priority": "P1", "status": "open,in_progress", "customerTier": "Platinum", "limit": "2"},
    {"limit": "500"}, {"sort": "sideways"},
])
async def test_list_cases_matches_contract(client, auth, params):
    r = await client.get("/v1/cases", params=params, headers=await auth())
    _check(r, "/v1/cases")


async def test_unauthorized_matches_contract(client):
    _check(await client.get("/v1/cases"), "/v1/cases")


async def test_get_case_matches_contract(client, auth):
    r = await client.get("/v1/cases/CS-10240", headers=await auth())
    _check(r, "/v1/cases/{caseId}", {"caseId": "CS-10240"})


async def test_add_comment_matches_contract(client, auth):
    h = {**await auth(), "Idempotency-Key": str(uuid.uuid4())}
    for _ in range(2):  # created, then replayed
        r = await client.post("/v1/cases/CS-10231/comments", json={"body": "contract"}, headers=h)
        _check(r, "/v1/cases/{caseId}/comments", {"caseId": "CS-10231"})
    r = await client.post("/v1/cases/CS-99999/comments", json={"body": "x"},
                          headers={**h, "Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 404
    _check(r, "/v1/cases/{caseId}/comments", {"caseId": "CS-99999"})


async def test_escalate_matches_contract(client, auth):
    h = await auth()
    etag = (await client.get("/v1/cases/CS-10231", headers=h)).headers["etag"]
    for headers in ({}, {"If-Match": etag}, {"If-Match": etag}):  # 428, 200, 412
        r = await client.post("/v1/cases/CS-10231/escalate", headers={**h, **headers},
                              json={"reason": "sla_breach_risk", "note": "P1 open for a month"})
        _check(r, "/v1/cases/{caseId}/escalate", {"caseId": "CS-10231"})
