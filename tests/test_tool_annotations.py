"""모든 도구가 읽기 전용으로 표시돼 있는지 확인한다.

표시가 빠진 도구는 claude.ai 팀 플랜에서 호출할 때마다 승인을 요구한다.
새 도구를 추가할 때 @mcp.tool(annotations=_READ_ONLY)를 빠뜨리면 이 테스트가 실패한다.
"""

from __future__ import annotations

import asyncio

from mydart_mcp.server import mcp


def test_every_tool_is_read_only():
    tools = asyncio.run(mcp.list_tools())
    missing = [t.name for t in tools if not (t.annotations and t.annotations.read_only_hint)]
    assert not missing, f"읽기 전용 표시가 없는 도구: {missing}"
