"""Skills on disk, tracked in Postgres.

    skills/<slug>/
      SKILL.md       frontmatter plus the guide the agent loads on demand
      schema.json    the measured SourceProfile
      metrics.yaml   the semantic layer
      examples.sql   verified queries
      CHANGELOG.md

Deep Agents only reads `name`, `description`, `license`, `compatibility`, `metadata`
and `allowed-tools` from frontmatter, so Nova's fields live under `metadata`, whose
values it keeps as strings. Names may only use lowercase letters, digits and single
hyphens, and must match the directory, hence `skill_slug`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from agent.harness.errors import InvalidRequest, NotFound
from agent.tools.profiler import Profiler, SchemaDiff, Shape, SourceProfile, diff_shapes, fingerprint, shape_of

MAX_DESCRIPTION = 1024
SKILL_FILE = "SKILL.md"
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


class SkillStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"


class DefinitionStatus(StrEnum):
    INFERRED = "inferred"
    REVIEWED = "reviewed"


class Metric(BaseModel):
    label: str
    sql: str
    base: str = Field(description="FROM and JOIN clause the expression applies to")
    filters: list[str] = Field(default_factory=list)
    unit: str | None = None
    description: str | None = None
    status: DefinitionStatus = DefinitionStatus.INFERRED
    reviewed_by: str | None = None


class SemanticLayer(BaseModel):
    metrics: dict[str, Metric] = Field(default_factory=dict)
    dimensions: dict[str, str] = Field(default_factory=dict)
    joins: list[str] = Field(default_factory=list)
    # A change to a reviewed metric waits here for a person to accept it.
    proposed: dict[str, Metric] = Field(default_factory=dict)


class SkillFrontmatter(BaseModel):
    name: str
    description: str
    source_id: str
    dialect: str
    schema_fingerprint: str
    version: int
    status: SkillStatus
    generated_at: str
    reviewed_by: str | None = None


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"


class FreshnessReport(BaseModel):
    status: Freshness
    skill_fingerprint: str | None = None
    live_fingerprint: str
    diff: SchemaDiff = Field(default_factory=SchemaDiff)


def skill_slug(value: str) -> str:
    """Turn a source name into a valid skill name."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)[:64].strip("-")
    if not slug:
        raise InvalidRequest(f"{value!r} doesn't make a usable skill name")
    return slug


def merge_semantic_layers(current: SemanticLayer, incoming: SemanticLayer) -> SemanticLayer:
    """Combine a fresh proposal with what exists. Reviewed definitions always win: if
    the proposal differs, it goes to `proposed` for a person to accept or reject,
    so re-profiling never silently overwrites a reviewed metric."""
    merged = SemanticLayer(metrics=dict(incoming.metrics), dimensions=dict(incoming.dimensions),
                           joins=list(incoming.joins), proposed=dict(current.proposed))
    for key, metric in current.metrics.items():
        if metric.status is not DefinitionStatus.REVIEWED:
            continue
        proposal = incoming.metrics.get(key)
        merged.metrics[key] = metric
        if proposal is not None and (proposal.sql, proposal.base, proposal.filters) != (
                metric.sql, metric.base, metric.filters):
            merged.proposed[key] = proposal
    return merged


def render_frontmatter(front: SkillFrontmatter) -> str:
    if len(front.description) > MAX_DESCRIPTION:
        raise InvalidRequest(f"description is {len(front.description)} characters; the limit is {MAX_DESCRIPTION}")
    document = {
        "name": front.name,
        "description": front.description,
        "metadata": {
            "source_id": front.source_id,
            "dialect": front.dialect,
            "schema_fingerprint": front.schema_fingerprint,
            "version": str(front.version),
            "status": front.status.value,
            "generated_at": front.generated_at,
            "reviewed_by": front.reviewed_by or "",
        },
    }
    return "---\n" + yaml.safe_dump(document, sort_keys=False, allow_unicode=True) + "---\n"


def parse_frontmatter(text: str) -> tuple[SkillFrontmatter, str]:
    match = _FRONTMATTER.match(text)
    if not match:
        raise InvalidRequest("SKILL.md has no frontmatter")
    data = yaml.safe_load(match.group(1))
    meta = data.get("metadata") or {}
    return SkillFrontmatter(name=data["name"], description=data["description"], source_id=meta["source_id"],
                            dialect=meta["dialect"], schema_fingerprint=meta["schema_fingerprint"],
                            version=int(meta["version"]), status=SkillStatus(meta["status"]),
                            generated_at=meta["generated_at"], reviewed_by=meta.get("reviewed_by") or None
                            ), match.group(2)


