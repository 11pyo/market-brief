"""마크다운 서브셋 → 안전한 HTML 렌더러 (순수 파이썬, 외부 의존성 없음).

이 모듈이 다루는 입력은 전부 **신뢰할 수 없는 출처**다.
  - LLM이 생성한 브리핑 본문 (외부 RSS 헤드라인을 인용한다)
  - 뉴스 제목·출처명 등 외부 API 응답

따라서 원칙은 하나다: **먼저 전부 이스케이프하고, 그 다음 우리가 만든 태그만 삽입한다.**
변환 후에는 입력에서 유래한 `<`, `>`, `"`, `'`가 태그 경계로 해석될 수 없다.

지원 문법 (의도적으로 최소):
  # / ## / ###   제목
  **굵게**  *기울임*  `코드`
  - / • / *      불릿 목록 (중첩 미지원)
  1.             번호 목록
  >              인용
  ---            수평선
  빈 줄          문단 구분, 단일 개행은 <br>
  bare URL       http(s) 스킴만 자동 링크
"""
from __future__ import annotations

import html
import re

__all__ = ["render_markdown_safe", "inline_markdown_safe"]

# [SECURE] 제어문자 제거 - 플레이스홀더 위조 및 파서 혼동 방지 (Category 1)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 플레이스홀더: 인라인 코드/링크를 잠시 빼두고 나머지 인라인 문법을 처리한다.
# 입력에서 제어문자를 이미 제거했으므로 이 토큰은 사용자 입력으로 위조할 수 없다.
_PH_OPEN = "\x01"
_PH_CLOSE = "\x02"
_PH_RE = re.compile(r"\x01(\d+)\x02")

# [SECURE] http(s) 스킴만 매칭 - javascript:/data: 등 위험 스킴 링크화 차단 (Category 1)
# 입력은 이미 HTML 이스케이프된 상태이므로 '<'는 '&lt;'다. 공백/따옴표/꺾쇠에서 끊는다.
_URL_RE = re.compile(r"(?<![\w@/.-])(https?://[^\s<>\"']+)")
_URL_TRAIL = ".,;:!?)]}>。，"

_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s|\*)([^*\n]+?)(?<!\s)\*(?!\*)")

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_HR_RE = re.compile(r"^\s*-{3,}\s*$")
_QUOTE_RE = re.compile(r"^\s*&gt;\s?(.*)$")
_ULI_RE = re.compile(r"^\s*(?:[-•·]|\*(?=\s))\s+(.+)$")
_OLI_RE = re.compile(r"^\s*(\d{1,3})[.)]\s+(.+)$")


def _sanitize(text: object) -> str:
    """None 안전 + 제어문자 제거 + 개행 정규화."""
    if text is None:
        return ""
    s = str(text)
    s = _CONTROL_CHARS.sub("", s)
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _autolink(escaped: str, box: list[str]) -> str:
    """이스케이프된 텍스트의 bare URL을 앵커로 바꾼다. 결과는 플레이스홀더로 보관."""

    def repl(m: re.Match[str]) -> str:
        url = m.group(1)
        trail = ""
        # 문장 끝 구두점은 URL에서 제외한다 (예: "...참고: https://a.com.")
        while url and url[-1] in _URL_TRAIL:
            trail = url[-1] + trail
            url = url[:-1]
        # 이스케이프 결과로 붙은 엔티티 꼬리(&gt; 등)는 링크에서 제외
        for entity in ("&gt;", "&lt;", "&quot;", "&#x27;", "&amp;"):
            if url.endswith(entity):
                url = url[: -len(entity)]
                trail = entity + trail
        if len(url) < 12:  # "https://" 뒤에 실질 호스트가 없으면 링크화하지 않는다
            return m.group(0)
        # url은 이미 HTML 이스케이프된 문자열이라 속성/본문 어디에 넣어도 안전하다.
        # [SECURE] rel=noopener noreferrer - 탭내빙(reverse tabnabbing) 및 리퍼러 유출 방지 (Category 1)
        anchor = (
            f'<a href="{url}" target="_blank" rel="noopener noreferrer nofollow ugc">{url}</a>'
        )
        box.append(anchor)
        return f"{_PH_OPEN}{len(box) - 1}{_PH_CLOSE}{trail}"

    return _URL_RE.sub(repl, escaped)


def _inline(escaped: str, *, links: bool = True) -> str:
    """이스케이프 완료된 한 줄/문단에 인라인 문법을 적용한다."""
    box: list[str] = []

    def stash(markup: str) -> str:
        box.append(markup)
        return f"{_PH_OPEN}{len(box) - 1}{_PH_CLOSE}"

    # 1) 코드 스팬을 먼저 격리한다 (코드 안의 *, URL은 그대로 두어야 한다)
    out = _CODE_RE.sub(lambda m: stash(f"<code>{m.group(1)}</code>"), escaped)
    # 2) 자동 링크
    if links:
        out = _autolink(out, box)
    # 3) 강조
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _ITALIC_RE.sub(r"<em>\1</em>", out)
    # 4) 플레이스홀더 복원
    return _PH_RE.sub(lambda m: box[int(m.group(1))], out)


