"""Слой вызова модели на подменённом сервере: разбор, проверка, повтор, отказ."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from backend.llm.client import (Endpoint, InferenceContractError, InferenceTimeout, InferenceUnavailable,
                                LlmClient)
from backend.llm.prompts import PromptSpec, render
from backend.llm.structured import Image, StructuredOutputRejected, parse, run_structured

LLM_DIR = Path(__file__).resolve().parents[2] / "backend" / "llm"


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | None


SPEC = PromptSpec(name="probe", revision=1, system="Верни JSON по схеме: {schema}", user="Данные: {text}",
                  retry="Ошибка: {errors}")


def reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class Server:
    """Подменённый сервер: отдаёт заранее заданные ответы и запоминает запросы."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer)
        if isinstance(answer, dict):
            return httpx.Response(200, json=answer)   # тело целиком, в том числе повреждённое
        return httpx.Response(200, json=reply(answer))


def client(server: Server) -> LlmClient:
    return LlmClient(Endpoint("http://llm/v1", "key", "m", 60), http=httpx.Client(transport=httpx.MockTransport(server)))


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, tool, request, response, duration_ms, outcome):
        self.calls.append({"tool": tool, "request": request, "response": response, "ms": duration_ms, "outcome": outcome})


def call(server, recorder, check=None, **kw):
    return run_structured(client(server), SPEC, Answer, {"text": "abc"}, record=recorder, check=check, **kw)


def test_request_disables_thinking_and_enforces_schema():
    server, rec = Server('{"value": "abc"}'), Recorder()
    assert call(server, rec).value == "abc"
    sent = server.requests[0]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["temperature"] == 0.0
    assert sent["response_format"]["json_schema"]["schema"] == Answer.model_json_schema()
    assert [c["outcome"] for c in rec.calls] == ["успех"]
    assert rec.calls[0]["tool"] == "llm.probe" and rec.calls[0]["request"]["prompt_version"].startswith("probe-1-")


def test_mismatch_is_retried_once_with_errors():
    server, rec = Server("не json", '{"value": "abc"}'), Recorder()
    assert call(server, rec).value == "abc"
    assert [c["outcome"] for c in rec.calls] == ["отказ", "успех"]
    retry = server.requests[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": "не json"}
    assert retry[-1]["content"].startswith("Ошибка:")
    assert rec.calls[1]["request"]["retry_errors"]


def test_second_mismatch_is_a_refusal_not_raw_text():
    server, rec = Server('{"value": 1}', '{"other": "x"}'), Recorder()
    with pytest.raises(StructuredOutputRejected) as exc:
        call(server, rec)
    assert len(exc.value.errors) == 2
    assert [c["outcome"] for c in rec.calls] == ["отказ", "отказ"]
    assert all(c["response"]["parsed"] is None for c in rec.calls)


def test_caller_check_catches_schema_valid_but_invented_values():
    server, rec = Server('{"value": "xyz"}', '{"value": "abc"}'), Recorder()
    check = lambda a: [] if a.value == "abc" else [f"значение «{a.value}» отсутствует во входных данных"]
    assert call(server, rec, check=check).value == "abc"
    assert rec.calls[0]["response"]["errors"] == ["значение «xyz» отсутствует во входных данных"]


def test_timeout_is_not_retried():
    server, rec = Server(httpx.ReadTimeout("slow")), Recorder()
    with pytest.raises(InferenceTimeout):
        call(server, rec)
    assert len(server.requests) == 1 and [c["outcome"] for c in rec.calls] == ["деградация"]


def test_server_error_is_unavailable():
    with pytest.raises(InferenceUnavailable):
        call(Server(503), Recorder())


def test_thinking_block_and_fence_are_stripped():
    value, errors = parse('<think>рассуждение</think>\n```json\n{"value": "abc"}\n```', Answer)
    assert value.value == "abc" and errors == []


def test_render_is_single_pass():
    assert render("Данные: {text}", {"text": "{schema} и {text}"}) == "Данные: {schema} и {text}"


def test_version_tracks_wording_schema_and_params():
    schema = Answer.model_json_schema()
    base = SPEC.version(schema)
    assert base == SPEC.version(schema)
    changed = [PromptSpec(**{**SPEC.__dict__, "system": SPEC.system + " "}),
               PromptSpec(**{**SPEC.__dict__, "max_tokens": 256}),
               PromptSpec(**{**SPEC.__dict__, "enforce_schema": False})]
    assert all(s.version(schema) != base for s in changed)
    assert SPEC.version({**schema, "title": "Other"}) != base


def test_images_are_sent_as_content_parts_and_logged_by_hash():
    server, rec = Server('{"value": "abc"}'), Recorder()
    call(server, rec, images=[Image("image/jpeg", b"\xff\xd8binary")])
    content = server.requests[0]["messages"][1]["content"]
    assert content[0]["type"] == "text" and content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    logged = rec.calls[0]["request"]["images"][0]
    assert set(logged) == {"content_type", "bytes", "sha256"}


def test_redaction_applies_to_logs_only():
    server, rec = Server('{"value": "abc"}'), Recorder()
    run_structured(client(server), SPEC, Answer, {"text": "тел. +7 000 000-00-01"}, record=rec,
                   redact=lambda s: s.replace("+7 000 000-00-01", "[телефон]"))
    assert "+7 000 000-00-01" in server.requests[0]["messages"][1]["content"]
    assert "[телефон]" in rec.calls[0]["request"]["user"]


def test_layer_knows_nothing_about_the_domain():
    terms = re.compile(r"обращени|оборудован|серийн|гаранти|партн[её]р|шильд|таблич|неисправн|ремонт", re.I)
    hits = [f"{p.name}: {m.group(0)}" for p in LLM_DIR.glob("*.py") for m in terms.finditer(p.read_text(encoding="utf-8"))]
    assert hits == []


# --- повреждённый ответ при HTTP 200 ---------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"choices": [], "usage": {}},                                  # выбор пуст
    {"usage": {}},                                                 # choices нет вовсе
    {"choices": [{"finish_reason": "stop"}]},                      # выбор без message
    {"choices": "нет"},                                            # choices не список
])
def test_malformed_success_is_a_controlled_failure(body):
    """Ответ 200 не по контракту — отказ вызова, а не исключение разбора:
    шаг завершается определённым исходом, а не остаётся в промежуточном."""
    server, rec = Server(body), Recorder()
    with pytest.raises(InferenceContractError):
        call(server, rec)
    assert len(server.requests) == 1                               # повтор бессмыслен
    assert [c["outcome"] for c in rec.calls] == ["деградация"]


def test_non_json_success_is_a_controlled_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>шлюз</html>")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(InferenceContractError):
        LlmClient(Endpoint("http://llm/v1", "key", "m", 60), http=http).embed(["текст"])


def test_embeddings_count_must_match_request():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(InferenceContractError):
        LlmClient(Endpoint("http://llm/v1", "key", "m", 60), http=http).embed(["первый", "второй"])
