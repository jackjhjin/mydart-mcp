"""원격(HTTP)으로 띄울 때 쓰는 진입점.

Claude Desktop에 설치해 쓰는 경우에는 필요 없다. claude.ai 채팅이나 폰에서 쓰려면
서버가 인터넷 어딘가에 떠 있어야 하고, 그때 이 파일이 쓰인다.

인증키는 서버에 저장하지 않는다. 커넥터 주소 뒤에 붙여 보낸 것을 그 요청 동안만
쓴다 — 여러 사람이 같은 주소를 써도 서로의 키가 섞이지 않는다.

    https://<주소>/mcp?key=발급받은_인증키
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.types import Receive, Scope, Send

from . import dart, landing, stock
from .server import mcp

# 서버리스(Vercel 등)에서는 홈 디렉터리에 쓸 수 없다. 쓸 수 있는 곳은 /tmp뿐이고,
# 인스턴스가 살아 있는 동안 유지되므로 캐시 자리로는 충분하다.
os.environ.setdefault("MYDART_CACHE_DIR", "/tmp/mydart-mcp")

_KEY_PARAMS = ("key", "opendart_key", "crtfc_key", "dart_api_key")


def _build_mcp_app():
    """요청 하나를 처리할 MCP 앱을 새로 만든다.

    SDK의 세션 관리자는 인스턴스당 한 번만 시작할 수 있고, 시작은 Starlette의
    lifespan에서 일어난다. 서버리스 플랫폼은 lifespan을 돌려준다는 보장이 없어,
    하나를 오래 들고 있으면 'Task group is not initialized'로 죽는다.

    세션을 쓰지 않는 모드라 요청 사이에 이어갈 상태가 없으므로, 매 요청마다 만들고
    끝나면 버린다. 도구 정의는 모듈 수준 `mcp`에 그대로 있어 다시 만들지 않는다.
    """
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        # SDK 기본값은 로컬 주소만 허용한다. 이건 브라우저가 내 PC의 로컬 서버를 치는
        # DNS 리바인딩을 막기 위한 것이라, 공개 주소로 배포하면 배포 도메인을 미리 알 수
        # 없어 전부 거부된다. 여기서는 인증키가 없으면 위에서 이미 막히므로 끈다.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


def _team_from(scope: Scope) -> str:
    """주소의 &team= 값(시세 도구용 팀 암호)을 꺼낸다. 없으면 빈 문자열."""
    from urllib.parse import parse_qs

    query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
    values = query.get("team")
    return values[0].strip() if values and values[0].strip() else ""


def _key_from(scope: Scope) -> str:
    """주소의 쿼리나 Authorization 헤더에서 인증키를 꺼낸다."""
    from urllib.parse import parse_qs

    query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
    for name in _KEY_PARAMS:
        values = query.get(name)
        if values and values[0].strip():
            return values[0].strip()

    for raw_name, raw_value in scope.get("headers", []):
        if raw_name == b"authorization":
            value = raw_value.decode("utf-8", "replace")
            if value.lower().startswith("bearer "):
                return value[7:].strip()
    return ""


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "lifespan":
        # 우리는 요청마다 MCP 앱을 새로 세우므로 여기서 할 일이 없다.
        # 그래도 플랫폼이 보내면 받아 줘야 시작 단계에서 멈추지 않는다.
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    if scope["type"] != "http":
        return

    # 클라이언트는 붙기 전에 OAuth 설정이 있는지 /.well-known/... 을 먼저 물어본다.
    # 여기에 200으로 아무거나 돌려주면 그것을 로그인 설정으로 읽고 등록을 시도하다
    # 실패한다. 이 서버는 OAuth를 쓰지 않으므로 없다고 분명히 답해야 한다.
    if scope.get("path", "").startswith("/.well-known/"):
        await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
        return

    key = _key_from(scope)

    # 인증키 없이 들어온 GET은 사람이 브라우저로 열어 본 것이다. 저장소를 볼 수 없는
    # 사람에게는 이 페이지가 유일한 설명서라, 붙이는 방법을 여기서 전부 알려 준다.
    # 커넥터는 언제나 키를 들고 오므로 여기 걸리지 않는다.
    if not key and scope.get("method", "GET").upper() == "GET":
        await HTMLResponse(landing.page(scope))(scope, receive, send)
        return

    if not key:
        # 401은 "로그인이 필요하다"는 신호라 클라이언트가 OAuth 절차를 시작한다.
        # 여기서 모자란 것은 로그인이 아니라 주소에 붙일 인증키다.
        await JSONResponse(
            {
                "error": "missing_api_key",
                "message": "주소 뒤에 ?key=발급받은_인증키 를 붙이세요. "
                "https://opendart.fss.or.kr 에서 무료로 발급받습니다.",
            },
            status_code=400,
        )(scope, receive, send)
        return

    # 이 함수 뒤에는 MCP뿐이라 경로가 무엇이든 MCP로 보낸다. Vercel은 rewrite를 거치며
    # 경로를 `/api/index`로 바꿔 넘기기 때문에, 그대로 두면 `/mcp` 라우트에 걸리지 않는다.
    scope = {**scope, "path": "/mcp", "raw_path": b"/mcp", "root_path": ""}

    dart.use_api_key(key)
    stock.use_team_token(_team_from(scope))
    mcp_app = _build_mcp_app()
    async with mcp_app.router.lifespan_context(mcp_app):
        await mcp_app(scope, receive, send)


def main() -> Any:
    """로컬에서 원격 방식으로 띄워 볼 때 쓴다: `mydart-mcp-http`."""
    import uvicorn

    dart.hide_api_key_in_logs()
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        access_log=False,  # 주소에 인증키가 들어 있어 접근 로그를 남기지 않는다
    )
