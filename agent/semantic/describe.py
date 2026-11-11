"""A data model as compact text, for agents to read."""

from __future__ import annotations

from typing import Any

from agent.semantic.model import DataModel, Measure, Status

ADDITIVE = {"count", "sum"}  # a total of these means something; of averages, distinct counts or ratios it doesn't


def describe_model(model: DataModel, *, with_sql: bool = False) -> str:
    """Every cube, with the members an agent can query. With `with_sql`, how each is defined, so an
    agent can check a number independently. Titles and descriptions were drafted by a model that
    read the source, so callers wrap this in `wrap_untrusted` where it matters."""
    sections = []
    for cube in model.cubes:
        lines = [f"### {cube.name}" + (f": {cube.title}" if cube.title else "")]
        if cube.description:
            lines.append(cube.description.strip())
        if with_sql:
            lines.append(f"Rows: {cube.sql_table or cube.sql}")
        if cube.measures:
            lines.append("Measures:")
            for measure in cube.measures:
                facts = [measure.type, *(f for f in (measure.meta.unit, measure.format) if f)]
                if measure.meta.status is Status.REVIEWED:
                    facts.append("reviewed")
                text = f"- {cube.name}.{measure.name} ({', '.join(facts)})"
                lines.append(text + (f": {measure.title}" if measure.title else "")
                             + (f". {measure.description.strip()}" if measure.description else ""))
                if with_sql:
                    where = "".join(f" WHERE {f.sql}" for f in measure.filters)
                    lines.append(f"  = {measure.type}({measure.sql or '*'}){where}")
        dimensions = [d for d in cube.dimensions if d.public is not False and not d.primary_key]
        if dimensions:
            lines.append("Dimensions:")
            lines += [f"- {cube.name}.{d.name} ({d.type})" + (f": {d.description.strip()}" if d.description else "")
                      for d in dimensions]
        if cube.segments:
            lines.append("Segments: " + ", ".join(f"{cube.name}.{s.name}" for s in cube.segments))
        if cube.joins:
            lines.append("Joins: " + ", ".join(f"{j.name} ({j.relationship}" + (f": {j.sql})" if with_sql else ")")
                                         for j in cube.joins))
        sections.append("\n".join(lines))
    return "\n\n".join(sections) or "(no cubes)"


def annotate(annotation: dict[str, Any], model: DataModel | None) -> dict[str, Any]:
    """Cube's notes on a result, plus whether each measure can be added up into a total. Cube calls
    every measure a number, so whether a total is meaningful comes from how the measure aggregates."""
    measures = {}
    for name, note in (annotation.get("measures") or {}).items():
        cube_name, _, member_name = name.partition(".")
        cube = model.cube(cube_name) if model else None
        member = cube.member(member_name) if cube else None
        measures[name] = {**note, "additive": isinstance(member, Measure) and member.type in ADDITIVE}
    return {**annotation, "measures": measures}
