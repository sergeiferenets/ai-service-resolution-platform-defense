"""Knowledge Agent (ADR-0002): гипотеза причины по найденным фрагментам.

Агент работает только с тем, что вернул поиск:
1. гибридный поиск по корпусу с фильтром прав в запросе (ADR-0003); вызов
   записывается в TOOL_CALL, применённые уровни доступа и выданные документы —
   в аудит: так проверяется, что представитель партнёра не получил внутренний
   бюллетень;
2. фрагменты — недоверенные данные: фрагмент с обращением к модели
   отбрасывается до вызова и в аудит (security.md, раздел 3.6);
3. пустая выдача — sufficient: false и эскалация человеку; ответ без
   источников не выдаётся ни при каких условиях (ADR-0003, Compliance);
4. ответ модели проверяется: ссылка — целиком, вместе с разделом, и только
   из выдачи этого запроса; код ошибки — по оборудованию из обращения и
   фрагментов; модели — из фрагментов; артикулы запрещены; стоимость,
   сроки, обещание бесплатного ремонта и вердикт по гарантии запрещены —
   их выносит код, а не гипотеза. Нарушение — повтор, второе — отказ.
"""

from __future__ import annotations

import re
import time
from typing import Callable, Sequence

from backend.agents.contracts import KnowledgeResult, KnowledgeTools, SourceRef
from backend.agents.knowledge.prompt import HYPOTHESIS
from backend.agents.knowledge.schema import Hypothesis
from backend.domain.models import AuthContext
from backend.knowledge.search import Fragment, Retriever, access_filter
from backend.llm.client import LlmClient
from backend.llm.structured import run_structured
from backend.orchestrator.recording import elapsed_ms
from backend.tools.error_codes import ErrorCodeDirectory

DOC_REF = re.compile(r"\bDOC-\d{3}(?:#\d+)?\b")
MODEL_REF = re.compile(r"\bMOD-\d{3}\b")
PART_REF = re.compile(r"\bSP-\d{3}\b")
CODE = re.compile(r"\b(?:\d{2}\.[\dA-Za-zx]{1,3}(?:\.[\dA-Za-zx]{1,3})?|[CE]\d{4}|#\d{3})\b")
FRAGMENT_LIMIT = 5

# Утверждения, которых гипотеза делать не может: их выносит код — смета и
# цены (approval.assess), сроки (compute_urgency, договор), гарантия
# (warranty.prequalify, предварительная по определению, PRD 5.5).
#
# Проверка идёт по нормализованным словам с границами, а не поиском подстроки:
# порядок слов и вставки между ними не должны давать обход. Предыдущая
# редакция сравнивала целые обороты, и «Гарантия на этот отказ не
# распространяется» проходила мимо неё.
WORD = re.compile(r"[а-яa-z0-9]+")
MONEY_SIGNS = ("₽", "$", "€")
# Семейства слов: сравнение по началу нормализованного слова.
MONEY_REASON = "стоимость и смету определяет платформа, а не гипотеза"
TERM_REASON = "сроки определяет платформа по договору и срочности"
CLAIM_FAMILIES = (
    (("гарант",), "вердикт по гарантии выносит предварительная квалификация, а не гипотеза"),
    (("оплат", "бесплат", "безвозмезд", "платн"), "об оплате ремонта гипотеза не заявляет"),
    (("руб", "стоим", "цен", "смет", "прайс", "тариф"), MONEY_REASON),
    (("срок",), TERM_REASON),
)
# Единицы времени запрещены не сами по себе, а как срок работы: «в течение
# 30 секунд прогрева» — описание работы устройства, «ремонт займёт неделю» —
# обещание срока.
TIME_UNITS = frozenset({
    "секунда", "секунды", "секунд", "минута", "минуты", "минут", "час", "часа", "часов",
    "день", "дня", "дней", "дн", "сутки", "суток", "неделя", "недели", "недель", "неделю",
    "месяц", "месяца", "месяцев",
})
WORK_PREFIXES = ("ремонт", "замен", "поставк", "устран", "восстанов", "исполнен", "доставк", "выезд", "обслуж")
SENTENCE = re.compile(r"[.!?;\n]+")


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def unsupported_claims(hypothesis: str) -> list[str]:
    """Неподтверждённые коммерческие и гарантийные утверждения в гипотезе.

    Проверяется только текст, сочинённый моделью. Содержимое найденных
    фрагментов здесь не разбирается: документация вправе говорить о гарантии
    и сроках, а гипотеза — нет.
    """
    text = normalize(hypothesis)
    problems = [MONEY_REASON] if any(sign in text for sign in MONEY_SIGNS) else []
    for sentence in SENTENCE.split(text):
        words = WORD.findall(sentence)
        if not words:
            continue
        for prefixes, reason in CLAIM_FAMILIES:
            if any(word.startswith(prefixes) for word in words):
                problems.append(reason)
        if any(word in TIME_UNITS for word in words) and any(word.startswith(WORK_PREFIXES) for word in words):
            problems.append(TERM_REASON)
    # Один и тот же упрёк не повторяется: повтору нужен перечень, а не эхо.
    return sorted(set(problems))


def render_fragments(fragments: Sequence[Fragment]) -> str:
    return "\n\n".join(f"[{f.reference}] {f.title} / {f.section_title}\n{f.content}" for f in fragments)