class SkillStore:
    def __init__(self, root: str | Path, pool: ConnectionPool) -> None:
        self.root = Path(root)
        self._pool = pool

    def path(self, slug: str) -> Path:
        return self.root / slug

    def exists(self, slug: str) -> bool:
        return (self.path(slug) / SKILL_FILE).is_file()

    def read_frontmatter(self, slug: str) -> SkillFrontmatter:
        if not self.exists(slug):
            raise NotFound(f"no skill named {slug}")
        return parse_frontmatter((self.path(slug) / SKILL_FILE).read_text())[0]

    def read_profile(self, slug: str) -> SourceProfile:
        return SourceProfile.model_validate_json((self.path(slug) / "schema.json").read_text())

    def read_semantic_layer(self, slug: str) -> SemanticLayer:
        path = self.path(slug) / "metrics.yaml"
        if not path.is_file():
            return SemanticLayer()
        return SemanticLayer.model_validate(yaml.safe_load(path.read_text()) or {})

    def write(self, *, source_id: str, slug: str, description: str, body: str, profile: SourceProfile,
              semantic_layer: SemanticLayer, examples_sql: str, change: str,
              reviewed_by: str | None = None) -> SkillFrontmatter:
        """Write a skill, as a new version if it already exists. Reviewed metric
        definitions survive, and a skill with any stays `reviewed`."""
        directory = self.path(slug)
        exists = self.exists(slug)
        previous = self.read_frontmatter(slug) if exists else None
        merged = merge_semantic_layers(self.read_semantic_layer(slug), semantic_layer) if exists else semantic_layer

        keeps_review = previous is not None and previous.status is SkillStatus.REVIEWED
        front = SkillFrontmatter(
            name=slug, description=description, source_id=source_id, dialect=profile.dialect,
            schema_fingerprint=profile.fingerprint, version=previous.version + 1 if previous else 1,
            status=SkillStatus.REVIEWED if keeps_review else SkillStatus.DRAFT,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            reviewed_by=reviewed_by or (previous.reviewed_by if previous else None),
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / SKILL_FILE).write_text(render_frontmatter(front) + body.rstrip() + "\n")
        (directory / "schema.json").write_text(profile.model_dump_json(indent=2) + "\n")
        (directory / "metrics.yaml").write_text(yaml.safe_dump(merged.model_dump(mode="json"), sort_keys=False))
        (directory / "examples.sql").write_text(examples_sql.rstrip() + "\n")
        with (directory / "CHANGELOG.md").open("a") as changelog:
            changelog.write(f"## v{front.version} ({front.generated_at})\n\n{change.rstrip()}\n\n")
        self._track(front, directory)
        return front

    def _track(self, front: SkillFrontmatter, directory: Path) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO skills (slug, source_id, version, status, schema_fingerprint, path, generated_at,
                                       reviewed_by)
                   VALUES (%(slug)s, %(source_id)s::uuid, %(version)s, %(status)s, %(fingerprint)s, %(path)s,
                           %(generated_at)s, %(reviewed_by)s)
                   ON CONFLICT (slug) DO UPDATE SET version = EXCLUDED.version, status = EXCLUDED.status,
                       schema_fingerprint = EXCLUDED.schema_fingerprint, path = EXCLUDED.path,
                       generated_at = EXCLUDED.generated_at, reviewed_by = EXCLUDED.reviewed_by, updated_at = now()""",
                {"slug": front.name, "source_id": front.source_id, "version": front.version,
                 "status": front.status.value, "fingerprint": front.schema_fingerprint, "path": str(directory),
                 "generated_at": front.generated_at, "reviewed_by": front.reviewed_by})

    def tracked(self, slug: str) -> dict[str, Any] | None:
        with self._pool.connection() as conn:
            cursor = conn.execute("SELECT slug, version, status, schema_fingerprint, reviewed_by FROM skills "
                                  "WHERE slug = %s", (slug,))
            row = cursor.fetchone()
            return dict(zip([c.name for c in cursor.description], row)) if row else None

    def check_freshness(self, slug: str, live: Shape) -> FreshnessReport:
        """Compare a skill with the live schema. Only a real schema change counts:
        new rows change nothing here."""
        live_fingerprint = fingerprint(live)
        if not self.exists(slug):
            return FreshnessReport(status=Freshness.MISSING, live_fingerprint=live_fingerprint)
        front = self.read_frontmatter(slug)
        if front.schema_fingerprint == live_fingerprint:
            return FreshnessReport(status=Freshness.FRESH, skill_fingerprint=front.schema_fingerprint,
                                   live_fingerprint=live_fingerprint)
        return FreshnessReport(status=Freshness.STALE, skill_fingerprint=front.schema_fingerprint,
                               live_fingerprint=live_fingerprint,
                               diff=diff_shapes(shape_of(self.read_profile(slug)), live))


def describe_change(diff: SchemaDiff) -> str:
    lines = []
    for label, items in (("Added tables", diff.added_tables), ("Removed tables", diff.removed_tables),
                         ("Added columns", diff.added_columns), ("Removed columns", diff.removed_columns),
                         ("Changed types", diff.changed_types)):
        lines += [f"- {label}: {item}" for item in items]
    return "\n".join(lines) or "- No schema changes"


def refresh(store: SkillStore, profiler: Profiler, *, slug: str, catalog: str, source_id: str, description: str,
            body: str, examples_sql: str) -> tuple[SkillFrontmatter, SchemaDiff]:
    """Incremental re-profile: measure only what changed, bump the version, and log
    the diff in the changelog. Does nothing if the skill is already fresh."""
    report = store.check_freshness(slug, profiler.read_shape(catalog))
    if report.status is Freshness.FRESH:
        return store.read_frontmatter(slug), report.diff
    previous = store.read_profile(slug)
    updated = profiler.profile(previous.source_slug, catalog, previous.dialect, previous=previous,
                               only_tables=set(report.diff.changed_tables))
    front = store.write(source_id=source_id, slug=slug, description=description, body=body, profile=updated,
                        semantic_layer=store.read_semantic_layer(slug), examples_sql=examples_sql,
                        change=describe_change(report.diff))
    return front, report.diff


def profile_to_json(profile: SourceProfile) -> str:
    """The profile as compact JSON, for the subagent. Always wrap with wrap_untrusted."""
    return json.dumps(profile.model_dump(mode="json"), ensure_ascii=False)
