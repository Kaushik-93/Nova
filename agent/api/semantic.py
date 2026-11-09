"""The semantic layer over HTTP: the catalog of what can be asked, queries, and people's review.

Every query runs in Cube as the person asking, with a token carrying their own grants
(agent/semantic/client.py), so nothing here has to remember to filter rows: Cube refuses or limits
the query itself. What this layer does filter is the catalog, so nobody is shown cubes they
couldn't query.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel

from agent.api.auth import User, require_admin, require_builder
from agent.harness.errors import NotFound
from agent.semantic.access import tenancy_of
from agent.semantic.client import identity_for
from agent.semantic.model import DataModel, Dimension, Measure, Status
from agent.semantic.query import SemanticQuery
from agent.semantic.review import certified
from agent.tools.skill_store import SkillStore, parse_frontmatter
from agent.tools.sources import Source, get_source_by_slug, list_sources

router = APIRouter(prefix="/api", tags=["semantic"])
MAX_CSV_ROWS = 50_000


class QueryBody(BaseModel):
    query: SemanticQuery


class Decision(BaseModel):
    accept: bool


def _services(request: Request) -> Any:
    return request.app.state.services


def _readable(services: Any, user: User) -> tuple[list[Source], Any]:
    sources = [source for source in list_sources(services.pool) if source.status == "ready"]
    access = services.access_of(user)
    return [source for source in sources if access.can_read(source)], access


def _identity(services: Any, user: User):
    readable, access = _readable(services, user)
    return identity_for(access, readable, user_id=user.email), readable


def _models(services: Any, sources: list[Source]) -> dict[str, DataModel]:
    store: SkillStore = services.skills
    models = {}
    for source in sources:
        model = store.read_model(source.slug) if store.exists(source.slug) else None
        if model is not None:
            models[source.slug] = model
    return models


def _member(cube_name: str, member: Dimension | Measure) -> dict[str, Any]:
    return {"name": f"{cube_name}.{member.name}", "short_name": member.name, "title": member.title or member.name,
            "description": member.description, "type": member.type,
            "format": getattr(member, "format", None), "unit": member.meta.unit,
            "status": member.meta.status.value, "reviewed_by": member.meta.reviewed_by}


def describe_cubes(model: DataModel, *, hidden: set[str] = frozenset()) -> list[dict[str, Any]]:
    """A model as the UI and agents see it: no primary keys or Nova's own tenant dimension."""
    cubes = []
    for cube in model.cubes:
        if cube.name in hidden:
            continue
        dimensions = [d for d in cube.dimensions if d.public is not False and not d.primary_key]
        cubes.append({
            "name": cube.name, "title": cube.title or cube.name, "description": cube.description,
            "measures": [_member(cube.name, m) for m in cube.measures],
            "dimensions": [_member(cube.name, d) for d in dimensions],
            "segments": [{"name": f"{cube.name}.{s.name}", "title": s.title or s.name, "description": s.description}
                         for s in cube.segments],
            "joins": [{"cube": j.name, "relationship": j.relationship} for j in cube.joins],
        })
    return cubes


def _hidden_for(services: Any, source: Source, identity) -> set[str]:
    """Cubes this person can't read: on a source they're limited on, those that can't be limited."""
    if identity.scopes.get(source.trino_catalog) is None:
        return set()
    rules = services.skills.read_access(source.slug)["cubes"]
    return {name for name, rule in rules.items() if not rule.get("tenant_dimension") and not rule.get("shared")}


@router.get("/semantic/meta")
def meta(request: Request, user: User = Depends(require_builder)) -> list[dict[str, Any]]:
    """What this person can ask about: each readable source's cubes and members."""
    services = _services(request)
    identity, readable = _identity(services, user)
    models = _models(services, readable)
    return [{"source": source.slug, "display_name": source.display_name, "catalog": source.trino_catalog,
             "cubes": describe_cubes(models[source.slug], hidden=_hidden_for(services, source, identity))}
            for source in readable if source.slug in models]


def _model_for(services: Any, readable: list[Source]) -> DataModel:
    """Every readable model as one, for working out whether a query is certified."""
    return DataModel(cubes=[cube for model in _models(services, readable).values() for cube in model.cubes])


def run_query(services: Any, user: User, query: SemanticQuery) -> dict[str, Any]:
    identity, readable = _identity(services, user)
    result = services.cube.load(identity, query.to_cube())
    columns = list(result.data[0].keys()) if result.data else [*query.measures, *query.dimensions]
    return {"columns": columns, "rows": result.data, "row_count": len(result.data),
            "annotation": result.annotation, "certified": certified(_model_for(services, readable), query.members())}


@router.post("/semantic/query")
def query(body: QueryBody, request: Request, user: User = Depends(require_builder)) -> dict[str, Any]:
    return run_query(_services(request), user, body.query)


