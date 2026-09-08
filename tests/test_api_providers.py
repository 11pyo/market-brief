"""API 폴백 프로바이더 테스트 — 실제 네트워크 호출은 절대 하지 않는다.

가짜 SDK 클라이언트를 주입해 다음을 고정한다:
  1. OpenAI/Gemini가 JSON 스키마로 구조화 출력을 요청하고 결과를 dict로 파싱한다
  2. 스키마가 거부되면 같은 프로바이더로 **1회만** 스키마 없이 재시도하고 텍스트를 돌려준다
  3. Anthropic은 텍스트 전용이지만 같은 ProviderResult 계약을 지킨다
  4. to_gemini_schema가 Gemini가 못 읽는 키를 재귀적으로 제거한다
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from mbrief.llm import api_providers
from mbrief.llm.prompts import STRUCTURED_JSON_SCHEMA

STRUCTURED_BODY = {"headline": "테스트", "overnight": "본문"}


@pytest.fixture(autouse=True)
def _clean_clients():
    api_providers.reset_clients()
    yield
    api_providers.reset_clients()


def _run(coro):
    return asyncio.run(coro)


# ===== to_gemini_schema =====
def test_to_gemini_schema_strips_unsupported_keys():
    converted = api_providers.to_gemini_schema(STRUCTURED_JSON_SCHEMA)
    raw = json.dumps(converted)
    for key in ("additionalProperties", "$schema", "$defs", "$ref", "examples"):
        assert key not in raw, key


def test_to_gemini_schema_strips_nested_keys_recursively():
    source = {
        "type": "object",
        "additionalProperties": False,
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "properties": {
            "outer": {
                "type": "array",
                "examples": ["x"],
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"inner": {"type": "string", "$ref": "#/$defs/x"}},
                },
            },
        },
        "$defs": {"x": {"type": "string"}},
    }
    converted = api_providers.to_gemini_schema(source)
    assert converted == {
        "type": "object",
        "properties": {
            "outer": {
                "type": "array",
                "items": {"type": "object", "properties": {"inner": {"type": "string"}}},
            },
        },
    }
    # 원본은 변형하지 않는다 (모듈 상수를 재사용하기 때문에 필수)
    assert source["additionalProperties"] is False
    assert "$defs" in source


def test_to_gemini_schema_converts_nullable_union_type():
    converted = api_providers.to_gemini_schema({"type": ["string", "null"]})
    assert converted == {"type": "string", "nullable": True}
    assert api_providers.to_gemini_schema({"type": "string"}) == {"type": "string"}


def test_to_gemini_schema_is_accepted_by_the_real_sdk():
    """설치된 google-genai가 변환 결과를 Schema로 받아들이는지 (네트워크 없이 계약만 확인)."""
    types = pytest.importorskip("google.genai.types")

    schema = types.Schema.model_validate(api_providers.to_gemini_schema(STRUCTURED_JSON_SCHEMA))
    assert set(schema.properties) == set(STRUCTURED_JSON_SCHEMA["properties"])
    assert schema.properties["portfolioNote"].nullable is True      # ["string","null"] 변환
    assert schema.properties["sessionRecap"].required == ["summary", "drivers"]
    assert set(schema.properties["keyActors"].items.properties) == {
        "name", "role", "action", "impact", "assets",
    }
    assert schema.properties["headline"].max_length == 90


def test_to_gemini_schema_rejects_runaway_nesting():
    node: dict = {"type": "string"}
    for _ in range(api_providers._MAX_SCHEMA_DEPTH + 5):
        node = {"type": "object", "properties": {"n": node}}
    with pytest.raises(ValueError):
        api_providers.to_gemini_schema(node)


# ===== OpenAI =====
class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))]
        )


def _fake_openai(monkeypatch, responses):
    completions = _FakeCompletions(responses)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(api_providers, "_get_openai_client", lambda: client)
    return completions


def test_openai_structured_returns_parsed_dict(monkeypatch):
    calls = _fake_openai(monkeypatch, [json.dumps(STRUCTURED_BODY)])

    result = _run(api_providers.call_openai("sys", "user", STRUCTURED_JSON_SCHEMA))

    assert result.structured == STRUCTURED_BODY
    assert result.model
    fmt = calls.calls[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "market_briefing"
    assert fmt["json_schema"]["strict"] is False
    assert fmt["json_schema"]["schema"] is STRUCTURED_JSON_SCHEMA


def test_openai_without_schema_sends_no_response_format(monkeypatch):
    calls = _fake_openai(monkeypatch, ["## PART 1 — 본문"])

    result = _run(api_providers.call_openai("sys", "user"))

    assert result.structured is None
    assert result.text == "## PART 1 — 본문"
    assert "response_format" not in calls.calls[0]


class _BadRequest(ValueError):
    """openai.BadRequestError 대역 (ValueError 계열로 잡히는지 확인)."""


def test_openai_retries_once_without_schema_when_rejected(monkeypatch):
    calls = _fake_openai(monkeypatch, [_BadRequest("response_format not supported"), "마크다운 본문"])

    result = _run(api_providers.call_openai("sys", "user", STRUCTURED_JSON_SCHEMA))

    assert len(calls.calls) == 2                      # 정확히 1회만 재시도한다
    assert "response_format" in calls.calls[0]
    assert "response_format" not in calls.calls[1]
    assert result.structured is None
    assert result.text == "마크다운 본문"


def test_openai_retries_when_structured_body_is_not_json(monkeypatch):
    calls = _fake_openai(monkeypatch, ["이건 JSON이 아니다", "마크다운 본문"])

    result = _run(api_providers.call_openai("sys", "user", STRUCTURED_JSON_SCHEMA))

    assert len(calls.calls) == 2
    assert result.structured is None
    assert result.text == "마크다운 본문"


def test_openai_retries_when_structured_body_is_a_json_array(monkeypatch):
    _fake_openai(monkeypatch, ["[1, 2, 3]", "마크다운 본문"])
    result = _run(api_providers.call_openai("sys", "user", STRUCTURED_JSON_SCHEMA))
    assert result.structured is None
    assert result.text == "마크다운 본문"


def test_openai_empty_text_response_raises(monkeypatch):
    _fake_openai(monkeypatch, [""])
    with pytest.raises(api_providers.ProviderError):
        _run(api_providers.call_openai("sys", "user"))


def test_openai_second_failure_propagates(monkeypatch):
    """재시도까지 실패하면 예외를 삼키지 않고 상위(engine)로 올린다."""
    _fake_openai(monkeypatch, [_BadRequest("bad schema"), RuntimeError("api down")])
    with pytest.raises(RuntimeError, match="api down"):
        _run(api_providers.call_openai("sys", "user", STRUCTURED_JSON_SCHEMA))


# ===== Gemini =====
class _FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)


class _FakeConfig(dict):
    """types.GenerateContentConfig 대역 — 넘어온 kwargs를 그대로 노출한다."""


def _fake_gemini(monkeypatch, responses, *, config_rejects: tuple[str, ...] = ()):
    models = _FakeModels(responses)
    monkeypatch.setattr(api_providers, "_get_gemini_client", lambda: SimpleNamespace(models=models))

    def config_factory(**kwargs):
        for key in config_rejects:
            if key in kwargs:
                raise TypeError(f"unexpected keyword argument '{key}'")
        return _FakeConfig(kwargs)

    fake_types = SimpleNamespace(GenerateContentConfig=config_factory)
    monkeypatch.setitem(
        __import__("sys").modules, "google.genai",
        SimpleNamespace(types=fake_types),
    )
    monkeypatch.setitem(__import__("sys").modules, "google.genai.types", fake_types)
    return models


def test_gemini_structured_sends_schema_and_parses_json(monkeypatch):
    models = _fake_gemini(monkeypatch, [json.dumps(STRUCTURED_BODY)])

    result = _run(api_providers.call_gemini("sys", "user", STRUCTURED_JSON_SCHEMA))

    assert result.structured == STRUCTURED_BODY
    config = models.calls[0]["config"]
    assert config["response_mime_type"] == "application/json"
    assert "additionalProperties" not in json.dumps(config["response_schema"])
    assert config["system_instruction"] == "sys"


def test_gemini_falls_back_when_schema_keyword_is_unsupported(monkeypatch):
    models = _fake_gemini(
        monkeypatch, ["마크다운 본문"], config_rejects=("response_schema",),
    )

    result = _run(api_providers.call_gemini("sys", "user", STRUCTURED_JSON_SCHEMA))

    assert len(models.calls) == 1                 # 첫 호출은 config 생성에서 실패했다
    assert "response_schema" not in models.calls[0]["config"]
    assert result.structured is None
    assert result.text == "마크다운 본문"


def test_gemini_falls_back_when_structured_body_is_not_json(monkeypatch):
    models = _fake_gemini(monkeypatch, ["JSON 아님", "마크다운 본문"])

    result = _run(api_providers.call_gemini("sys", "user", STRUCTURED_JSON_SCHEMA))

    assert len(models.calls) == 2
    assert "response_schema" in models.calls[0]["config"]
    assert "response_schema" not in models.calls[1]["config"]
    assert result.text == "마크다운 본문"


def test_gemini_empty_response_raises(monkeypatch):
    _fake_gemini(monkeypatch, ["", ""])
    with pytest.raises(api_providers.ProviderError):
        _run(api_providers.call_gemini("sys", "user", STRUCTURED_JSON_SCHEMA))


# ===== Anthropic =====
def test_anthropic_is_text_only_but_keeps_the_contract(monkeypatch):
    seen: dict = {}

    async def create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="본문")])

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(api_providers, "_get_anthropic_client", lambda: client)

    result = _run(api_providers.call_anthropic("sys", "user", STRUCTURED_JSON_SCHEMA))

    assert isinstance(result, api_providers.ProviderResult)
    assert result.structured is None                  # 스키마 인자는 무시한다
    assert result.text == "본문"
    assert seen["system"] == "sys"


# ===== 프로바이더 목록 =====
def test_structured_support_map():
    assert api_providers.supports_structured("openai") is True
    assert api_providers.supports_structured("gemini") is True
    assert api_providers.supports_structured("anthropic") is False
    assert api_providers.supports_structured("unknown") is False


def test_providers_keep_their_registry_names():
    assert set(api_providers.PROVIDERS) == {"gemini", "openai", "anthropic"}


def test_available_providers_filters_missing_keys(monkeypatch):
    from mbrief.config import settings

    monkeypatch.setattr(settings, "llm_fallback_providers", "openai,gemini,nope")
    monkeypatch.setattr(settings, "openai_api_key", "k")
    monkeypatch.setattr(settings, "gemini_api_key", "  ")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert api_providers.available_providers() == ["openai"]


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Unsafe Deserialization: 구조화 응답이 JSON 객체일 때만 structured로 넘어가고,
#     배열·비JSON은 텍스트로 강등되는지 검증 (필드 검증은 engine 계층 테스트가 담당)
#   - Improper Exception Handling: 스키마 거부 시 재시도가 정확히 1회이고, 두 번째 실패는
#     삼키지 않고 상위로 전파되는지 검증
#   - Infinite Loop or Recursion: to_gemini_schema의 재귀 깊이 상한이 동작하는지 검증
#   - Hard-coded Credentials: 실제 클라이언트를 만들지 않으므로 키가 필요 없다 (가짜 주입)
# Not Applied:
#   - [WARN] 실제 OpenAI/Gemini/Anthropic 호출은 테스트하지 않는다 (네트워크·과금).
#     SDK 응답 객체 형태가 바뀌면 이 테스트는 그 사실을 잡아내지 못한다.
# --------------------------------------------------