def hypothesis_check(fragments: Sequence[Fragment], request_text: str, codes: ErrorCodeDirectory):
    """Проверяемость утверждений: всё названное существует, относится к этому
    оборудованию и не выходит за границы того, что можно утверждать по
    документации.

    Ссылка проверяется целиком, вместе с разделом: документ мог быть выдан
    поиском, а раздел — нет. Источник обязан принадлежать выдаче именно этого
    запроса: available собирается из фрагментов текущего вызова.
    """
    available = {f.reference for f in fragments}
    models = {m for f in fragments for m in f.model_refs}
    corpus_text = " ".join(f.content for f in fragments)

    def code_applicable(code: str) -> bool:
        """Код допустим, если он назван в обращении или во фрагментах. Код,
        существующий в справочнике для другого оборудования, не подходит:
        проверка идёт по моделям выданных фрагментов."""
        if code in request_text or code in corpus_text:
            return True
        return any(codes.check(code, model).status == "в справочнике" for model in models)

    def check(value: Hypothesis) -> list[str]:
        problems: list[str] = []
        if value.sufficient and not value.sources:
            problems.append("sources: при sufficient: true нужна хотя бы одна ссылка")
        if not value.sufficient and value.hypothesis.strip():
            problems.append("hypothesis: при sufficient: false гипотеза должна быть пустой")
        problems += [f"sources: ссылки «{s}» нет среди фрагментов, найденных по этому обращению"
                     for s in value.sources if s not in available]
        for ref in DOC_REF.findall(value.hypothesis):
            if "#" not in ref:
                problems.append(f"hypothesis: ссылка «{ref}» без раздела: ссылаться можно только на выданный фрагмент")
            elif ref not in available:
                problems.append(f"hypothesis: фрагмента «{ref}» нет среди найденных по этому обращению")
        problems += [f"hypothesis: модели «{m}» нет во фрагментах" for m in MODEL_REF.findall(value.hypothesis)
                     if m not in models]
        problems += [f"hypothesis: артикул «{p}» назвать нельзя: платформа его не проверяет"
                     for p in PART_REF.findall(value.hypothesis)]
        problems += [f"hypothesis: код «{c}» не относится к оборудованию из обращения и фрагментов"
                     for c in CODE.findall(value.hypothesis) if not code_applicable(c)]
        problems += [f"hypothesis: {reason}" for reason in unsupported_claims(value.hypothesis)]
        return problems
    return check


class LlmKnowledgeAgent:
    IS_STUB = False

    def __init__(self, retriever: Retriever, client: LlmClient, codes: ErrorCodeDirectory,
                 *, injection: Callable[[str], list[str]] | None = None, limit: int = FRAGMENT_LIMIT):
        self._retriever = retriever
        self._client = client
        self._codes = codes
        self._injection = injection
        self._limit = limit

    def _safe(self, fragments: list[Fragment], tools: KnowledgeTools) -> list[Fragment]:
        """Фрагмент корпуса — недоверенные данные, как и текст обращения.

        Документ мог быть загружен с текстом, обращённым к модели. Такой
        фрагмент не передаётся ей вовсе: запрет в формулировке запроса —
        не защита, а просьба. Отбрасывание попадает в аудит.
        """
        if self._injection is None:
            return fragments
        safe, rejected = [], []
        for fragment in fragments:
            hits = self._injection(fragment.content)
            (rejected if hits else safe).append((fragment, hits))
        if rejected:
            tools.audit("corpus_injection", {"fragments": [f.reference for f, _ in rejected],
                                             "hits": sorted({h for _, hits in rejected for h in hits})})
        return [f for f, _ in safe]

    def _retrieve(self, ctx: AuthContext, query: str, tools: KnowledgeTools) -> list[Fragment]:
        started = time.perf_counter()
        request = {"query": tools.redact(query), "limit": self._limit, "access": access_filter(ctx)}
        try:
            fragments = self._retriever.search(ctx, query, limit=self._limit)
        except Exception as exc:
            tools.record("search.hybrid", request, {"error": f"{type(exc).__name__}: {exc}"},
                         elapsed_ms(started), "деградация")
            raise
        fragments = self._safe(fragments, tools)
        found = [{"reference": f.reference, "document_id": f.document_id,
                  "confidentiality_level": f.confidentiality_level, "score": f.score} for f in fragments]
        tools.record("search.hybrid", request, {"fragments": found}, elapsed_ms(started), "успех")
        # Аудит прав: видно, какие уровни были разрешены и что выдано.
        tools.audit("retrieval", {"user": ctx.user_id, "role": ctx.role, "partner_id": ctx.partner_id,
                                  "levels": sorted({f.confidentiality_level for f in fragments}),
                                  "documents": sorted({f.document_id for f in fragments})})
        return fragments

    def search(self, ctx: AuthContext, raw_text: str, *, tools: KnowledgeTools) -> KnowledgeResult:
        fragments = self._retrieve(ctx, raw_text, tools)
        considered = tuple(f.reference for f in fragments)
        if not fragments:
            return KnowledgeResult("", "раздел документации", (), considered, sufficient=False)

        value = run_structured(
            self._client, HYPOTHESIS, Hypothesis,
            {"request": raw_text, "fragments": render_fragments(fragments)},
            record=tools.record, check=hypothesis_check(fragments, raw_text, self._codes), redact=tools.redact)

        by_reference = {f.reference: f for f in fragments}
        sources = tuple(SourceRef(reference=f.reference, document_id=f.document_id, chunk_id=f.chunk_id,
                                  title=f.title, section_title=f.section_title,
                                  confidentiality_level=f.confidentiality_level)
                        for f in (by_reference[s] for s in value.sources))
        return KnowledgeResult(hypothesis=value.hypothesis, evidence_level=value.evidence_level, sources=sources,
                               considered=considered, sufficient=value.sufficient and bool(sources))