@router.post("/semantic/sql")
def sql(body: QueryBody, request: Request, user: User = Depends(require_builder)) -> dict[str, str]:
    """The SQL Cube runs for a query, with this person's limits applied."""
    services = _services(request)
    identity, _ = _identity(services, user)
    return {"sql": services.cube.sql(identity, body.query.to_cube())}


@router.post("/semantic/csv")
def export_csv(body: QueryBody, request: Request, user: User = Depends(require_builder)) -> Response:
    query = body.query.model_copy(update={"limit": min(body.query.limit or MAX_CSV_ROWS, MAX_CSV_ROWS)})
    result = run_query(_services(request), user, query)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=result["columns"], extrasaction="ignore")
    writer.writeheader()
    for row in result["rows"]:
        # A value starting with = + - @ would run as a formula in a spreadsheet.
        writer.writerow({key: f"'{value}" if isinstance(value, str) and value[:1] in "=+-@" else value
                         for key, value in row.items()})
    return Response(buffer.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="nova-query.csv"'})


# --- Skills: the catalog and its review (administrators) ---------------------------------------------

def _require_skill(services: Any, slug: str) -> Source:
    source = get_source_by_slug(services.pool, slug)
    if source is None or not services.skills.exists(slug):
        raise NotFound(f"no skill named {slug}")
    return source


def _legacy_metrics(model: DataModel) -> dict[str, dict[str, Any]]:
    """The measures in the shape the Skills page read before Cube. Goes when that page does."""
    metrics = {}
    for cube in model.cubes:
        for m in cube.measures:
            metrics[f"{cube.name}.{m.name}"] = {
                "label": m.title or m.name, "sql": f"{m.type.upper()}({m.sql or '*'})", "base": cube.sql_table or cube.sql,
                "filters": [f.sql for f in m.filters], "unit": m.meta.unit, "description": m.description,
                "status": m.meta.status.value}
    return metrics


@router.get("/skills/{slug}", dependencies=[Depends(require_admin)])
def skill(slug: str, request: Request) -> dict[str, Any]:
    services = _services(request)
    _require_skill(services, slug)
    store: SkillStore = services.skills
    directory = store.path(slug)
    front, body = parse_frontmatter((directory / "SKILL.md").read_text())
    model = store.read_model(slug) or DataModel()
    profile = store.read_profile(slug)
    proposals = store.read_proposals(slug)
    examples = store.read_examples(slug)
    return {"name": front.name, "description": front.description, "version": front.version, "status": front.status.value,
            "generated_at": front.generated_at, "reviewed_by": front.reviewed_by, "guide": body,
            "cubes": describe_cubes(model), "access": store.read_access(slug)["cubes"],
            "proposals": [{"index": index, "cube": p.cube, "member": p.member.model_dump(mode="json", exclude_none=True),
                           "reason": p.reason} for index, p in enumerate(proposals)],
            "example_queries": [e.model_dump(mode="json", exclude_defaults=True) for e in examples],
            "metrics": _legacy_metrics(model),
            "proposed": {f"{p.cube}.{p.member.name}": {"label": p.member.title or p.member.name, "sql": p.member.sql or ""}
                         for p in proposals},
            "examples": "\n\n".join(f"-- {e.question}\n{e.query.model_dump_json(exclude_defaults=True)}" for e in examples),
            "gotchas": [gotcha.model_dump(mode="json") for gotcha in profile.gotchas],
            "tables": [{"name": t.qualified, "rows": t.row_count} for t in profile.tables],
            "changelog": (directory / "CHANGELOG.md").read_text() if (directory / "CHANGELOG.md").is_file() else ""}


@router.post("/skills/{slug}/approve")
def approve_skill(slug: str, request: Request, user: User = Depends(require_admin)) -> dict[str, Any]:
    services = _services(request)
    source = _require_skill(services, slug)
    front = services.skills.approve(slug, reviewed_by=user.email, tenancy=tenancy_of(source))
    return {"name": front.name, "version": front.version, "status": front.status.value}


@router.post("/skills/{slug}/members/{cube}/{member}/approve")
def approve_member(slug: str, cube: str, member: str, request: Request, user: User = Depends(require_admin)) -> dict[str, bool]:
    services = _services(request)
    source = _require_skill(services, slug)
    services.skills.approve_member(slug, cube, member, reviewed_by=user.email, tenancy=tenancy_of(source))
    return {"ok": True}


@router.post("/skills/{slug}/proposals/{index}")
def decide(slug: str, index: int, body: Decision, request: Request, user: User = Depends(require_admin)) -> dict[str, bool]:
    services = _services(request)
    source = _require_skill(services, slug)
    services.skills.decide(slug, index, accepted=body.accept, reviewed_by=user.email, tenancy=tenancy_of(source))
    return {"ok": True}


def status_of(model: DataModel) -> dict[str, int]:
    members = [m for cube in model.cubes for m in [*cube.measures, *cube.dimensions]]
    return {"members": len(members), "reviewed": sum(m.meta.status is Status.REVIEWED for m in members)}
