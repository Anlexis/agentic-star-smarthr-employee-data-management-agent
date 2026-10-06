# CMN-C2-281 - Unit tests: SmartHrClient service (SmartHR REST API v1 shape)
# Pure service layer (stdlib-only, no framework imports) - plain function tests.

import pytest

from src.services.smarthr_client import SmartHrApiError, SmartHrClient


def test_find_crew_success_with_injected_get():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"crews": [{"id": "crew-1", "emp_code": "1001", "last_name": "Employee 1001"}]}

    client = SmartHrClient("https://tenant.smarthr.example.test/api/v1/", get=get)
    resp = client.find_crew("1001", "tok123")
    assert resp["crews"][0]["emp_code"] == "1001"
    assert captured["url"] == "https://tenant.smarthr.example.test/api/v1/crews"
    # SmartHR REST API v1 auth: the per-call token travels as an OAuth2 Bearer.
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"]["emp_code"] == "1001"


def test_create_crew_success_with_injected_post():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"id": "crew-9002", "emp_code": "9002"}

    client = SmartHrClient("https://tenant.smarthr.example.test/api/v1", post=post)
    payload = {"emp_code": "9002", "last_name": "Alice"}
    resp = client.create_crew(payload, "tok")
    assert resp["emp_code"] == "9002"
    assert captured["url"] == "https://tenant.smarthr.example.test/api/v1/crews"
    assert captured["body"] == payload


def test_update_crew_success_with_injected_patch():
    captured = {}

    def patch(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"id": "crew-1001", "emp_code": "1001"}

    client = SmartHrClient("https://tenant.smarthr.example.test/api/v1", patch=patch)
    payload = {"emp_code": "1001", "custom_fields": [{"name": "Grade", "value": "4"}]}
    resp = client.update_crew(payload, "tok")
    assert resp["emp_code"] == "1001"
    # v1 uses emp_code as the PATCH path identifier (live adapter resolves the UUID).
    assert captured["url"] == "https://tenant.smarthr.example.test/api/v1/crews/1001"
    assert captured["body"] == payload


def test_non_2xx_raises_smarthr_api_error():
    def post(url, headers, body):
        return 400, {"errors": ["emp_code is malformed"]}

    client = SmartHrClient("https://tenant.smarthr.example.test/api/v1", post=post)
    with pytest.raises(SmartHrApiError) as exc:
        client.create_crew({"emp_code": ""}, "tok")
    assert exc.value.status_code == 400
    assert "emp_code is malformed" in str(exc.value)


def test_default_stub_transport_lookup_shape():
    # No transport injected -> deterministic, network-free v1 stub.
    client = SmartHrClient()
    assert client.uses_stub_transport is True
    resp = client.find_crew("1001", "tok")
    assert resp.get("_stub") is True
    record = resp["crews"][0]
    assert record["emp_code"] == "1001"
    assert record["last_name"] == "Employee 1001"
    assert record["id"].startswith("crew-")


def test_default_stub_transport_create_echoes_emp_code():
    client = SmartHrClient()
    resp = client.create_crew({"emp_code": "9002", "last_name": "Alice"}, "tok")
    assert resp.get("_stub") is True
    assert resp["emp_code"] == "9002"
    assert resp["id"].startswith("crew-")


def test_injected_transport_disables_stub_flag():
    client = SmartHrClient(get=lambda url, headers, body: (200, {"crews": []}))
    assert client.uses_stub_transport is False
