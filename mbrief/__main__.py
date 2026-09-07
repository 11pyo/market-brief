"""`python -m mbrief` 진입점."""
import sys

from mbrief.cli import main

if __name__ == "__main__":
    sys.exit(main())

# --------------------------------------------------
# Security Checklist
# Applied:
#   - Error Message Information Exposure: 예외 처리와 종료 코드 결정은 cli.main에 위임 (스택 노출 없음)
# Not Applied:
#   - [WARN] 해당 없음 - 진입점 위임 전용 모듈
# --------------------------------------------------
