"""IndexNow API 푸시 모듈.

정적 사이트 빌드 또는 배포 즉시 IndexNow 프로토콜(Microsoft Bing, Naver, Yandex 및 연계 AI 에이전트)에
최신 URL 갱신을 푸시하여 크롤러의 즉각적인 색인과 캐시 무효화를 유도한다.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Sequence
from urllib.parse import urlparse

from mbrief.config import settings

logger = logging.getLogger("mbrief.indexing")

INDEXNOW_KEY = "4f63c8a92b1e7d0a5c8e3f1b6a9d2e4f"
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"


def push_indexnow(
    site_url: str = "",
    urls: Sequence[str] | None = None,
    *,
    timeout_sec: float = 8.0,
) -> bool:
    """IndexNow API로 갱신된 URL 목록을 푸시한다.

    Args:
        site_url: 사이트 기본 URL (예: https://market-brief-4ns.pages.dev)
        urls: 알릴 URL 목록. None이면 사이트 루트, llms.txt, llms-full.txt, data/latest.json 포함.
        timeout_sec: HTTP 요청 타임아웃(초)

    Returns:
        성공(200 또는 202) 시 True, 실패 시 False.
    """
    base = (site_url or getattr(settings, "site_url", "") or "").rstrip("/")
    if not base or not base.startswith("http"):
        logger.warning("[IndexNow] 유효한 site_url이 없어 푸시를 건너뜁니다: %r", base)
        return False

    parsed = urlparse(base)
    host = parsed.netloc

    if urls is None:
        target_urls = [
            f"{base}/",
            f"{base}/llms.txt",
            f"{base}/llms-full.txt",
            f"{base}/data/latest.json",
            f"{base}/data/market.json",
        ]
    else:
        target_urls = list(urls)

    payload = {
        "host": host,
        "key": INDEXNOW_KEY,
        "keyLocation": f"{base}/{INDEXNOW_KEY}.txt",
        "urlList": target_urls,
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        INDEXNOW_ENDPOINT,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "MarketBriefAI-IndexNow-Agent/2.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            status = resp.status
            if status in (200, 202):
                logger.info(
                    "[IndexNow] 성공: %d개 URL 푸시 완료 (HTTP %d, host=%s)",
                    len(target_urls),
                    status,
                    host,
                )
                return True
            logger.warning("[IndexNow] 비정상 응답: HTTP %d", status)
            return False
    except Exception as e:
        logger.warning("[IndexNow] 푸시 요청 실패 (오프라인이거나 엔드포인트 일시 장애): %s", e)
        return False
