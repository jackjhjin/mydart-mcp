"""OpenDART(전자공시) MCP 서버."""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import attachments, catalog, dart, extract, stock

mcp = MCPServer("mydart")

# 재무지표 구분 코드
IDX_CL_CODES = {
    "M210000": "수익성지표",
    "M220000": "안정성지표",
    "M230000": "성장성지표",
    "M240000": "활동성지표",
}
IDX_CL_ALIASES = {name: code for code, name in IDX_CL_CODES.items()}


def _num(value: str | None) -> int | None:
    if not value or value.strip() in ("-", ""):
        return None
    try:
        return int(value.replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def _with_items(description: str, category: str) -> str:
    """도구 설명 끝에 해당 카테고리의 항목명을 붙인다 (카탈로그와 항상 일치)."""
    return f"{description}\n\n사용 가능한 item ({len(catalog.by_category(category))}개): {catalog.item_names(category)}"


# 연도를 안 적어주면 "재무제표는 보통 작년 걸 본다"는 관행대로 한 해를 빼고 부르는 일이
# 있다. 그러면 조회는 성공하고 숫자도 그럴듯해서, 틀린 해를 봤다는 걸 알기 어렵다.
_YEAR_RULE = """
[연도 규칙]
- 사용자가 말한 연도를 그대로 넣는다. 관행적으로 작년으로 바꾸지 않는다.
  "2025년 실적"이면 bsns_year는 2025다.
- 연도를 안 알려줬으면 추측하지 말고 사용자에게 묻는다. 오늘 날짜로 넣지 않는다.
- 아직 안 나온 보고서면 빈 결과가 돌아온다. 그때 연도를 낮춰 다시 부르고,
  답할 때 어느 해 자료인지 밝힌다."""


def _year_rule(fn):
    """연도를 받는 도구의 설명 끝에 연도 규칙을 붙인다. @mcp.tool() 아래에 놓는다."""
    fn.__doc__ = f"{(fn.__doc__ or '').rstrip()}\n{_YEAR_RULE}"
    return fn


def _rows(endpoint: catalog.Endpoint, data: dict[str, Any]) -> dict[str, Any]:
    rows = data.get("list", [])
    return {"item": endpoint.name, "endpoint": endpoint.id, "count": len(rows), "rows": rows}


MAX_QUERIES = 200


def _lookup(
    corps: list[dict[str, str]], query: str, listed_only: bool, limit: int
) -> tuple[list[dict[str, str]], bool]:
    """한 건 검색. 상장사에 없으면 비상장까지 넓혀 한 번 더 본다."""
    matches = dart.search_corp_codes(corps, query, listed_only=listed_only, limit=limit)
    if matches or not listed_only:
        return matches, False
    widened = dart.search_corp_codes(corps, query, listed_only=False, limit=limit)
    return widened, bool(widened)


@mcp.tool()
def search_company(
    query: str | list[str], listed_only: bool = True, limit: int = 10
) -> dict[str, Any]:
    """회사명이나 6자리 종목코드로 DART 고유번호(corp_code)를 찾는다.

    다른 모든 도구는 corp_code를 요구하므로 보통 여기서 시작한다.

    **여러 회사는 목록으로 한 번에 넣는다.** 다른 곳에서 받은 종목코드 목록을
    corp_code로 바꿔 compare_financials나 get_periodic_report_item에 넘길 때,
    회사마다 따로 부르지 않는다. 조회는 내려받아 둔 목록에서 하므로 몇 개를 찾든
    비용이 같고, 나눠 부르면 중간에 빠뜨리기만 쉽다.

    못 찾은 것은 not_found에 남는다 — 목록에서 조용히 사라지지 않는다.

    Args:
        query: 회사명 일부 또는 6자리 종목코드. 하나면 문자열, 여럿이면 목록.
            예: "삼성전자" 또는 ["005930", "000660", "042700"]
        listed_only: True면 상장사만 검색한다.
        limit: 검색어 하나당 최대 결과 수.
    """
    corps = dart.load_corp_codes()

    if isinstance(query, str):
        matches, widened = _lookup(corps, query, listed_only, limit)
        result = {"query": query, "count": len(matches), "companies": matches}
        if widened:
            result["note"] = "상장사에 없어 비상장까지 넓혀 찾았습니다."
        return result

    if not query:
        raise dart.DartError("query가 비어 있습니다.")
    if len(query) > MAX_QUERIES:
        raise dart.DartError(
            f"한 번에 {MAX_QUERIES}개까지 찾습니다(요청 {len(query)}개). 나눠서 부르세요."
        )

    companies: list[dict[str, Any]] = []
    not_found: list[str] = []
    widened_queries: list[str] = []
    seen: set[str] = set()

    for one in query:
        matches, widened = _lookup(corps, one, listed_only, limit)
        if not matches:
            not_found.append(one)
            continue
        if widened:
            widened_queries.append(one)
        for corp in matches:
            if corp["corp_code"] in seen:
                continue
            seen.add(corp["corp_code"])
            # 회사명으로 찾으면 여러 건이 걸리므로, 어느 검색어에서 나왔는지 붙인다
            companies.append({"query": one, **corp})

    result: dict[str, Any] = {
        "queries": len(query),
        "count": len(companies),
        "companies": companies,
    }
    if not_found:
        result["not_found"] = not_found
    if widened_queries:
        result["note"] = f"상장사에 없어 비상장까지 넓혀 찾았습니다: {', '.join(widened_queries)}"
    return result


@mcp.tool()
def get_company_profile(corp_code: str) -> dict[str, Any]:
    """기업개황을 조회한다 (대표자, 법인구분, 설립일, 결산월, 주소, 홈페이지 등).

    Args:
        corp_code: DART 고유번호 8자리. search_company로 먼저 찾는다.
    """
    data = dart.get_json("company.json", corp_code=corp_code)
    data.pop("status", None)
    data.pop("message", None)
    return data


@mcp.tool()
def search_disclosures(
    corp_code: str | None = None,
    bgn_de: str | None = None,
    end_de: str | None = None,
    pblntf_ty: str | None = None,
    last_reprt_only: bool = True,
    page_no: int = 1,
    page_count: int = 20,
) -> dict[str, Any]:
    """공시를 검색한다. 특정 회사의 공시 목록이나 기간별 전체 공시를 볼 때 쓴다.

    Args:
        corp_code: DART 고유번호 8자리. 생략하면 전체 회사가 대상이다.
        bgn_de: 검색 시작일 YYYYMMDD. 생략하면 종료일 기준 최근 공시.
        end_de: 검색 종료일 YYYYMMDD.
        pblntf_ty: 공시유형 (A=정기공시, B=주요사항보고, C=발행공시, D=지분공시,
            E=기타공시, F=외부감사관련, G=펀드공시, H=자산유동화, I=거래소공시, J=공정위공시)
        last_reprt_only: True면 정정된 공시는 최종 보고서만 보여준다.
        page_no: 페이지 번호.
        page_count: 페이지당 건수 (최대 100).
    """
    data = dart.get_json(
        "list.json",
        corp_code=corp_code,
        bgn_de=bgn_de,
        end_de=end_de,
        pblntf_ty=pblntf_ty,
        last_reprt_at="Y" if last_reprt_only else "N",
        page_no=page_no,
        page_count=min(page_count, 100),
    )
    return {
        "total_count": data.get("total_count", 0),
        "page_no": data.get("page_no", page_no),
        "total_page": data.get("total_page", 0),
        "disclosures": [
            {
                "rcept_no": item.get("rcept_no"),
                "corp_name": item.get("corp_name"),
                "corp_code": item.get("corp_code"),
                "stock_code": item.get("stock_code"),
                "report_nm": item.get("report_nm"),
                "rcept_dt": item.get("rcept_dt"),
                "flr_nm": item.get("flr_nm"),
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={item.get('rcept_no')}",
            }
            for item in data.get("list", [])
        ],
    }


@mcp.tool()
def get_disclosure_document(rcept_no: str, max_chars: int = 20000, offset: int = 0) -> dict[str, Any]:
    """공시 원문을 텍스트로 가져온다.

    원문은 수십만 자에 달할 수 있어 잘라서 반환한다. truncated가 True면
    next_offset을 넣어 다시 호출해 이어서 읽는다.

    Args:
        rcept_no: 접수번호 14자리. search_disclosures 결과의 rcept_no.
        max_chars: 이번 호출에서 반환할 최대 글자 수.
        offset: 읽기 시작할 위치.
    """
    files = dart.get_zip("document.xml", rcept_no=rcept_no)
    text = "\n\n".join(dart.document_to_text(content) for content in files.values())
    chunk = text[offset : offset + max_chars]
    end = offset + len(chunk)
    return {
        "rcept_no": rcept_no,
        "total_chars": len(text),
        "offset": offset,
        "truncated": end < len(text),
        "next_offset": end if end < len(text) else None,
        "text": chunk,
    }


@mcp.tool()
def list_attachments(rcept_no: str) -> dict[str, Any]:
    """공시에 딸린 첨부파일 목록을 본다.

    감사보고서, 외부평가기관 의견서, 계약서처럼 정작 중요한 내용은 본문이 아니라
    첨부에 있는 경우가 많다. 여기서 파일을 고른 뒤 read_attachment로 읽는다.

    본문뿐 아니라 뷰어의 '첨부문서'로 걸린 별도 문서까지 전부 훑는다. 각 파일의
    document 필드가 어느 문서에서 나왔는지 알려준다.

    첨부는 OpenDART 오픈API가 아니라 DART 뷰어를 통해 가져온다. 사용자가 지목한
    공시 하나에만 쓰고, 공시 목록을 돌며 첨부를 긁어모으는 데 쓰지 않는다.

    Args:
        rcept_no: 접수번호 14자리. search_disclosures 결과의 rcept_no.
    """
    return attachments.list_attachments(rcept_no)


@mcp.tool()
def read_attachment(
    rcept_no: str,
    filename: str | None = None,
    index: int | None = None,
    max_chars: int = 20000,
    offset: int = 0,
) -> dict[str, Any]:
    """공시 첨부파일 하나를 내려받아 텍스트로 읽는다 (HWP, HWPX, PDF, 텍스트).

    긴 문서는 잘라서 반환한다. truncated가 True면 next_offset으로 다시 호출해 이어 읽는다.

    Args:
        rcept_no: 접수번호 14자리.
        filename: 읽을 파일명. list_attachments의 filename과 부분일치도 된다.
        index: 파일명 대신 목록의 index로 지정 (filename이 우선).
        max_chars: 이번 호출에서 반환할 최대 글자 수.
        offset: 읽기 시작할 위치.
    """
    with attachments.session() as client:
        listed = attachments.list_attachments(rcept_no, client=client)
        target = _pick_attachment(listed, rcept_no, filename, index)
        raw = attachments.download(
            target["download_url"],
            referer=target.get("download_page_url"),
            client=client,
        )

    text = extract.extract(raw, target["format"])
    chunk = text[offset : offset + max_chars]
    end = offset + len(chunk)
    return {
        "rcept_no": rcept_no,
        "filename": target["filename"],
        "format": target["format"],
        "total_chars": len(text),
        "offset": offset,
        "truncated": end < len(text),
        "next_offset": end if end < len(text) else None,
        "text": chunk,
    }


def _pick_attachment(
    listed: dict[str, Any], rcept_no: str, filename: str | None, index: int | None
) -> dict[str, Any]:
    files = listed["attachments"]
    if not files:
        raise attachments.AttachmentError(
            listed.get("note") or f"공시 {rcept_no}에 첨부파일이 없습니다."
        )

    if filename:
        matches = [f for f in files if f["filename"] == filename] or [
            f for f in files if filename in f["filename"]
        ]
        if not matches:
            raise attachments.AttachmentError(
                f"'{filename}'과 맞는 첨부가 없습니다. "
                f"목록: {', '.join(f['filename'] for f in files)}"
            )
        if len(matches) > 1:
            raise attachments.AttachmentError(
                f"'{filename}'에 여러 파일이 걸립니다: {', '.join(f['filename'] for f in matches)}"
            )
        target = matches[0]
    elif index is not None:
        if not 0 <= index < len(files):
            raise attachments.AttachmentError(
                f"index는 0~{len(files) - 1} 범위여야 합니다 (첨부 {len(files)}개)."
            )
        target = files[index]
    else:
        raise attachments.AttachmentError("filename 또는 index 중 하나는 지정해야 합니다.")
    return target


@mcp.tool()
@_year_rule
def get_financial_statements(
    corp_code: str,
    bsns_year: str,
    reprt_code: str = "11011",
    fs_div: str = "CFS",
    sj_div: str | None = None,
) -> dict[str, Any]:
    """단일 회사의 전체 재무제표를 계정과목 단위로 조회한다 (당기/전기/전전기 3개년).

    Args:
        corp_code: DART 고유번호 8자리.
        bsns_year: 사업연도 4자리 (2015년 이후만 제공).
        reprt_code: 11011=사업보고서, 11012=반기, 11013=1분기, 11014=3분기.
        fs_div: CFS=연결재무제표, OFS=별도재무제표.
        sj_div: 특정 재무제표만 볼 때 지정 (BS=재무상태표, IS=손익계산서,
            CIS=포괄손익계산서, CF=현금흐름표, SCE=자본변동표).
    """
    data = dart.get_json(
        "fnlttSinglAcntAll.json",
        corp_code=corp_code,
        bsns_year=bsns_year,
        reprt_code=dart.normalize_reprt_code(reprt_code),
        fs_div=fs_div.upper(),
    )
    rows = data.get("list", [])
    if sj_div:
        rows = [row for row in rows if row.get("sj_div") == sj_div.upper()]
    return {
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
        "fs_div": fs_div.upper(),
        "currency": rows[0].get("currency") if rows else None,
        "accounts": [
            {
                "sj": row.get("sj_nm"),
                "account": row.get("account_nm"),
                "detail": row.get("account_detail") if row.get("account_detail") != "-" else None,
                "current": _num(row.get("thstrm_amount")),
                "prior": _num(row.get("frmtrm_amount")),
                "prior2": _num(row.get("bfefrmtrm_amount")),
            }
            for row in rows
        ],
    }


@mcp.tool()
@_year_rule
def compare_financials(
    corp_codes: list[str],
    bsns_year: str,
    reprt_code: str = "11011",
) -> dict[str, Any]:
    """여러 회사의 주요계정(매출액, 영업이익, 당기순이익, 자산, 부채, 자본)을 나란히 비교한다.

    Args:
        corp_codes: DART 고유번호 목록 (최대 10개).
        bsns_year: 사업연도 4자리.
        reprt_code: 11011=사업보고서, 11012=반기, 11013=1분기, 11014=3분기.
    """
    if not corp_codes:
        raise dart.DartError("corp_codes가 비어 있습니다.")
    if len(corp_codes) > 10:
        raise dart.DartError("한 번에 최대 10개 회사까지 비교할 수 있습니다.")

    data = dart.get_json(
        "fnlttMultiAcnt.json",
        corp_code=",".join(corp_codes),
        bsns_year=bsns_year,
        reprt_code=dart.normalize_reprt_code(reprt_code),
    )
    names = {corp["corp_code"]: corp["corp_name"] for corp in dart.load_corp_codes()}

    companies: dict[str, dict[str, Any]] = {}
    for row in data.get("list", []):
        code = row.get("corp_code")
        company = companies.setdefault(
            code,
            {"corp_code": code, "corp_name": names.get(code), "fs_div": row.get("fs_nm"), "accounts": {}},
        )
        company["accounts"][row.get("account_nm")] = {
            "current": _num(row.get("thstrm_amount")),
            "prior": _num(row.get("frmtrm_amount")),
            "prior2": _num(row.get("bfefrmtrm_amount")),
        }

    return {
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
        "companies": list(companies.values()),
    }


_PERIODIC_DESC = _with_items(
    """정기보고서(사업·반기·분기보고서)에 실린 항목별 상세 정보를 조회한다.
지분구조, 임원·직원, 보수, 감사인, 미상환 채권 잔액, 자금 사용내역 등을 다룬다.

**여러 회사와 여러 해를 한 번에 조회한다.** "최근 5년"이면 bsns_years에 5개 연도를,
"이 회사들 중"이면 corp_codes에 전부 넣어 **한 번만** 호출한다 — 회사나 연도마다
따로 부르지 않는다.

후보군을 놓고 훑는 용도로 쓴다. 예: 피어 20곳의 감사인·감사의견 비교,
후보군 중 최대주주가 바뀐 곳 찾기, 여러 회사 임원 보수 비교.

전체 상장사를 훑는 용도로는 쓸 수 없다. OpenDART가 회사를 지정해야만 답하기 때문에
회사 목록을 먼저 알아야 한다.

Args:
    corp_codes: DART 고유번호 8자리 목록. search_company로 먼저 찾는다.
        회사가 하나여도 목록으로 넣는다. 예: ["00126380"]
    bsns_years: 사업연도 목록 (2015년 이후). 예: ["2021","2022","2023","2024","2025"]
    item: 아래 항목명 중 하나 (한글명 또는 엔드포인트 id).
    reprt_code: 11011=사업보고서, 11012=반기, 11013=1분기, 11014=3분기.
"""
    + _YEAR_RULE,
    "periodic_report",
)


# 회사 하나에 연도 하나가 요청 하나다. 원격(서버리스)으로 띄우면 함수 실행시간
# 제한이 있어, 조회가 길어지면 답을 받기 전에 끊긴다. 끊기고 나서 알기보다
# 부르기 전에 나눠 달라고 하는 편이 낫다.
MAX_PERIODIC_LOOKUPS = 60


@mcp.tool(description=_PERIODIC_DESC)
def get_periodic_report_item(
    corp_codes: list[str],
    bsns_years: list[str],
    item: str,
    reprt_code: str = "11011",
) -> dict[str, Any]:
    if not corp_codes:
        raise dart.DartError("corp_codes가 비어 있습니다.")
    if not bsns_years:
        raise dart.DartError("bsns_years가 비어 있습니다.")

    lookups = len(corp_codes) * len(bsns_years)
    if lookups > MAX_PERIODIC_LOOKUPS:
        raise dart.DartError(
            f"회사 {len(corp_codes)}곳 × {len(bsns_years)}개 연도 = {lookups}회 조회는 한 번에 "
            f"처리하기에 많습니다(상한 {MAX_PERIODIC_LOOKUPS}회). 회사나 연도를 나눠 부르세요."
        )

    endpoint = catalog.resolve("periodic_report", item)
    code = dart.normalize_reprt_code(reprt_code)
    names = {c["corp_code"]: c["corp_name"] for c in dart.load_corp_codes()}

    rows: list[dict[str, Any]] = []
    empty: list[dict[str, str]] = []
    failed: dict[str, str] = {}
    for corp_code in corp_codes:
        name = names.get(corp_code, "")
        for year in bsns_years:
            try:
                data = dart.get_json(
                    f"{endpoint.id}.json", corp_code=corp_code, bsns_year=year, reprt_code=code
                )
            except dart.DartError as exc:
                # 한 회사·한 해가 실패해도 나머지는 계속 조회한다
                failed[f"{name or corp_code} {year}"] = str(exc)
                continue
            found = data.get("list", [])
            if found:
                # 여러 회사를 섞어 돌려주므로 어느 회사 것인지 행마다 붙인다.
                rows.extend(
                    {"corp_code": corp_code, "corp_name": name, "bsns_year": year, **row}
                    for row in found
                )
            else:
                empty.append({"corp_code": corp_code, "corp_name": name, "bsns_year": year})

    result: dict[str, Any] = {
        "item": endpoint.name,
        "endpoint": endpoint.id,
        "companies": len(corp_codes),
        "count": len(rows),
        "rows": rows,
    }
    if empty:
        result["empty"] = empty
    if failed:
        result["failed"] = failed

    unlisted = [c for c in corp_codes if not dart.is_listed(c)]
    if unlisted and not rows:
        # 비상장사도 사업보고서 제출대상이면 이 항목들이 나온다. 비었다고 단정하지 말고
        # 이 회사가 실제로 무슨 서류를 내는지 확인하도록 안내한다.
        result["note"] = (
            f"비상장사가 {len(unlisted)}곳 있습니다. 사업보고서 제출대상법인이면 조회되지만, "
            "감사보고서만 제출하는 회사면 이 항목이 없습니다. search_disclosures로 어떤 "
            "보고서를 내는지 확인하고, 감사보고서뿐이라면 list_attachments로 첨부를 읽으세요."
        )
    return result


_MAJOR_EVENT_DESC = _with_items(
    """주요사항보고서 항목별 상세 정보를 조회한다.
증자·감자, 합병·분할, 사채 발행, 자기주식 취득·처분, 영업 양수도, 소송, 부도·회생 등
회사에 중대한 영향을 주는 결정과 사건을 다룬다.

Args:
    corp_code: DART 고유번호 8자리.
    item: 아래 항목명 중 하나 (한글명 또는 엔드포인트 id).
    bgn_de: 검색 시작 접수일자 YYYYMMDD (2015년 이후).
    end_de: 검색 종료 접수일자 YYYYMMDD.""",
    "major_event",
)


@mcp.tool(description=_MAJOR_EVENT_DESC)
def get_major_event(corp_code: str, item: str, bgn_de: str, end_de: str) -> dict[str, Any]:
    endpoint = catalog.resolve("major_event", item)
    data = dart.get_json(f"{endpoint.id}.json", corp_code=corp_code, bgn_de=bgn_de, end_de=end_de)
    return _rows(endpoint, data)


_REGISTRATION_DESC = _with_items(
    """증권신고서 항목별 요약 정보를 조회한다. 공모 발행 조건과 일정을 확인할 때 쓴다.

Args:
    corp_code: DART 고유번호 8자리.
    item: 아래 항목명 중 하나 (한글명 또는 엔드포인트 id).
    bgn_de: 검색 시작 접수일자 YYYYMMDD (2015년 이후).
    end_de: 검색 종료 접수일자 YYYYMMDD.""",
    "securities_registration",
)


@mcp.tool(description=_REGISTRATION_DESC)
def get_securities_registration(
    corp_code: str, item: str, bgn_de: str, end_de: str
) -> dict[str, Any]:
    endpoint = catalog.resolve("securities_registration", item)
    data = dart.get_json(f"{endpoint.id}.json", corp_code=corp_code, bgn_de=bgn_de, end_de=end_de)
    return _rows(endpoint, data)


_SHAREHOLDING_DESC = _with_items(
    """지분공시를 조회한다. 5% 이상 대량보유 변동과 임원·주요주주의 소유주식 변동을 다룬다.

Args:
    corp_code: DART 고유번호 8자리.
    item: 아래 항목명 중 하나 (한글명 또는 엔드포인트 id).""",
    "shareholding",
)


@mcp.tool(description=_SHAREHOLDING_DESC)
def get_shareholding(corp_code: str, item: str) -> dict[str, Any]:
    endpoint = catalog.resolve("shareholding", item)
    data = dart.get_json(f"{endpoint.id}.json", corp_code=corp_code)
    return _rows(endpoint, data)


@mcp.tool()
@_year_rule
def get_financial_indicators(
    corp_codes: list[str],
    bsns_year: str,
    idx_cl_code: str,
    reprt_code: str = "11011",
) -> dict[str, Any]:
    """상장사 주요 재무지표(수익성·안정성·성장성·활동성)를 조회한다.

    재무제표 계정 원본이 아니라 OpenDART가 계산해 둔 비율 지표다.

    Args:
        corp_codes: DART 고유번호 목록. 1개면 단일회사, 2개 이상이면 다중회사로 조회한다.
        bsns_year: 사업연도 4자리 (2023년 이후 제공).
        idx_cl_code: 수익성지표(M210000), 안정성지표(M220000), 성장성지표(M230000),
            활동성지표(M240000). 한글명으로 넣어도 된다.
        reprt_code: 11011=사업보고서, 11012=반기, 11013=1분기, 11014=3분기.
    """
    if not corp_codes:
        raise dart.DartError("corp_codes가 비어 있습니다.")
    code = IDX_CL_ALIASES.get(idx_cl_code.strip(), idx_cl_code.strip().upper())
    if code not in IDX_CL_CODES:
        raise dart.DartError(
            f"알 수 없는 지표 구분입니다: {idx_cl_code} "
            f"(사용 가능: {', '.join(f'{n}({c})' for c, n in IDX_CL_CODES.items())})"
        )
    single = len(corp_codes) == 1
    data = dart.get_json(
        "fnlttSinglIndx.json" if single else "fnlttCmpnyIndx.json",
        corp_code=corp_codes[0] if single else ",".join(corp_codes),
        bsns_year=bsns_year,
        reprt_code=dart.normalize_reprt_code(reprt_code),
        idx_cl_code=code,
    )
    return {
        "idx_cl_code": code,
        "idx_cl_name": IDX_CL_CODES[code],
        "bsns_year": bsns_year,
        "count": len(data.get("list", [])),
        "rows": data.get("list", []),
    }


@mcp.tool()
def list_dart_apis(category: str | None = None, query: str | None = None) -> dict[str, Any]:
    """OpenDART가 제공하는 83개 오픈API 전체 목록을 훑는다.

    전용 도구로 안 잡히는 정보를 찾을 때 여기서 엔드포인트를 확인한 뒤
    call_dart_api로 직접 호출한다.

    Args:
        category: disclosure, periodic_report, finance, shareholding,
            major_event, securities_registration 중 하나. 생략하면 전체.
        query: 한글 항목명이나 엔드포인트 id에 대한 부분 검색어.
    """
    endpoints = list(catalog.ENDPOINTS.values())
    if category:
        if category not in catalog.CATEGORY_NAMES:
            raise dart.DartError(
                f"알 수 없는 카테고리입니다: {category} "
                f"(사용 가능: {', '.join(catalog.CATEGORY_NAMES)})"
            )
        endpoints = [e for e in endpoints if e.category == category]
    if query:
        keyword = query.strip().lower()
        endpoints = [e for e in endpoints if keyword in e.name.lower() or keyword in e.id.lower()]
    return {
        "categories": catalog.CATEGORY_NAMES,
        "count": len(endpoints),
        "apis": [
            {
                "endpoint": e.id,
                "name": e.name,
                "category": e.category,
                "params": list(e.params),
                "format": e.ext,
            }
            for e in endpoints
        ],
    }


@mcp.tool()
def call_dart_api(endpoint: str, params: dict[str, str]) -> dict[str, Any]:
    """OpenDART JSON API를 엔드포인트 이름으로 직접 호출한다.

    전용 도구가 없는 API를 쓰기 위한 창구다. 엔드포인트 이름은 list_dart_apis로 확인한다.
    인증키(crtfc_key)는 서버가 붙이므로 넣지 않는다.

    Args:
        endpoint: 엔드포인트 이름 (예: "irdsSttus"). ".json"은 붙여도 되고 안 붙여도 된다.
        params: 해당 API의 요청 파라미터.
    """
    name = endpoint.strip().removesuffix(".json").removesuffix(".xml")
    known = catalog.ENDPOINTS.get(name)
    if known is None:
        raise dart.DartError(
            f"'{endpoint}'는 OpenDART 엔드포인트가 아닙니다. list_dart_apis로 목록을 확인하세요."
        )
    if known.ext != "json":
        raise dart.DartError(
            f"{known.name}({known.id})은 ZIP/XML로 응답합니다. "
            "공시 원문은 get_disclosure_document를, 고유번호는 search_company를 쓰세요."
        )
    data = dart.get_json(f"{known.id}.json", **params)
    data.pop("status", None)
    data.pop("message", None)
    return {"endpoint": known.id, "name": known.name, **data}


@mcp.tool()
def get_stock_price(stock_code: str, start_date: str, end_date: str) -> dict[str, Any]:
    """국내 상장주식의 일별 시세를 기간으로 조회한다(공공데이터포털 금융위원회_주식시세정보).

    종가·시가·고가·저가·거래량·거래대금·상장주식수·시가총액을 하루 한 행으로 돌려준다.
    OpenDART에는 주가가 없으므로 시가총액·주가 추이·특정일 종가는 이 도구로 본다.

    - 원 종가 기준이다(수정주가 아님). 액면분할이 낀 기간은 끊겨 보인다.
    - 기준일 다음 영업일 13시 이후 갱신되어 오늘·어제 값은 아직 없을 수 있다.
    - 휴장일은 행이 없다. 특정일 값이 없으면 그 직전 영업일 행을 쓰고 날짜를 밝힌다.
    - rows는 API 원문 그대로다. 금액 단위는 원.

    Args:
        stock_code: 6자리 단축 종목코드 (예: "263750"). corp_code(8자리)가 아니다.
        start_date: 시작일 YYYYMMDD (포함)
        end_date: 종료일 YYYYMMDD (포함)
    """
    return stock.price_history(stock_code, start_date, end_date)


@mcp.tool()
def get_market_snapshot(base_date: str, market: str | None = None) -> dict[str, Any]:
    """특정 영업일의 전 종목 시세·시가총액을 한 번에 조회한다(공공데이터포털 금융위원회_주식시세정보).

    시가총액 순위, 업종·그룹 합산, 여러 종목의 같은 날 종가 비교에 쓴다.
    휴장일이면 행이 0개다 — 직전 영업일로 다시 부른다.

    Args:
        base_date: 기준일 YYYYMMDD
        market: "KOSPI", "KOSDAQ", "KONEX" 중 하나. 생략하면 전체(약 2,800종목).
    """
    return stock.market_snapshot(base_date, market)


def _prepare_stdio() -> None:
    """stdout이 파이프에 물리면 파이썬은 블록 버퍼링을 건다. 그러면 initialize 응답이
    버퍼에 갇혀 클라이언트에 닿지 않고, 클라이언트는 60초를 기다리다 연결을 포기한다.
    (Windows에서 실제로 이 증상이 났다.)

    기본 인코딩도 문제다. 한국어 Windows는 cp949라 도구 설명의 한글이 깨진다.
    """
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    if getattr(sys.stdout, "reconfigure", None) is not None:
        sys.stdout.reconfigure(line_buffering=True)


def main() -> None:
    dart.hide_api_key_in_logs()
    _prepare_stdio()
    mcp.run()


if __name__ == "__main__":
    main()
