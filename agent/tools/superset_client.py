"""A typed client for the Superset REST API.

Writes are idempotent because every object has a deterministic UUID (charts,
datasets) or slug (dashboards). `ensure_*` looks the object up first, so a retry
after a timed-out POST that actually succeeded finds that object instead of making
a second one. That is also what makes it safe to retry the whole operation.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, TypeVar

import httpx
import prison
from pydantic import BaseModel, Field
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from agent.harness.errors import (ConnectionFailure, InvalidRequest, NotFound, NovaError, PermissionFailure,
                                  QueryFailure, ServerFailure)
from agent.tools.superset_payloads import (ChartSlot, ChartSpec, SelectFilter, TimeFilter, build_chart_payload,
                                           build_json_metadata, build_positions, stable_uuid)

logger = logging.getLogger(__name__)
T = TypeVar("T")

MAX_ATTEMPTS = 4
TRANSIENT_STATUSES = {429, 502, 503, 504}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class Dataset(BaseModel):
    id: int
    uuid: str | None = None
    table_name: str
    sql: str | None = None


class Chart(BaseModel):
    id: int
    uuid: str | None = None
    slice_name: str
    viz_type: str | None = None


class Dashboard(BaseModel):
    id: int
    slug: str | None = None
    dashboard_title: str
    published: bool | None = None
    url: str | None = None


class ChartData(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    query: str | None = None
    rejected_filters: list[dict[str, Any]] = Field(default_factory=list)


def dataset_uuid(key: str) -> str:
    return str(stable_uuid("dataset", key))


def _is_retryable(error: BaseException) -> bool:
    return isinstance(error, NovaError) and error.retryable


class SupersetClient:
    """Thread-safe enough for tool calls: httpx.Client is, and a re-login only
    replaces headers with equivalent values.
    """

    def __init__(self, base_url: str, username: str, password: str, *, timeout: float = 120.0,
                 max_attempts: int = MAX_ATTEMPTS, retry_wait: float = 0.5,
                 transport: httpx.BaseTransport | None = None,
                 on_write: Callable[[str, str, int], None] | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._credentials = {"username": username, "password": password, "provider": "db", "refresh": False}
        self._http = httpx.Client(base_url=self._base_url, timeout=timeout, transport=transport)
        self._max_attempts = max_attempts
        self._retry_wait = retry_wait
        self._logged_in = False
        self._on_write = on_write  # called with (method, path, status) for the audit log

    def __enter__(self) -> "SupersetClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _retrying(self, operation: Callable[[], T]) -> T:
        for attempt in Retrying(retry=retry_if_exception(_is_retryable), stop=stop_after_attempt(self._max_attempts),
                                wait=wait_exponential_jitter(multiplier=self._retry_wait, max=8), reraise=True):
            with attempt:
                return operation()
        raise AssertionError("unreachable")

    # --- Session -------------------------------------------------------------

    def login(self) -> None:
        """Log in and fetch a CSRF token. The token is tied to the session cookie,
        which the underlying client keeps."""
        self._http.headers.pop("Authorization", None)
        self._http.headers.pop("X-CSRFToken", None)
        self._http.cookies.clear()
        response = self._send("POST", "/api/v1/security/login", json=self._credentials)
        self._raise_for_status(response)
        self._http.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        csrf = self._send("GET", "/api/v1/security/csrf_token/")
        self._raise_for_status(csrf)
        self._http.headers["X-CSRFToken"] = csrf.json()["result"]
        self._http.headers["Referer"] = self._base_url
        self._logged_in = True

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.TransportError as error:
            raise ConnectionFailure(f"could not reach Superset: {type(error).__name__}") from error

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        message = _error_message(response)
        if status in TRANSIENT_STATUSES:
            error: NovaError = ConnectionFailure(f"Superset returned {status}: {message}", status=status)
        elif status >= 500:
            error = ServerFailure(f"Superset failed with {status}: {message}", status=status)
        elif status in {401, 403}:
            error = PermissionFailure(f"Superset refused the request: {message}", status=status)
        elif status == 404:
            error = NotFound(f"Superset has no such object: {message}", status=status)
        else:
            error = InvalidRequest(f"Superset rejected the request: {message}", status=status)
        raise error

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send one request, logging in when needed and retrying what's safe to.

        Reads retry on any transient failure. Writes aren't retried here if the
        request may have reached Superset; `ensure_*` handles those by looking up
        again first.
        """

        def attempt() -> httpx.Response:
            if not self._logged_in:
                self.login()
            response = self._send(method, path, **kwargs)
            if response.status_code == 401:  # the JWT expired
                self.login()
                response = self._send(method, path, **kwargs)
            self._raise_for_status(response)
            return response

        if method in SAFE_METHODS:
            return self._retrying(attempt)
        try:
            response = attempt()
        except NovaError as error:
            if self._on_write:
                self._on_write(method, path, error.status or 0)
            raise
        if self._on_write:
            self._on_write(method, path, response.status_code)
        return response

    def _find_one(self, resource: str, column: str, value: str) -> dict[str, Any] | None:
        query = prison.dumps({"filters": [{"col": column, "opr": "eq", "value": value}], "page_size": 2})
        results = self._call("GET", f"/api/v1/{resource}/", params={"q": query}).json()["result"]
        return results[0] if results else None

    # --- Databases -----------------------------------------------------------

    def database_id(self, name: str) -> int:
        match = self._find_one("database", "database_name", name)
        if match is None:
            raise NotFound(f"no Superset database named {name!r}")
        return match["id"]

    # --- Datasets ------------------------------------------------------------

    def ensure_dataset(self, *, key: str, name: str, sql: str, database_id: int) -> Dataset:
        """Create or update a virtual dataset. `sql` must use fully qualified names."""
        uuid = dataset_uuid(key)

        def run() -> Dataset:
            existing = self._find_one("dataset", "uuid", uuid)
            if existing is None:
                created = self._call("POST", "/api/v1/dataset/", json={
                    "database": database_id, "schema": None, "table_name": name, "sql": sql, "uuid": uuid,
                })
                dataset_id = created.json()["id"]
            else:
                dataset_id = existing["id"]
                self._call("PUT", f"/api/v1/dataset/{dataset_id}", json={"sql": sql, "table_name": name},
                           params={"override_columns": "true"})
                self._call("PUT", f"/api/v1/dataset/{dataset_id}/refresh")
            return self.get_dataset(dataset_id)

        try:
            return self._retrying(run)
        except ServerFailure as error:
            # Superset reads a virtual dataset's columns by running its SQL, and
            # answers a query that fails with a bare 500.
            raise QueryFailure(f"Superset could not read this dataset's columns; check its SQL. {error.message}",
                               status=error.status) from error

    def find_dataset(self, key: str) -> Dataset | None:
        match = self._find_one("dataset", "uuid", dataset_uuid(key))
        return self.get_dataset(match["id"]) if match else None

    def get_dataset(self, dataset_id: int) -> Dataset:
        return Dataset.model_validate(self._call("GET", f"/api/v1/dataset/{dataset_id}").json()["result"])

    def dataset_columns(self, dataset_id: int) -> list[dict[str, Any]]:
        return self._call("GET", f"/api/v1/dataset/{dataset_id}").json()["result"]["columns"]

    def delete_dataset(self, dataset_id: int) -> None:
        self._call("DELETE", f"/api/v1/dataset/{dataset_id}")

    # --- Charts --------------------------------------------------------------

    def ensure_chart(self, spec: ChartSpec, *, dataset_id: int, dashboard_key: str) -> Chart:
        payload = build_chart_payload(spec, dataset_id, dashboard_key)

        def run() -> Chart:
            existing = self._find_one("chart", "uuid", payload["uuid"])
            if existing is None:
                chart_id = self._call("POST", "/api/v1/chart/", json=payload).json()["id"]
            else:
                chart_id = existing["id"]
                update = {k: v for k, v in payload.items() if k != "uuid"}
                self._call("PUT", f"/api/v1/chart/{chart_id}", json=update)
            return self.get_chart(chart_id)

        return self._retrying(run)

    def get_chart(self, chart_id: int) -> Chart:
        return Chart.model_validate(self._call("GET", f"/api/v1/chart/{chart_id}").json()["result"])

    def chart_detail(self, chart_id: int) -> dict[str, Any]:
        return self._call("GET", f"/api/v1/chart/{chart_id}").json()["result"]

    def chart_data(self, chart_id: int, *, force: bool = False) -> ChartData:
        """Run the chart's saved query. Raises QueryFailure if it can't run."""
        params = {"force": "true"} if force else None
        try:
            payload = self._call("GET", f"/api/v1/chart/{chart_id}/data/", params=params).json()
        except InvalidRequest as error:
            raise QueryFailure(error.message, status=error.status) from error
        result = payload["result"][0]
        if result.get("error"):
            raise QueryFailure(str(result["error"]))
        return ChartData(columns=result.get("colnames") or [], rows=result.get("data") or [],
                         row_count=result.get("rowcount") or 0, query=result.get("query"),
                         rejected_filters=result.get("rejected_filters") or [])

    def delete_chart(self, chart_id: int) -> None:
        self._call("DELETE", f"/api/v1/chart/{chart_id}")

    # --- Dashboards ----------------------------------------------------------

    def ensure_dashboard(self, *, slug: str, title: str, rows: list[list[ChartSlot]] | None = None,
                         filters: list[SelectFilter | TimeFilter] | None = None, published: bool = True) -> Dashboard:
        """Create or update a dashboard, replacing its layout and filters.

        Superset takes the chart list from the layout, so call this after the
        charts exist. With `rows=None` it only makes sure the dashboard exists.
        """
        def run() -> Dashboard:
            existing = self._find_one("dashboard", "slug", slug)
            if existing is None:
                dashboard_id = self._call("POST", "/api/v1/dashboard/", json={
                    "dashboard_title": title, "slug": slug, "published": published,
                }).json()["id"]
            else:
                dashboard_id = existing["id"]

            update: dict[str, Any] = {"dashboard_title": title, "published": published}
            if rows is not None:
                positions = build_positions(title, rows)
                update["json_metadata"] = json.dumps(build_json_metadata(positions, filters or []))
            self._call("PUT", f"/api/v1/dashboard/{dashboard_id}", json=update)
            return self.get_dashboard(dashboard_id)

        return self._retrying(run)

    def get_dashboard(self, dashboard_id: int | str) -> Dashboard:
        return Dashboard.model_validate(self._call("GET", f"/api/v1/dashboard/{dashboard_id}").json()["result"])

    def dashboard_detail(self, dashboard_id: int | str) -> dict[str, Any]:
        return self._call("GET", f"/api/v1/dashboard/{dashboard_id}").json()["result"]

    def dashboard_charts(self, dashboard_id: int | str) -> list[Chart]:
        result = self._call("GET", f"/api/v1/dashboard/{dashboard_id}/charts").json()["result"]
        return [Chart(id=c["id"], slice_name=c["slice_name"], viz_type=c.get("viz_type")) for c in result]

    def delete_dashboard(self, dashboard_id: int) -> None:
        self._call("DELETE", f"/api/v1/dashboard/{dashboard_id}")

    # --- Embedding -----------------------------------------------------------

    def enable_embedding(self, dashboard_id: int, allowed_domains: list[str]) -> str:
        """Turn on embedding and return the UUID the embedded SDK needs."""
        response = self._retrying(lambda: self._call(
            "POST", f"/api/v1/dashboard/{dashboard_id}/embedded", json={"allowed_domains": allowed_domains}))
        return response.json()["result"]["uuid"]

    def guest_token(self, embed_uuid: str, *, username: str, rls: list[dict[str, Any]] | None = None) -> str:
        """A short-lived token for one embedded dashboard. `rls` clauses look like
        {"dataset": 12, "clause": "region_code = 'UK'"}, and apply to every query the
        token makes."""
        response = self._retrying(lambda: self._call("POST", "/api/v1/security/guest_token/", json={
            "user": {"username": username},
            "resources": [{"type": "dashboard", "id": embed_uuid}],
            "rls": rls or [],
        }))
        return response.json()["token"]


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        for key in ("message", "msg", "error", "errors"):
            if key in body:
                return str(body[key])[:600]
    return str(body)[:600]
