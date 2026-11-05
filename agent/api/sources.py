"""Connecting sources and setting up how they separate tenants."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from agent.api.auth import User, current_user, require_admin
from agent.harness.access import load_access
from agent.harness.audit import AuditKind, AuditStatus, RunContext
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.guardrails import IDENTIFIER
from agent.subagents.profiler import generate_skill
from agent.tools.connectors import (CONNECTORS, Connection, create_catalog_sql, encrypt_credentials, superset_uri,
                                    test_connection, write_credentials)
from agent.tools.profiler import suggest_tenancy
from agent.tools.skill_store import skill_slug
from agent.tools.sources import Source, get_source_by_slug, list_sources, register_source

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sources", tags=["sources"])

DEMOS = {
    "demo-rental": {"name": "Rental platform", "kind": "postgres", "host": "demo-postgres", "port": 5432, "database": "rental",
                    "catalog": "demo_rental", "description": "Multi-tenant vehicle rental: fleets, agreements, invoices and damages"},
    "demo-shop": {"name": "Demo shop", "kind": "postgres", "host": "demo-postgres", "port": 5432, "database": "shop",
                  "catalog": "demo_shop", "description": "E-commerce: orders, customers, products and refunds"},
    "demo-marketing": {"name": "Demo marketing", "kind": "mysql", "host": "demo-mysql", "port": 3306, "database": "marketing",
                       "catalog": "demo_marketing", "description": "Campaigns and ad spend"},
}
PUBLIC_FIELDS = {"slug", "display_name", "dialect", "status"}
PROFILE_LIMITS = BudgetLimits(max_steps=80, max_tokens=800_000, max_wall_seconds=600, max_sql_queries=1500)


class TenancyBody(BaseModel):
    column: str | None = None
    table: str | None = None
    label_column: str | None = None
    shared_tables: list[str] = []


def describe(source: Source, services: Any, *, admin: bool) -> dict[str, Any]:
    entry = source.model_dump(exclude={"id"}) if admin else source.model_dump(include=PUBLIC_FIELDS)
    if services.skills.exists(source.slug):
        front = services.skills.read_frontmatter(source.slug)
        entry["skill"] = {"name": front.name, "version": front.version, "status": front.status.value}
    return entry


def catalogs_in_trino(services: Any, context: RunContext) -> set[str]:
    return {row[0] for row in services.executor.run_internal("SHOW CATALOGS", context=context, purpose="list catalogs")}


def connect_job(services: Any, *, source: Source, connection: Connection, context: RunContext,
                sink: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Everything after the connection test, in the background. Failing part-way marks the source
    failed: the agent only ever sees sources that are ready."""
    settings = services.settings

    def step(name: str) -> None:
        sink({"event": "connect_step", "agent": "connector", "step": name})

    def status(value: str) -> None:
        with services.pool.connection() as conn:
            conn.execute("UPDATE sources SET status = %s WHERE id = %s", (value, source.id))

    try:
        step("superset")
        database_id = services.superset.ensure_database(connection.name, superset_uri(connection))
        services.audit.record(context, AuditKind.SUPERSET_WRITE, AuditStatus.OK, target=f"database {connection.name}")
        with services.pool.connection() as conn:
            conn.execute("UPDATE sources SET superset_database_id = %s WHERE id = %s", (database_id, source.id))

        step("catalog")
        if source.trino_catalog not in catalogs_in_trino(services, context):
            write_credentials(connection, catalog=source.trino_catalog, directory=settings.nova_trino_secrets_dir)
            services.executor.run_internal(create_catalog_sql(connection, catalog=source.trino_catalog), context=context,
                                           purpose=f"add catalog {source.trino_catalog}")
        status("profiling")
        step("profile")
        services.profile_source(source=source, context=context, sink=sink)
        status("ready")
        step("done")
        return {"status": "finished", "source": source.slug}
    except Exception:
        status("failed")
        raise


