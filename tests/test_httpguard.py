"""httpguard(SSRF 가드) 단위 테스트 + 캘린더·시세 수집기 배선 확인 — 네트워크 없음."""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from mbrief.sources import calendar_service, httpguard, market_data


# ===== 1. 허용 목록 파생 =====


def test_hosts_from_urls_extracts_lowercased_hosts():
    hosts = httpguard.hosts_from_urls(
        ["https://Finnhub.IO/api/v1", "https://api.bls.gov/publicAPI/v1/timeseries/data/"]
    )
    assert hosts == frozenset({"finnhub.io", "api.bls.gov"})


def test_hosts_from_urls_rejects_non_https_and_broken_constants(caplog):
    with caplog.at_level(logging.WARNING):
        hosts = httpguard.hosts_from_urls(["http://insecure.example.com/x", "https://ok.example.com"])
    assert hosts == frozenset({"ok.example.com"})
    assert "https가 아닌" in caplog.text


# ===== 2. check_url =====


@pytest.mark.parametrize("url", [
    "http://www.federalreserve.gov/feeds/press_all.xml",   # 스킴 강등
    "https://evil.example.com/feed.xml",                   # 허용 목록 밖
    "https://sub.federalreserve.gov/x",                    # 서브도메인 자동 허용 없음
    "http://127.0.0.1:8000/",
    "http://169.254.169.254/latest/meta-data/",
])
def test_check_url_blocks(url):
    allowed = frozenset({"www.federalreserve.gov"})
    with pytest.raises(httpguard.BlockedHostError):
        httpguard.check_url(httpx.URL(url), allowed)


def test_check_url_allows_exact_host():
    allowed = frozenset({"www.federalreserve.gov"})
    httpguard.check_url(httpx.URL("https://www.federalreserve.gov/feeds/press_all.xml"), allowed)


def test_blocked_error_message_has_no_query_string():
    """토큰이 실릴 수 있는 전체 URL을 예외 메시지에 담지 않는다."""
    with pytest.raises(httpguard.BlockedHostError) as exc:
        httpguard.check_url(httpx.URL("https://evil.example.com/x?token=SECRET"), frozenset())
    assert "SECRET" not in str(exc.value)
    assert "evil.example.com" in str(exc.value)


def test_blocked_error_is_httpx_http_error():
    """기존 수집기의 except httpx.HTTPError 격리 경로가 그대로 잡도록 한다."""
    assert issubclass(httpguard.BlockedHostError, httpx.HTTPError)


# ===== 3. 훅 동작 =====


def test_event_hooks_block_request_before_it_is_sent():
    seen: list[str] = []
    allowed = frozenset({"good.example.com"})

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "good.example.com":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8000/"})
        return httpx.Response(200, text="internal secret")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            event_hooks=httpguard.async_event_hooks(allowed),
        ) as client:
            with pytest.raises(httpguard.BlockedHostError):
                await client.get("https://good.example.com/x", follow_redirects=True)

    asyncio.run(run())
    assert seen == ["https://good.example.com/x"]  # 내부 주소로는 요청조차 나가지 않았다


# ===== 4. 수집기 배선 =====


def test_calendar_service_allowlist_matches_constants():
    hosts = calendar_service.ALLOWED_HOSTS
    assert hosts == frozenset({"finnhub.io", "api.bls.gov", "www.federalreserve.gov"})
    assert calendar_service._GUARD_HOOKS.keys() == {"request", "response"}


def test_market_data_allowlist_matches_constants():
    assert market_data.ALLOWED_HOSTS == frozenset({"production.dataviz.cnn.io"})
    assert market_data._GUARD_HOOKS.keys() == {"request", "response"}


def test_fomc_fetch_rejects_redirect_to_private_address(monkeypatch, caplog):
    """회귀: Fed 캘린더가 302로 사설망을 가리켜도 HTML이 파싱되지 않는다."""
    seen: list[str] = []
    parsed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "www.federalreserve.gov":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8000/"})
        return httpx.Response(200, text="<html>internal</html>")

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(calendar_service.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(
        calendar_service, "_parse_fomc_html", lambda html: parsed.append(html) or []
    )
    calendar_service._cache.clear()

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(calendar_service.get_fomc_schedule())

    assert result == []
    assert parsed == []                                 # 내부 응답 본문은 파서에 닿지 않았다
    assert not any("127.0.0.1" in url for url in seen), seen
    assert "일정 수집 실패" in caplog.text
    calendar_service._cache.clear()


def test_cnn_fear_greed_rejects_redirect_to_private_address(monkeypatch, caplog):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "production.dataviz.cnn.io":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8000/"})
        return httpx.Response(200, json={"fear_and_greed": {"score": 99, "rating": "Extreme Greed"}})

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(market_data.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(market_data, "_CNN_FG_CACHE", None, raising=False)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(market_data.get_cnn_fear_greed())

    assert result is None                               # 내부 응답이 지표로 채택되지 않았다
    assert not any("127.0.0.1" in url for url in seen), seen
    assert "CNN 조회 실패" in caplog.text
    monkeypatch.setattr(market_data, "_CNN_FG_CACHE", None, raising=False)


# --------------------------------------------------
# Security Checklist
# Applied:
#   - SSRF: 허용 목록 파생·스킴 강등 거부·서브도메인 비허용·리다이렉트 차단을 각각 검증
#   - Error Message Information Exposure: 차단 예외 메시지에 쿼리스트링(토큰)이 없음을 검증
#   - Improper Exception Handling: 차단이 각 수집기에서 경고 로그로 남고 빈 결과로 격리됨을 검증
# Not Applied:
#   - [WARN] DNS rebinding(허용 호스트가 사설 IP로 해석되는 경우)은 검증 범위 밖이다.
# --------------------------------------------------
