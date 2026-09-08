"""정적 사이트 렌더링 패키지.

- `markdown` : LLM/외부 유래 텍스트를 안전하게 HTML로 변환 (이스케이프 우선)
- `i18n`     : UI 라벨 사전 + 로케일 날짜 포맷
- `builder`  : data/ + web/ + templates/ → site/ 정적 산출물

무거운 의존성(jinja2)은 `builder`를 import할 때만 로드된다.
"""
from __future__ import annotations

__all__ = ["build", "build_from_data", "BuildResult"]


def __getattr__(name: str):
    # [SECURE] 지연 import - 모듈 로드 시점의 불필요한 코드 실행 표면 축소 (Category 5)
    if name in __all__:
        from mbrief.site import builder

        return getattr(builder, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Unsafe Deserialization: 이 모듈은 외부 데이터를 역직렬화하지 않는다
#   - Improper Input for Security Decision: __getattr__는 화이트리스트(__all__)만 허용
# Not Applied:
#   - [WARN] 해당 없음 (상수/지연 import 전용 모듈)
# --------------------------------------------------