def start_connecting(services: Any, connection: Connection, *, user: User, slug: str, catalog: str) -> dict[str, Any]:
    result = test_connection(connection)  # first, so that a typo gets an answer straight away
    existing = get_source_by_slug(services.pool, slug)
    if existing is not None and existing.status != "failed":  # a failed attempt can be retried
        raise HTTPException(409, f"a source called {slug} is already connected")
    source_id = register_source(services.pool, slug=slug, display_name=connection.name, catalog=catalog,
                                dialect=CONNECTORS[connection.kind].dialect, kind=connection.kind, status="connecting")
    with services.pool.connection() as conn:
        conn.execute("UPDATE sources SET status = 'connecting', read_only = %s, encrypted_credentials = %s WHERE id = %s",
                     (result.read_only, encrypt_credentials(connection, services.settings.fernet_key), source_id))
    source = get_source_by_slug(services.pool, slug)
    started = services.runs.start_job(label=f"Connect {connection.name}", thread_id=f"source-{slug}", user_id=user.email,
                                      job=lambda context, sink: connect_job(services, source=source, connection=connection,
                                                                            context=context, sink=sink))
    return {**started, "slug": slug, "read_only": result.read_only, "warning": result.detail or None}


@router.get("")
def sources(request: Request, user: User = Depends(current_user)) -> list[dict[str, Any]]:
    services = request.app.state.services
    access = load_access(services.pool, user_id=user.id, email=user.email, role=user.role)
    visible = [source for source in list_sources(services.pool) if access.can_read(source)]
    return [describe(source, services, admin=access.is_admin) for source in visible]


@router.get("/demos", dependencies=[Depends(require_admin)])
def demos(request: Request) -> list[dict[str, Any]]:
    connected = {source.slug for source in list_sources(request.app.state.services.pool)}
    return [{"slug": slug, "name": demo["name"], "description": demo["description"], "connected": slug in connected}
            for slug, demo in DEMOS.items()]


@router.post("/demos/{slug}", status_code=202)
def connect_demo(slug: str, request: Request, user: User = Depends(require_admin)) -> dict[str, Any]:
    demo = DEMOS.get(slug)
    if demo is None:
        raise HTTPException(404, "no such demo source")
    services = request.app.state.services
    connection = Connection(name=demo["name"], kind=demo["kind"], host=demo["host"], port=demo["port"],
                            database=demo["database"], username="nova_ro", password=services.settings.demo_ro_password)
    return start_connecting(services, connection, user=user, slug=slug, catalog=demo["catalog"])


@router.post("/test", dependencies=[Depends(require_admin)])
def test_source(connection: Connection) -> dict[str, Any]:
    result = test_connection(connection)
    return {"connected": True, "read_only": result.read_only, "warning": result.detail or None}


@router.post("", status_code=202)
def connect_source(connection: Connection, request: Request, user: User = Depends(require_admin)) -> dict[str, Any]:
    slug = skill_slug(connection.name)
    return start_connecting(request.app.state.services, connection, user=user, slug=slug,
                            catalog="src_" + re.sub(r"[^a-z0-9]+", "_", slug))