def inline_markdown_safe(text: object) -> str:
    """한 줄짜리 필드(제목·근거·라벨)용. 굵게/기울임/코드만 지원, 링크 없음."""
    escaped = html.escape(_sanitize(text)).replace("\n", " ")
    return _inline(escaped, links=False)


def _flush_paragraph(buf: list[str], out: list[str]) -> None:
    if not buf:
        return
    body = "<br>".join(_inline(line) for line in buf)
    out.append(f"<p>{body}</p>")
    buf.clear()


def _flush_list(items: list[str], out: list[str], tag: str) -> None:
    if not items:
        return
    lis = "".join(f"<li>{_inline(i)}</li>" for i in items)
    out.append(f"<{tag}>{lis}</{tag}>")
    items.clear()


def _flush_quote(lines: list[str], out: list[str]) -> None:
    if not lines:
        return
    body = "<br>".join(_inline(line) for line in lines)
    out.append(f"<blockquote>{body}</blockquote>")
    lines.clear()


def render_markdown_safe(text: object) -> str:
    """마크다운 서브셋을 안전한 HTML 문자열로 변환한다.

    반환값은 이미 이스케이프를 마친 신뢰 가능한 마크업이므로 템플릿에서 `|safe` 사용이 가능하다.
    """
    src = _sanitize(text)
    if not src.strip():
        return ""

    # [SECURE] 마크다운 변환 이전에 전체 HTML 이스케이프 - XSS 차단 (Category 1)
    escaped = html.escape(src)

    out: list[str] = []
    para: list[str] = []
    ul: list[str] = []
    ol: list[str] = []
    quote: list[str] = []

    def flush_all() -> None:
        _flush_paragraph(para, out)
        _flush_list(ul, out, "ul")
        _flush_list(ol, out, "ol")
        _flush_quote(quote, out)

    # [SECURE] 라인 수 상한 - 비정상 입력에 의한 과도한 반복 방지 (Category 3)
    lines = escaped.split("\n")[:20000]
    for raw in lines:
        line = raw.rstrip()

        if not line.strip():
            flush_all()
            continue

        m = _HEADING_RE.match(line)
        if m:
            flush_all()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2).strip())}</h{level}>")
            continue

        if _HR_RE.match(line):
            flush_all()
            out.append("<hr>")
            continue

        m = _QUOTE_RE.match(line)
        if m:
            _flush_paragraph(para, out)
            _flush_list(ul, out, "ul")
            _flush_list(ol, out, "ol")
            quote.append(m.group(1).strip())
            continue

        m = _OLI_RE.match(line)
        if m:
            _flush_paragraph(para, out)
            _flush_list(ul, out, "ul")
            _flush_quote(quote, out)
            ol.append(m.group(2).strip())
            continue

        m = _ULI_RE.match(line)
        if m:
            _flush_paragraph(para, out)
            _flush_list(ol, out, "ol")
            _flush_quote(quote, out)
            ul.append(m.group(1).strip())
            continue

        _flush_list(ul, out, "ul")
        _flush_list(ol, out, "ol")
        _flush_quote(quote, out)
        para.append(line.strip())

    flush_all()
    return "".join(out)


# --------------------------------------------------
# Security Checklist
# Applied:
#   - XSS: html.escape()를 최우선 적용 후에만 태그를 삽입한다. 입력 유래 문자는 태그가 될 수 없다
#   - Open Redirect / 위험 스킴: 자동 링크는 http(s) 정규식에만 매칭, javascript:/data: 링크화 불가
#   - Reverse Tabnabbing: 모든 외부 링크에 rel="noopener noreferrer nofollow ugc" 고정
#   - Code Injection: eval/exec/동적 실행 없음. 정규식 치환만 사용
#   - Infinite Loop: 라인 수 상한(20000), 역참조 없는 비탐욕 정규식 사용
#   - Improper Input for Security Decision: 제어문자를 사전 제거해 플레이스홀더 위조를 차단
# Not Applied:
#   - [WARN] 마크다운 링크 문법 [text](url)은 의도적으로 미지원. 지원하려면 스킴 화이트리스트
#            (http/https/mailto)를 강제하고 상대경로 처리 규칙을 별도로 정의해야 한다.
#   - [WARN] 표(table)·중첩 목록·각주 미지원. 필요 시 블록 파서를 확장하되 이스케이프 우선 원칙은 유지할 것.
# --------------------------------------------------
