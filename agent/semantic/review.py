"""People's review of a data model.

A definition someone has reviewed is never changed by a model. Re-profiling a source keeps every
reviewed measure and dimension as it is, and if the fresh draft defines one differently, that goes
to a person as a proposal. A query is *certified* when every member it uses has been reviewed.
"""

from __future__ import annotations

from collections.abc import Iterable

from agent.harness.errors import NotFound
from agent.semantic.model import DataModel, Dimension, Measure, Proposal, Status


def _definition(member: Dimension | Measure) -> tuple:
    filters = tuple(f.sql for f in member.filters) if isinstance(member, Measure) else ()
    return type(member).__name__, member.type, member.sql, filters


def _put(cube, member: Dimension | Measure) -> None:
    cube.dimensions = [m for m in cube.dimensions if m.name != member.name]
    cube.measures = [m for m in cube.measures if m.name != member.name]
    (cube.dimensions if isinstance(member, Dimension) else cube.measures).append(member)


def merge_reviewed(current: DataModel | None, incoming: DataModel) -> tuple[DataModel, list[Proposal]]:
    """`incoming`, with every reviewed member of `current` kept as it was."""
    merged = incoming.model_copy(deep=True)
    proposals: list[Proposal] = []
    for old_cube in (current.cubes if current else []):
        new_cube = merged.cube(old_cube.name)
        if new_cube is None:
            continue  # its table is gone, so there's nothing for the definitions to apply to
        for old in [*old_cube.dimensions, *old_cube.measures]:
            if old.meta.status is not Status.REVIEWED:
                continue
            new = new_cube.member(old.name)
            if isinstance(new, (Dimension, Measure)) and _definition(new) != _definition(old):
                proposals.append(Proposal(cube=old_cube.name, member=new,
                                          reason="Re-profiling the source suggested this instead"))
            _put(new_cube, old)
    return merged, proposals


def approve_all(model: DataModel, *, reviewer: str) -> DataModel:
    model = model.model_copy(deep=True)
    for cube in model.cubes:
        for member in [*cube.dimensions, *cube.measures]:
            member.meta.status, member.meta.reviewed_by = Status.REVIEWED, reviewer
    return model


def approve_member(model: DataModel, cube_name: str, member_name: str, *, reviewer: str) -> DataModel:
    model = model.model_copy(deep=True)
    cube = model.cube(cube_name)
    member = cube.member(member_name) if cube else None
    if not isinstance(member, (Dimension, Measure)):
        raise NotFound(f"no measure or dimension {cube_name}.{member_name}")
    member.meta.status, member.meta.reviewed_by = Status.REVIEWED, reviewer
    return model


def accept(model: DataModel, proposal: Proposal, *, reviewer: str) -> DataModel:
    model = model.model_copy(deep=True)
    cube = model.cube(proposal.cube)
    if cube is None:
        raise NotFound(f"no cube {proposal.cube}")
    member = proposal.member.model_copy(deep=True)
    member.meta.status, member.meta.reviewed_by = Status.REVIEWED, reviewer
    _put(cube, member)
    return model


def certified(model: DataModel, members: Iterable[str]) -> bool:
    """Whether every member named (cube.member, or cube.time.granularity) has been reviewed."""
    for name in members:
        cube_name, _, rest = name.partition(".")
        cube = model.cube(cube_name)
        member = cube.member(rest.split(".")[0]) if cube else None
        if not isinstance(member, (Dimension, Measure)) or member.meta.status is not Status.REVIEWED:
            return False
    return True