@router.post("/{slug}/profile", status_code=202)
def profile_again(slug: str, request: Request, user: User = Depends(require_admin)) -> dict[str, Any]:
    """Retry profiling, for a source whose first attempt failed or to learn it again."""
    services = request.app.state.services
    source = get_source_by_slug(services.pool, slug)
    if source is None:
        raise HTTPException(404, "no such source")

    def job(context: RunContext, sink: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        services.profile_source(source=source, context=context, sink=sink)
        with services.pool.connection() as conn:
            conn.execute("UPDATE sources SET status = 'ready' WHERE id = %s", (source.id,))
        sink({"event": "connect_step", "agent": "connector", "step": "done"})
        return {"status": "finished", "source": slug}

    return services.runs.start_job(label=f"Profile {source.display_name}", thread_id=f"source-{slug}", user_id=user.email, job=job)


@router.get("/{slug}/tenancy", dependencies=[Depends(require_admin)])
def tenancy(slug: str, request: Request) -> dict[str, Any]:
    services = request.app.state.services
    source = get_source_by_slug(services.pool, slug)
    if source is None:
        raise HTTPException(404, "no such source")
    profile = services.skills.read_profile(slug) if services.skills.exists(slug) else None
    return {"column": source.tenant_column, "table": source.tenant_table, "label_column": source.tenant_label_column,
            "shared_tables": source.shared_tables, "suggestion": suggest_tenancy(profile) if profile else None,
            "tables": [table.qualified for table in profile.tables] if profile else []}


@router.put("/{slug}/tenancy", dependencies=[Depends(require_admin)])
def set_tenancy(slug: str, body: TenancyBody, request: Request) -> dict[str, Any]:
    """Declare how a source separates tenants. From then on every dataset must carry the column."""
    services = request.app.state.services
    source = get_source_by_slug(services.pool, slug)
    if source is None:
        raise HTTPException(404, "no such source")
    if body.column:
        validate_tenancy(body, services.skills.read_profile(slug) if services.skills.exists(slug) else None)
    with services.pool.connection() as conn:
        conn.execute("""UPDATE sources SET tenant_column = %s, tenant_table = %s, tenant_label_column = %s, shared_tables = %s
                        WHERE id = %s""", (body.column, body.table, body.label_column, body.shared_tables, source.id))
    return {"ok": True}


def validate_tenancy(body: TenancyBody, profile: Any) -> None:
    """Everything here ends up in SQL, so it has to name things the source really has."""
    if profile is None:
        raise HTTPException(400, "profile this source first")
    tables = {table.qualified: {column.name for column in table.columns} for table in profile.tables}
    if not IDENTIFIER.fullmatch(body.column) or not any(body.column in columns for columns in tables.values()):
        raise HTTPException(400, f"no table has a column called {body.column}")
    if body.table and body.table not in tables:
        raise HTTPException(400, f"no table called {body.table}")
    if body.label_column and (not body.table or body.label_column not in tables[body.table]):
        raise HTTPException(400, f"{body.table} has no column called {body.label_column}")
    unknown = [table for table in body.shared_tables if table not in tables]
    if unknown:
        raise HTTPException(400, f"no such tables: {', '.join(unknown)}")


@router.get("/{slug}/tenants", dependencies=[Depends(require_admin)])
def tenants(slug: str, request: Request) -> list[dict[str, str]]:
    """The tenants of a source, for an admin choosing what someone can see."""
    services = request.app.state.services
    source = get_source_by_slug(services.pool, slug)
    if source is None or not (source.tenant_column and source.tenant_table and source.tenant_label_column):
        raise HTTPException(404, "this source isn't set up as multi-tenant")
    table = source.tenant_table
    if not re.fullmatch(r"[A-Za-z0-9_]+\.[A-Za-z0-9_]+", table):
        raise HTTPException(400, "that isn't a usable table name")
    context = RunContext(run_id="tenant-list", user_id="system")
    rows = services.executor.run_internal(
        f"SELECT {source.tenant_column}, {source.tenant_label_column} FROM {source.trino_catalog}.{table} ORDER BY 2",
        context=context, purpose=f"{source.slug}: list tenants")
    return [{"id": str(row[0]), "name": str(row[1])} for row in rows]


def profile_source(services: Any, *, source: Source, context: RunContext, sink: Callable[[dict[str, Any]], None]) -> None:
    """Measure a source and have the profiler subagent write its skill."""
    generate_skill(model=services.model, store=services.skills, executor=services.executor, source_id=source.id, slug=source.slug,
                   catalog=source.trino_catalog, dialect=source.dialect, context=context, budget=RunBudget(PROFILE_LIMITS),
                   sink=sink)

