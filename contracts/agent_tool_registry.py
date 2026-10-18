"""登録済み tool の一覧（ADR-0047）。各 tool の定義は ``contracts/agent_tools_*.py`` が持ち、
ここは束ねるだけ。同じ ``(tool_id, version)`` の二重登録は起動時に失敗する。
"""

from __future__ import annotations

from contracts.agent_tools import ToolDef, ToolId, parse_tool_ref, tool_ref
from contracts.agent_tools_research import RESEARCH_TOOL_DEFS

_DEFS: tuple[ToolDef, ...] = (*RESEARCH_TOOL_DEFS,)


def _index(defs: tuple[ToolDef, ...]) -> dict[str, ToolDef]:
    out: dict[str, ToolDef] = {}
    for d in defs:
        if d.ref in out:
            raise ValueError(f"duplicate tool registration: {d.ref}")
        out[d.ref] = d
    return out


TOOL_DEFS: dict[str, ToolDef] = _index(_DEFS)


class UnknownToolError(LookupError):
    pass


def resolve_tool(ref: str, *, defs: dict[str, ToolDef] = TOOL_DEFS) -> ToolDef:
    try:
        tool_id, version = parse_tool_ref(ref)
    except ValueError as exc:
        raise UnknownToolError(str(exc)) from exc
    found = defs.get(tool_ref(tool_id, version))
    if found is None:
        raise UnknownToolError(f"unknown tool: {ref!r}")
    return found


def registered_ids(*, defs: dict[str, ToolDef] = TOOL_DEFS) -> frozenset[ToolId]:
    return frozenset(d.tool_id for d in defs.values())
