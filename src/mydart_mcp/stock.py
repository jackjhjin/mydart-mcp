"""국내 상장주식 일별 시세 — 공공데이터포털 '금융위원회_주식시세정보' 클라이언트.

OpenDART에는 주가가 없어 시가총액·주가 추이를 볼 수 없다. 금융위원회가 공공데이터포털에
무료로 여는 주식시세정보 API로 그 빈자리를 채운다.

인증키는 서버 환경변수 DATA_GO_KR_KEY 로 받는다(공공데이터포털 마이페이지의
'일반 인증키(Decoding)'). OpenDART 키와 달리 주소에 붙여 보내지 않는다 — 한 팀이
같은 서버를 쓸 때 키 하나로 충분하고, 팀원이 따로 발급받을 필요가 없다.

알아둘 점
- 갱신: 기준일 다음 영업일 13시 이후. 오늘·어제 종가는 아직 없을 수 있다.
- 수정주가가 아니다(원 종가). 액면분할 전후를 이으면 끊긴다.
- beginBasDt 는 '이상', endBasDt 는 '미만'이라 종료일을 포함하려면 하루를 더해 보낸다.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

import httpx

URL = "https://apis.data.go.kr/1160100/GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2"
SOURCE = "공공데이터포털 금융위원회_주식시세정보(getStockPriceInfo_V2)"
PAGE_SIZE = 1000
MAX_PAGES = 10  # 전 종목 하루치가 약 2,800행이라 이 정도면 충분하다

# 응답 필드 뜻. 결과에 함께 실어 보내 모델이 필드명을 추측하지 않게 한다.
FIELDS = {
    "basDt": "기준일자",
    "srtnCd": "단축코드",
    "isinCd": "ISIN",
    "itmsNm": "종목명",
    "mrktCtg": "시장구분",
    "clpr": "종가",
    "vs": "전일대비",
    "fltRt": "등락률(%)",
    "mkp": "시가",
    "hipr": "고가",
    "lopr": "저가",
    "trqu": "거래량",
    "trPrc": "거래대금(원)",
    "lstgStCnt": "상장주식수",
    "mrktTotAmt": "시가총액(원)",
}


class StockError(RuntimeError):
    """시세 API가 오류를 돌려줬거나 설정이 빠진 경우."""


def _key() -> str:
    value = (os.environ.get("DATA_GO_KR_KEY") or "").strip()
    if not value or (value.startswith("${") and value.endswith("}")):
        raise StockError(
            "시세 조회용 인증키가 없습니다. 서버 환경변수 DATA_GO_KR_KEY 에 "
            "공공데이터포털 '금융위원회_주식시세정보'의 일반 인증키(Decoding)를 넣으세요."
        )
    return value


def _ymd(value: str, name: str) -> str:
    digits = value.replace("-", "").replace(".", "").strip()
    try:
        datetime.strptime(digits, "%Y%m%d")
    except ValueError as exc:
        raise StockError(f"{name}는 YYYYMMDD 형식이어야 합니다: {value!r}") from exc
    return digits


def next_day(yyyymmdd: str) -> str:
    return (datetime.strptime(yyyymmdd, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")


def fetch(**params: Any) -> dict[str, Any]:
    """조건에 맞는 행을 페이지를 넘겨 가며 전부 모은다. 행은 API 원문 그대로 둔다."""
    query = {k: v for k, v in params.items() if v not in (None, "")}
    rows: list[dict[str, Any]] = []
    total = 0
    for page in range(1, MAX_PAGES + 1):
        try:
            response = httpx.get(
                URL,
                params={"serviceKey": _key(), "resultType": "json", "numOfRows": PAGE_SIZE, "pageNo": page, **query},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            # 예외 문자열에 요청 URL(=인증키)이 들어가므로 그대로 올리지 않는다
            raise StockError(f"시세 API에 연결하지 못했습니다: {type(exc).__name__}") from None
        if response.status_code != 200:
            # raise_for_status()의 메시지에는 serviceKey가 든 URL이 통째로 찍힌다 — 쓰지 않는다
            hint = {
                401: "인증키가 틀렸습니다.",
                403: "인증키가 이 API에 아직 승인·동기화되지 않았습니다. 활용신청 직후라면 1~2시간 뒤 다시 시도하고, "
                "공공데이터포털 마이페이지에서 '금융위원회_주식시세정보'가 활용 목록에 있는지 확인하세요.",
                429: "호출 한도를 넘었습니다.",
            }.get(response.status_code, "")
            raise StockError(f"시세 API HTTP {response.status_code}. {hint} 응답: {response.text[:200]}")
        try:
            data = response.json()
        except ValueError as exc:
            # 인증키 오류 등은 JSON이 아니라 XML로 온다
            raise StockError(f"시세 API가 JSON이 아닌 응답을 보냈습니다(인증키 확인): {response.text[:300]}") from exc
        header = data.get("response", {}).get("header", {})
        if header.get("resultCode") != "00":
            raise StockError(f"시세 API 오류 [{header.get('resultCode')}] {header.get('resultMsg', '')}")
        body = data["response"].get("body", {})
        total = int(body.get("totalCount") or 0)
        items = (body.get("items") or {}).get("item") or []
        if isinstance(items, dict):
            items = [items]
        rows.extend(items)
        if not items or len(rows) >= total:
            break
    rows.sort(key=lambda row: (row.get("basDt", ""), row.get("srtnCd", "")))
    return {
        "source": SOURCE,
        "request": query,
        "count": len(rows),
        "total_count": total,
        "fields": FIELDS,
        "note": "원 종가 기준(수정주가 아님). 기준일 다음 영업일 13시 이후 갱신. 금액 단위는 원.",
        "rows": rows,
    }


def price_history(stock_code: str, start_date: str, end_date: str) -> dict[str, Any]:
    code = stock_code.strip().removeprefix("A")
    if not (len(code) == 6 and code.isalnum()):
        raise StockError(f"종목코드는 6자리 단축코드여야 합니다(예: 263750): {stock_code!r}")
    start = _ymd(start_date, "start_date")
    end = _ymd(end_date, "end_date")
    if start > end:
        raise StockError(f"start_date({start})가 end_date({end})보다 늦습니다.")
    result = fetch(likeSrtnCd=code, beginBasDt=start, endBasDt=next_day(end))
    # likeSrtnCd 는 '포함' 검색이라 다른 종목이 섞일 수 있다
    result["rows"] = [row for row in result["rows"] if row.get("srtnCd") == code]
    result["count"] = len(result["rows"])
    return result


def market_snapshot(
    base_date: str,
    market: str | None = None,
    stock_codes: list[str] | None = None,
    top: int = 30,
) -> dict[str, Any]:
    """하루치 전 종목 응답은 1MB가 넘어 그대로 돌려주면 대화가 넘친다.
    종목을 지정하면 그 종목만, 아니면 시가총액 상위 top개만 돌려준다(행은 원문 그대로)."""
    day = _ymd(base_date, "base_date")
    market_code = (market or "").strip().upper() or None
    if market_code and market_code not in ("KOSPI", "KOSDAQ", "KONEX"):
        raise StockError("market은 KOSPI, KOSDAQ, KONEX 중 하나이거나 비워 둡니다.")
    result = fetch(basDt=day, mrktCls=market_code)
    rows = result["rows"]
    universe = len(rows)
    total_cap = sum(int(row.get("mrktTotAmt") or 0) for row in rows)
    if stock_codes:
        wanted = {code.strip().removeprefix("A") for code in stock_codes}
        rows = [row for row in rows if row.get("srtnCd") in wanted]
        result["missing_codes"] = sorted(wanted - {row.get("srtnCd") for row in rows})
    else:
        limit = max(1, min(int(top), 300))
        rows = sorted(rows, key=lambda row: int(row.get("mrktTotAmt") or 0), reverse=True)[:limit]
    result["rows"] = rows
    result["count"] = len(rows)
    result["universe_count"] = universe
    result["universe_mrktTotAmt_sum"] = total_cap  # 조회 범위(시장) 전체 시가총액 합, 원
    return result
