"""Context Agent (ADR-0002): извлечение идентификаторов из обращения и их проверка.

Модель извлекает — справочник решает (PRD 5.3). Агент:
1. извлекает из текста дословно серийный номер, обозначение модели, код
   ошибки, фрагмент с описанием неисправности и упоминания обстоятельств,
   влияющих на гарантию; каждое значение сверяется с текстом;
2. определяет оборудование по порядку PRD 5.3: серийный номер из текста;
   при его отсутствии — с фотографии таблички; при отсутствии и там — по
   партнёру и обозначению модели;
3. проверяет код ошибки по справочнику; код вне справочника не
   отбрасывается, а передаётся дальше как свободный признак;
4. отмечает признаки риска по гарантии с дословной цитатой (PRD 5.10), без
   вывода о негарантийности.

Выводов о неисправности и маршруте агент не делает. К учётной системе он
обращается только через переданный порт — вызовы записывает оркестратор.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Literal, Protocol

from backend.adapters.port import ErpPort
from backend.agents.context.prompt import PLATE_READING, TEXT_EXTRACTION
from backend.agents.context.schema import PlateReading, TextExtraction
from backend.agents.context.verify import TEXT_FIELDS, contradicts, plausible_serial, text_check
from backend.domain.errors import NotFound
from backend.domain.models import AuthContext, Equipment
from backend.llm.client import InferenceError, LlmClient
from backend.llm.structured import CallRecorder, Image, StructuredOutputRejected, run_structured
from backend.tools.error_codes import CodeCheck, ErrorCodeDirectory

_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"})

Status = Literal["confirmed", "recognized", "derived", "mismatch", "stated", "ambiguous", "insufficient",
                 "manual_input"]


@dataclass(frozen=True)
class RiskSignal:
    kind: str
    quote: str
    source: Literal["агент", "предохранитель"]


@dataclass(frozen=True)
class Identification:
    """status: confirmed, recognized, derived — оборудование определено;
    mismatch — определено, но обозначение в тексте противоречит карточке;
    stated — номер указан, в справочнике не найден; ambiguous — по партнёру и
    модели не одно совпадение; insufficient — данных для определения нет;
    manual_input — нужен ручной ввод серийного номера."""
    status: Status
    confidence_level: Literal["подтверждён", "распознан", "выведен", "со слов"] | None
    discrepancy: bool
    source: Literal["текст", "изображение"] | None
    serial_number: str | None
    equipment: Equipment | None
    candidates: tuple[Equipment, ...]
    reason: str


@dataclass(frozen=True)
class ContextResult:
    extraction: TextExtraction
    plate: PlateReading | None
    identification: Identification
    code_check: CodeCheck | None
    risk_signals: tuple[RiskSignal, ...]
    stub: bool


@dataclass(frozen=True)
class ContextTools:
    erp: ErpPort
    record: CallRecorder
    redact: Callable[[str], str]


class PlateUnavailable(Exception):
    """Модель изображений недоступна или её ответ отвергнут: переход на ручной ввод (PRD, раздел 8)."""


class TextExtractor(Protocol):
    IS_STUB: bool

    def extract(self, text: str, tools: ContextTools) -> TextExtraction: ...


class PlateReader(Protocol):
    def read(self, image: Image, tools: ContextTools) -> PlateReading: ...


class LlmTextExtractor:
    IS_STUB = False

    def __init__(self, client: LlmClient):
        self._client = client

    def extract(self, text: str, tools: ContextTools) -> TextExtraction:
        return run_structured(self._client, TEXT_EXTRACTION, TextExtraction, {"text": text}, record=tools.record,
                              check=text_check(text), redact=tools.redact)


class VisionPlateReader:
    def __init__(self, client: LlmClient):
        self._client = client

    @staticmethod
    def _consistent(reading: PlateReading) -> list[str]:
        if reading.readable and not (reading.serial_number or reading.model_designation):
            return ["readable: true при пустых полях"]
        return []

    def read(self, image: Image, tools: ContextTools) -> PlateReading:
        try:
            return run_structured(self._client, PLATE_READING, PlateReading, {}, record=tools.record,
                                  check=self._consistent, images=[image])
        except (InferenceError, StructuredOutputRejected) as exc:
            raise PlateUnavailable(str(exc)) from exc


def canonical_serial(value: str) -> str:
    return value.translate(_DASHES).strip().upper()


def _cleaned(extraction: TextExtraction) -> TextExtraction:
    """Пустые строки — это отсутствие значения."""
    return extraction.model_copy(update={f: (v.strip() or None) if isinstance(v := getattr(extraction, f), str) else v
                                         for f in TEXT_FIELDS})


class ContextAgent:
    def __init__(self, extractor: TextExtractor, plate_reader: PlateReader, codes: ErrorCodeDirectory,
                 injection: Callable[[str], list[str]]):
        self._extractor = extractor
        self._plate = plate_reader
        self._codes = codes
        self._injection = injection

    @property
    def IS_STUB(self) -> bool:  # noqa: N802 — единая пометка заглушек (stubs.py)
        return bool(getattr(self._extractor, "IS_STUB", False))

    def identify(self, ctx: AuthContext, text: str, *, image: Image | None, partner_id: str | None,
                 tools: ContextTools) -> ContextResult:
        extraction = _cleaned(self._extractor.extract(text, tools))
        plate: PlateReading | None = None
        if extraction.serial_number:
            ident = self._by_serial(ctx, extraction.serial_number, "текст", extraction.model_designation, tools)
        elif image is not None:
            ident, plate = self._by_plate(ctx, image, extraction, partner_id, tools)
        else:
            ident = self._by_partner_model(ctx, partner_id, extraction.model_designation, "текст", tools)
        model_id = ident.equipment.model.model_id if ident.equipment else None
        code = self._codes.check(extraction.error_code, model_id) if extraction.error_code else None
        risks = tuple(RiskSignal(c.kind, c.quote, "агент") for c in extraction.circumstances)
        return ContextResult(extraction, plate, ident, code, risks, self.IS_STUB)

    def _by_serial(self, ctx: AuthContext, serial: str, source: Literal["текст", "изображение"],
                   designation: str | None, tools: ContextTools) -> Identification:
        try:
            equipment = tools.erp.find_equipment_by_serial(ctx, canonical_serial(serial))
        except NotFound:
            reason = ("серийный номер из обращения в справочнике не найден" if source == "текст"
                      else "номер с таблички в справочнике не найден: нужен ручной ввод серийного номера")
            return Identification("stated", "со слов", True, source, serial, None, (), reason)
        if contradicts(designation, equipment.model.manufacturer, equipment.model.name):
            return Identification("mismatch", "со слов", True, source, serial, equipment, (),
                                  f"обозначение «{designation}» в обращении противоречит карточке "
                                  f"({equipment.model.manufacturer} {equipment.model.name})")
        if source == "текст":
            return Identification("confirmed", "подтверждён", False, source, serial, equipment, (),
                                  "серийный номер найден в справочнике")
        return Identification("recognized", "распознан", False, source, serial, equipment, (),
                              "номер с таблички найден в справочнике")

    def _by_plate(self, ctx: AuthContext, image: Image, extraction: TextExtraction, partner_id: str | None,
                  tools: ContextTools) -> tuple[Identification, PlateReading | None]:
        try:
            reading = self._plate.read(image, tools)
        except PlateUnavailable as exc:
            return Identification("manual_input", None, False, "изображение", None, None, (),
                                  f"табличка не распознана ({exc}): нужен ручной ввод серийного номера"), None
        recognized = [v for v in (reading.manufacturer, reading.model_designation, reading.serial_number) if v]
        if any(self._injection(v) for v in recognized):
            return Identification("manual_input", None, False, "изображение", None, None, (),
                                  "распознанный текст содержит указания, адресованные системе: результат "
                                  "отброшен, нужен ручной ввод серийного номера"), reading
        serial = reading.serial_number if reading.readable else None
        if serial and not plausible_serial(serial):
            return Identification("manual_input", None, False, "изображение", serial, None, (),
                                  f"распознанный номер «{serial}» неправдоподобен: нужен ручной ввод"), reading
        if serial:
            return self._by_serial(ctx, serial, "изображение", extraction.model_designation, tools), reading
        designation = extraction.model_designation or reading.model_designation
        source = "текст" if extraction.model_designation else "изображение"
        return self._by_partner_model(ctx, partner_id, designation, source, tools), reading

    def _by_partner_model(self, ctx: AuthContext, partner_id: str | None, designation: str | None,
                          source: Literal["текст", "изображение"], tools: ContextTools) -> Identification:
        missing = [name for name, value in (("партнёр", partner_id), ("обозначение модели", designation)) if not value]
        if missing:
            return Identification("insufficient", None, False, None, None, None, (),
                                  "серийный номер не указан; для определения по партнёру и модели не хватает: "
                                  + ", ".join(missing))
        found = tools.erp.find_partner_equipment(ctx, partner_id, designation)
        if len(found) == 1:
            return Identification("derived", "выведен", False, source, None, found[0], (),
                                  f"определено по партнёру {partner_id} и модели «{designation}»")
        candidates = found or tools.erp.find_partner_equipment(ctx, partner_id, None)
        reason = (f"по партнёру {partner_id} и модели «{designation}» найдено совпадений: {len(found)}" if found
                  else f"у партнёра {partner_id} нет оборудования модели «{designation}»")
        return Identification("ambiguous", None, False, source, None, None, tuple(candidates),
                              reason + "; выбор за диспетчером")


def with_stub_flag(result: ContextResult, stub: bool) -> ContextResult:
    return replace(result, stub=stub)
