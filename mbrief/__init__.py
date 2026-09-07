"""mbrief — AI 모닝 마켓 브리핑 생성기 (로컬 Claude Code CLI → 정적 사이트).

파이프라인: 수집(뉴스·시세·캘린더) → 필터 → `claude -p` 구조화 생성 → 저장 → 정적 사이트 빌드 → 배포(gh-pages) → 알림.
"""

__version__ = "2.0.0"
