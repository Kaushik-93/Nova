"""A data model as compact text, for agents to read."""

from __future__ import annotations

from agent.semantic.model import DataModel, Status


def describe_model(model: DataModel) -> str:
    """Every cube, with the members an agent can query. Titles and descriptions were drafted by
    a model that read the source, so callers wrap this in `wrap_untrusted` where it matters."""
    sections = []
    for cube in model.cubes:
        lines = [f"### {cube.name}" + (f": {cube.title}" if cube.title else "")]
        if cube.description:
            lines.append(cube.description.strip())
        if cube.measures:
            lines.append("Measures:")
            for measure in cube.measures:
                facts = [measure.type, *(f for f in (measure.meta.unit, measure.format) if f)]
                if measure.meta.status is Status.REVIEWED:
                    facts.append("reviewed")
                text = f"- {cube.name}.{measure.name} ({', '.join(facts)})"
                lines.append(text + (f": {measure.title}" if measure.title else "")
                             + (f". {measure.description.strip()}" if measure.description else ""))
        dimensions = [d for d in cube.dimensions if d.public is not False and not d.primary_key]
        if dimensions:
            lines.append("Dimensions:")
            lines += [f"- {cube.name}.{d.name} ({d.type})" + (f": {d.description.strip()}" if d.description else "")
                      for d in dimensions]
        if cube.segments:
            lines.append("Segments: " + ", ".join(f"{cube.name}.{s.name}" for s in cube.segments))
        if cube.joins:
            lines.append("Joins: " + ", ".join(f"{j.name} ({j.relationship})" for j in cube.joins))
        sections.append("\n".join(lines))
    return "\n\n".join(sections) or "(no cubes)"
