"""외부 HTTP 요청 호스트 가드 — 리다이렉트 종착지까지 화이트리스트로 강제한다.

수집 대상 URL은 전부 소스 상수지만, 수집기들은 `follow_redirects=True`로 요청한다.
상대 서버(또는 중간자)가 302로 사설망 주소(`http://127.0.0.1:8000/`, `http://169.254.169.254/`)를
돌려주면 httpx는 그 주소로 다시 요청하고 응답 본문을 그대로 파서에 넘긴다.
즉 "URL이 상수"라는 사실만으로는 SSRF가 막히지 않는다. (Category 1: SSRF)

이 모듈은 httpx의 `event_hooks={"response": [...]}`에 붙어, **응답을 읽기 전에**
그 응답이 실제로 도착한 URL의 스킴(https)과 호스트(허용 목록)를 검사한다.
httpx는 리다이렉트 체인의 모든 응답에 대해 response 훅을 호출하고, 훅이 예외를 던지면
응답 스트림을 닫은 뒤 예외를 전파하므로 차단된 응답의 본문은 절대 파싱되지 않는다.

허용 목록은 각 수집 모듈이 자신의 URL 상수에서 파생시킨다(`hosts_from_urls`).
상수에 없는 호스트는 서브도메인이라도 허용하지 않는다(정확히 일치만 허용).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine, Iterable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ALLOWED_SCHEME = "https"


class BlockedHostError(httpx.HTTPError):
    """허용 목록 밖 호스트 또는 https가 아닌 스킴으로 도착한 응답.

    httpx.HTTPError를 상속한다. 수집기들의 기존 `except httpx.HTTPError` 경로가
    그대로 잡아 개별 소스 실패로 격리·로깅하도록 하기 위함이다. (Category 4)
    """


def hosts_from_urls(urls: Iterable[str]) -> frozenset[str]:
    """URL 상수 목록 → 허용 호스트 집합.

    [SECURE] 허용 호스트를 소스 상수에서만 파생 - 임의 호스트 접근 방지 (Category 1: SSRF).
    스킴이 https가 아닌 상수는 애초에 허용 목록에 넣지 않는다.
    """
    hosts: set[str] = set()
    for raw in urls:
        try:
            url = httpx.URL(str(raw))
        except (httpx.InvalidURL, TypeError, ValueError) as e:
            # [SECURE] 잘못된 상수를 조용히 넘기지 않는다 (Category 4)
            logger.warning("[HttpGuard] 허용 목록 URL 파싱 실패: %s", e)
            continue
        if url.scheme != _ALLOWED_SCHEME:
            logger.warning("[HttpGuard] https가 아닌 상수는 허용 목록에서 제외: %s", url.scheme)
            continue
        host = (url.host or "").lower()
        if host:
            hosts.add(host)
    return frozenset(hosts)


def check_url(url: httpx.URL, allowed: frozenset[str]) -> None:
    """[SECURE] 응답 도착지 스킴·호스트 화이트리스트 검증 - SSRF 방지 (Category 1)."""
    scheme = (url.scheme or "").lower()
    if scheme != _ALLOWED_SCHEME:
        raise BlockedHostError(f"허용되지 않은 스킴 '{scheme}' (https만 허용)")
    host = (url.host or "").lower()
    if host not in allowed:
        # 상세 URL(쿼리스트링에 토큰이 실릴 수 있다)이 아니라 호스트만 메시지에 담는다 (Category 2)
        raise BlockedHostError(f"허용되지 않은 호스트 '{host}'")


RequestHook = Callable[[httpx.Request], Coroutine[Any, Any, None]]
ResponseHook = Callable[[httpx.Response], Coroutine[Any, Any, None]]


def async_event_hooks(allowed: frozenset[str]) -> dict[str, list[Any]]:
    """httpx.AsyncClient(event_hooks=...)에 그대로 넣는 훅 묶음.

    request 훅과 response 훅을 모두 건다.
      - request 훅: 리다이렉트로 만들어진 요청까지 **전송 전에** 차단한다.
        응답 단계에서만 막으면 내부 주소로 GET 자체는 나가버려(blind SSRF) 의미가 반감된다.
      - response 훅: 최종 도착지 검증(방어 다중화). 본문을 읽기 전에 실행된다.
    AsyncClient의 훅은 코루틴이어야 하므로 async 함수를 돌려준다.
    """

    async def _check_request(request: httpx.Request) -> None:
        try:
            check_url(request.url, allowed)
        except BlockedHostError as e:
            # [SECURE] 차단 사유를 로컬 로그로만 남기고 예외 전파 - 빈 except 없음 (Category 4)
            logger.warning("[HttpGuard] 요청 차단: %s", e)
            raise

    async def _check_response(response: httpx.Response) -> None:
        try:
            check_url(response.url, allowed)
        except BlockedHostError as e:
            logger.warning("[HttpGuard] 응답 차단: %s", e)
            raise

    return {"request": [_check_request], "response": [_check_response]}


# --------------------------------------------------
# Security Checklist
# Applied:
#   - SSRF: 리다이렉트로 생성된 요청까지 전송 전에 호스트·스킴을 화이트리스트로 검증하고(request 훅),
#     최종 응답도 본문 파싱 전에 다시 검증한다(response 훅). 차단 시 본문은 읽히지 않는다
#     (httpx가 스트림을 닫고 예외를 전파한다)
#   - Improper Input for Security Decision: 허용 판단은 응답이 실제 도착한 URL(response.url)로만 하며,
#     Location 헤더나 응답 본문 같은 상대측 데이터로 판단하지 않는다
#   - Improper Exception Handling: 빈 except 없음, 차단은 반드시 로깅 후 예외 전파
#   - Error Message Information Exposure: 예외 메시지에 전체 URL(쿼리스트링·토큰)을 넣지 않고 호스트만 담는다
#   - Security Decision Based on DNS Lookup: DNS 조회 결과가 아니라 URL 호스트 문자열로만 판단한다
# Not Applied:
#   - [WARN] DNS rebinding: 허용 호스트가 사설 IP로 해석되는 경우까지는 막지 못한다.
#     IP 고정(pinning)이 필요하면 커스텀 트랜스포트에서 연결 IP를 검증해야 한다.
#   - [WARN] yfinance 내부 요청은 이 가드를 거치지 않는다(자체 세션 사용).
# --------------------------------------------------
