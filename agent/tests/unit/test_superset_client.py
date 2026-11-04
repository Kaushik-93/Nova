"""Retry and auth behaviour, which can't be provoked against a healthy Superset.
Everything else is tested against the real one in tests/integration."""

import json

import httpx
import pytest

from agent.harness.errors import (ConnectionFailure, ErrorCategory, InvalidRequest, NotFound, PermissionFailure,
                                  QueryFailure, ServerFailure)
from agent.tools.superset_client import SupersetClient


class FakeSuperset:
    """Answers login and CSRF, and scripts everything else."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.logins = 0
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/security/login":
            self.logins += 1
            return httpx.Response(200, json={"access_token": f"token-{self.logins}"})
        if path == "/api/v1/security/csrf_token/":
            return httpx.Response(200, json={"result": f"csrf-{self.logins}"})
        self.calls.append(request)
        next_response = self.responses.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        return next_response


def client(fake: FakeSuperset, **options) -> SupersetClient:
    return SupersetClient("http://superset", "admin", "secret", transport=httpx.MockTransport(fake), retry_wait=0.001,
                          **options)


def ok(result=None):
    return httpx.Response(200, json={"result": result if result is not None else {}})


DATASET = {"id": 5, "table_name": "t"}


def test_sends_bearer_csrf_and_referer():
    fake = FakeSuperset(ok(DATASET))
    client(fake).get_dataset(5)
    request = fake.calls[0]
    assert request.headers["authorization"] == "Bearer token-1"
    assert request.headers["x-csrftoken"] == "csrf-1"
    assert request.headers["referer"] == "http://superset"


def test_logs_in_once_across_calls():
    fake = FakeSuperset(ok(DATASET), ok(DATASET))
    superset = client(fake)
    superset.get_dataset(5)
    superset.get_dataset(5)
    assert fake.logins == 1


def test_an_expired_token_is_refreshed_and_the_call_repeated():
    fake = FakeSuperset(httpx.Response(401, json={"msg": "Token has expired"}), ok(DATASET))
    assert client(fake).get_dataset(5).id == 5
    assert fake.logins == 2
    assert fake.calls[1].headers["authorization"] == "Bearer token-2"


def test_still_being_refused_after_login_is_a_permission_failure():
    fake = FakeSuperset(httpx.Response(401, json={"msg": "nope"}), httpx.Response(401, json={"msg": "nope"}))
    with pytest.raises(PermissionFailure) as caught:
        client(fake).get_dataset(5)
    assert caught.value.category is ErrorCategory.PERMISSION


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_reads_retry_transient_statuses(status):
    fake = FakeSuperset(httpx.Response(status, text="busy"), httpx.Response(status, text="busy"), ok(DATASET))
    assert client(fake).get_dataset(5).id == 5
    assert len(fake.calls) == 3


def test_reads_retry_connection_errors():
    fake = FakeSuperset(httpx.ConnectError("refused"), ok(DATASET))
    assert client(fake).get_dataset(5).id == 5


def test_retries_give_up_with_a_connection_failure():
    fake = FakeSuperset(*[httpx.Response(503, text="down") for _ in range(4)])
    with pytest.raises(ConnectionFailure) as caught:
        client(fake).get_dataset(5)
    assert caught.value.retryable
    assert len(fake.calls) == 4


@pytest.mark.parametrize("status, error", [(400, InvalidRequest), (403, PermissionFailure), (404, NotFound),
                                           (422, InvalidRequest), (500, ServerFailure)])
def test_permanent_failures_are_not_retried(status, error):
    fake = FakeSuperset(httpx.Response(status, json={"message": "no"}))
    with pytest.raises(error):
        client(fake).get_dataset(5)
    assert len(fake.calls) == 1


def test_a_dataset_superset_cannot_read_is_a_sql_problem_and_is_not_retried():
    fake = FakeSuperset(ok([]), httpx.Response(500, json={"message": "Fatal error"}))
    with pytest.raises(QueryFailure, match="check its SQL"):
        client(fake).ensure_dataset(key="k", name="t", sql="not sql", database_id=1)
    assert len(fake.calls) == 2


def test_a_lost_response_to_a_create_does_not_create_a_duplicate():
    """The POST succeeded but its response was lost. The retry must find the object,
    not make a second one."""
    fake = FakeSuperset(
        ok([]),                                     # lookup: nothing yet
        httpx.ReadTimeout("response lost"),         # POST: reached Superset, response lost
        ok([{"id": 9}]),                            # lookup on retry: there it is
        ok(),                                       # PUT update
        ok(),                                       # PUT refresh
        ok({"id": 9, "table_name": "t"}),           # final read
    )
    dataset = client(fake).ensure_dataset(key="k", name="t", sql="SELECT 1", database_id=1)

    assert dataset.id == 9
    assert [r.method for r in fake.calls].count("POST") == 1


def test_error_messages_never_include_the_credentials():
    fake = FakeSuperset(httpx.Response(400, json={"message": "bad"}))
    with pytest.raises(InvalidRequest) as caught:
        client(fake).get_dataset(5)
    assert "secret" not in caught.value.message


def test_a_chart_query_error_is_a_query_failure():
    fake = FakeSuperset(httpx.Response(400, json={"message": "Column not found"}))
    with pytest.raises(QueryFailure, match="Column not found"):
        client(fake).chart_data(1)


def test_guest_token_request_names_the_resource_and_rls():
    fake = FakeSuperset(httpx.Response(200, json={"token": "guest"}))
    token = client(fake).guest_token("embed-uuid", username="sam", rls=[{"dataset": 1, "clause": "x = 1"}])

    assert token == "guest"
    body = json.loads(fake.calls[0].content)
    assert body["resources"] == [{"type": "dashboard", "id": "embed-uuid"}]
    assert body["rls"] == [{"dataset": 1, "clause": "x = 1"}]
    assert body["user"] == {"username": "sam"}
