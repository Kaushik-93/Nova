"""Skills on disk, tracked in Postgres.

    skills/<slug>/
      SKILL.md             frontmatter plus the guide the agent loads on demand
      schema.json          the measured SourceProfile
      model/<slug>.yml     the semantic layer: a Cube data model, which Cube loads
      model/access.json    who may read which cube, enforced by Cube (agent/semantic/access.py)
      examples.json        business questions with the semantic queries that answer them, verified
      proposals.json       changes to reviewed definitions, waiting for a person
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
from agent.semantic.access import Tenancy, apply_tenancy
from agent.semantic.model import (TENANT_DIMENSION, DataModel, Dimension, Measure, MemberMeta, Proposal, Proposals,
                                  write_atomically)
from agent.semantic.query import Example
from agent.semantic.review import accept, approve_all, approve_member
from agent.semantic.validate import render_checked
from agent.tools.profiler import Profiler, SchemaDiff, Shape, SourceProfile, diff_shapes, fingerprint, shape_of

MAX_DESCRIPTION = 1024
SKILL_FILE = "SKILL.md"
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


class SkillStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"


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

    def model_file(self, slug: str) -> Path:
        return self.path(slug) / "model" / f"{slug}.yml"

    def read_model(self, slug: str) -> DataModel | None:
        """The data model as drafted and reviewed, without the tenant dimension Nova adds."""
        path = self.model_file(slug)
        if not path.is_file():
            return None
        model = DataModel.from_yaml(path.read_text())
        for cube in model.cubes:
            cube.dimensions = [d for d in cube.dimensions if d.name != TENANT_DIMENSION]
        return model

    def read_access(self, slug: str) -> dict[str, Any]:
        path = self.path(slug) / "model" / "access.json"
        return json.loads(path.read_text()) if path.is_file() else {"catalog": None, "cubes": {}}

    def read_examples(self, slug: str) -> list[Example]:
        path = self.path(slug) / "examples.json"
        return [Example.model_validate(item) for item in json.loads(path.read_text())] if path.is_file() else []

    def read_proposals(self, slug: str) -> list[Proposal]:
        path = self.path(slug) / "proposals.json"
        return Proposals.model_validate_json(path.read_text()).items if path.is_file() else []

    def cube_names(self, *, excluding: str | None = None) -> set[str]:
        """Cube names already used by other skills; Cube needs them unique across all of them."""
        names = set()
        for directory in self.root.iterdir() if self.root.is_dir() else []:
            if directory.name != excluding and not directory.name.startswith(("_", ".")):
                model = self.read_model(directory.name)
                names |= {cube.name for cube in model.cubes} if model else set()
        return names

    def write(self, *, source_id: str, slug: str, description: str, body: str, profile: SourceProfile,
              model: DataModel, tenancy: Tenancy, examples: list[Example], change: str,
              proposals: list[Proposal] = (), reviewed_by: str | None = None) -> SkillFrontmatter:
        """Write a skill, as a new version if it already exists. `model` must already have been
        verified, and merged with what people reviewed (agent/semantic/review.py). A skill with
        reviewed definitions stays `reviewed`."""
        directory = self.path(slug)
        previous = self.read_frontmatter(slug) if self.exists(slug) else None
        keeps_review = previous is not None and previous.status is SkillStatus.REVIEWED
        front = SkillFrontmatter(
            name=slug, description=description, source_id=source_id, dialect=profile.dialect,
            schema_fingerprint=profile.fingerprint, version=previous.version + 1 if previous else 1,
            status=SkillStatus.REVIEWED if keeps_review else SkillStatus.DRAFT,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            reviewed_by=reviewed_by or (previous.reviewed_by if previous else None),
        )
        directory.mkdir(parents=True, exist_ok=True)
        write_atomically(directory / SKILL_FILE, render_frontmatter(front) + body.rstrip() + "\n")
        write_atomically(directory / "schema.json", profile.model_dump_json(indent=2) + "\n")
        write_atomically(directory / "examples.json",
                         json.dumps([e.model_dump(mode="json", exclude_defaults=True) for e in examples], indent=2) + "\n")
        if proposals:
            self._write_proposals(slug, [*self.read_proposals(slug), *proposals])
        self._write_model(slug, model, profile=profile, tenancy=tenancy)
        for old in ("metrics.yaml", "examples.sql"):  # the format before Cube
            (directory / old).unlink(missing_ok=True)
        self._log(slug, f"## v{front.version} ({front.generated_at})\n\n{change.rstrip()}")
        self._track(front, directory)
        return front

    def publish_model(self, slug: str, model: DataModel, *, profile: SourceProfile, tenancy: Tenancy) -> None:
        """Publish a model written by code rather than drafted by a model, such as the chart gallery's.
        It still gets its tenant limits from the tenancy settings, like every other model."""
        self._write_model(slug, model, profile=profile, tenancy=tenancy)

    def _write_model(self, slug: str, model: DataModel, *, profile: SourceProfile, tenancy: Tenancy) -> None:
        scoped, manifest = apply_tenancy(model, catalog=profile.catalog, profile=profile, tenancy=tenancy)
        directory = self.path(slug) / "model"
        # The manifest first: until the model's own file changes, Cube keeps the model it compiled.
        write_atomically(directory / "access.json", json.dumps(manifest, indent=2) + "\n")
        write_atomically(self.model_file(slug), render_checked(scoped))

    def _write_proposals(self, slug: str, proposals: list[Proposal]) -> None:
        write_atomically(self.path(slug) / "proposals.json", Proposals(items=proposals).model_dump_json(indent=2) + "\n")

    def _log(self, slug: str, entry: str) -> None:
        with (self.path(slug) / "CHANGELOG.md").open("a") as changelog:
            changelog.write(f"{entry.rstrip()}\n\n")

    def _require_model(self, slug: str) -> DataModel:
        model = self.read_model(slug)
        if model is None:
            raise NotFound(f"the skill {slug} has no data model yet")
        return model

    def apply_tenancy(self, slug: str, tenancy: Tenancy) -> dict[str, Any]:
        """Rewrite the access manifest after a source's tenancy settings change."""
        model = self._require_model(slug)
        self._write_model(slug, model, profile=self.read_profile(slug), tenancy=tenancy)
        return self.read_access(slug)

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

    def approve(self, slug: str, *, reviewed_by: str, tenancy: Tenancy) -> SkillFrontmatter:
        """Mark a skill and every definition in it as reviewed. From then on re-profiling can
        only propose changes to them."""
        directory = self.path(slug)
        front, body = parse_frontmatter((directory / SKILL_FILE).read_text())
        model = approve_all(self._require_model(slug), reviewer=reviewed_by)
        approved = front.model_copy(update={"status": SkillStatus.REVIEWED, "reviewed_by": reviewed_by,
                                            "version": front.version + 1})
        self._write_model(slug, model, profile=self.read_profile(slug), tenancy=tenancy)
        write_atomically(directory / SKILL_FILE, render_frontmatter(approved) + body)
        self._log(slug, f"## v{approved.version}\n\nApproved by {reviewed_by}.")
        self._track(approved, directory)
        return approved

    def approve_member(self, slug: str, cube: str, member: str, *, reviewed_by: str, tenancy: Tenancy) -> None:
        model = approve_member(self._require_model(slug), cube, member, reviewer=reviewed_by)
        self._write_model(slug, model, profile=self.read_profile(slug), tenancy=tenancy)
        self._log(slug, f"## {cube}.{member}\n\nApproved by {reviewed_by}.")

    def add_member(self, slug: str, cube: str, member: Dimension | Measure, *, reason: str, verifier: Any,
                   identity: Any) -> None:
        """Add a definition the model lacks, once it's been checked (agent/semantic/verify.py). It's
        published as inferred, so nothing built on it is certified until a person reviews it."""
        model = self._require_model(slug)
        target = model.cube(cube)
        if target is None:
            raise NotFound(f"no cube {cube} in {slug}")
        if target.member(member.name) is not None:
            raise InvalidRequest(f"{cube}.{member.name} already exists; propose a change to it instead")
        member = member.model_copy(update={"meta": MemberMeta(unit=member.meta.unit)}, deep=True)
        (target.dimensions if isinstance(member, Dimension) else target.measures).append(member)
        verifier.verify_member(model, cube, member.name, identity)
        self._write_model(slug, model, profile=self.read_profile(slug), tenancy=verifier.tenancy)
        self._log(slug, f"## Added {cube}.{member.name}\n\n{reason.rstrip()} (AI-inferred, not yet reviewed)")

    def propose(self, slug: str, proposal: Proposal) -> None:
        """Record a suggested change for a person to accept or reject. Nothing the agent relies
        on changes until someone does."""
        self._require_model(slug)
        self._write_proposals(slug, [*self.read_proposals(slug), proposal])
        self._log(slug, f"## Proposed change to {proposal.cube}.{proposal.member.name}\n\n{proposal.reason}")

    def decide(self, slug: str, index: int, *, accepted: bool, reviewed_by: str, tenancy: Tenancy) -> None:
        """Accept or reject a proposal. An accepted one replaces the definition, as reviewed."""
        proposals = self.read_proposals(slug)
        if not 0 <= index < len(proposals):
            raise NotFound("that proposal no longer exists")
        proposal = proposals.pop(index)
        if accepted:
            model = accept(self._require_model(slug), proposal, reviewer=reviewed_by)
            self._write_model(slug, model, profile=self.read_profile(slug), tenancy=tenancy)
        self._write_proposals(slug, proposals)
        verdict = "Accepted" if accepted else "Rejected"
        self._log(slug, f"## {proposal.cube}.{proposal.member.name}\n\n{verdict} by {reviewed_by}.")

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
            body: str, tenancy: Tenancy) -> tuple[SkillFrontmatter, SchemaDiff]:
    """Incremental re-profile: measure only what changed, bump the version, and log
    the diff in the changelog. Does nothing if the skill is already fresh. The data model is kept
    as it is: re-profiling it is a person's decision, because reviewed definitions depend on it."""
    report = store.check_freshness(slug, profiler.read_shape(catalog))
    if report.status is Freshness.FRESH:
        return store.read_frontmatter(slug), report.diff
    previous = store.read_profile(slug)
    updated = profiler.profile(previous.source_slug, catalog, previous.dialect, previous=previous,
                               only_tables=set(report.diff.changed_tables))
    front = store.write(source_id=source_id, slug=slug, description=description, body=body, profile=updated,
                        model=store._require_model(slug), tenancy=tenancy, examples=store.read_examples(slug),
                        change=describe_change(report.diff))
    return front, report.diff


def profile_to_json(profile: SourceProfile) -> str:
    """The profile as compact JSON, for the subagent. Always wrap with wrap_untrusted."""
    return json.dumps(profile.model_dump(mode="json"), ensure_ascii=False)
