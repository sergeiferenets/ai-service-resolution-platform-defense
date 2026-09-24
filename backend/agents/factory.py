"""Сборка агентов живого режима.

Все три агента ADR-0002 настоящие. Policy Agent оценивает только правила,
размеченные в своде как требующие оценки, и при любом сомнении отвечает
«insufficient» — обращение уходит человеку. Заглушки остаются для модульных
и изолированных тестов (agents/stubs.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.agents.context.agent import ContextAgent, LlmTextExtractor, VisionPlateReader
from backend.agents.context.prompt import PLATE_READING, TEXT_EXTRACTION
from backend.agents.context.schema import PlateReading, TextExtraction
from backend.agents.contracts import Agents
from backend.agents.knowledge.agent import FRAGMENT_LIMIT, LlmKnowledgeAgent
from backend.agents.knowledge.prompt import HYPOTHESIS
from backend.agents.knowledge.schema import Hypothesis
from backend.agents.policy.agent import LlmPolicyAgent
from backend.agents.policy.prompt import RULE_EVALUATION
from backend.agents.policy.schema import RuleApplicability
from backend.config import Settings
from backend.guardrails.input.check import InputRules, find_injection
from backend.knowledge.bm25 import Bm25
from backend.knowledge.chunks import MAX_CHARS
from backend.knowledge.qdrant import SEARCH_BUDGET_S, QdrantClient, QdrantEndpoint
from backend.knowledge.search import CANDIDATES, Retriever
from backend.llm.client import Endpoint, LlmClient
from backend.rules.catalog import Catalog
from backend.tools.error_codes import ErrorCodeDirectory

# ADR-0007: языковая модель — 60 с, распознавание изображения — 30 с, поиск — 5 с.
LLM_BUDGET_S = 60.0
VISION_BUDGET_S = 30.0


@dataclass(frozen=True)
class AgentSet:
    agents: Agents
    prompt_versions: dict[str, str]
    chunker_version: str
    retrieval_config_version: str


def model_agents(settings: Settings, codes: ErrorCodeDirectory, input_rules: InputRules,
                 catalog: Catalog) -> AgentSet:
    if not all((settings.llm_model_revision, settings.vision_model_revision, settings.embed_model_revision)):
        raise RuntimeError("Живой режим требует LLM_MODEL_REVISION, VISION_MODEL_REVISION и EMBED_MODEL_REVISION")
    if not settings.vllm_api_key:
        raise RuntimeError("Не задан VLLM_API_KEY_FILE: без ключа сервис инференса отвечает 401")
    llm = LlmClient(Endpoint(settings.llm_base_url, settings.vllm_api_key, settings.llm_model, LLM_BUDGET_S))
    vision = LlmClient(Endpoint(settings.vision_base_url, settings.vllm_api_key, settings.vision_model,
                                VISION_BUDGET_S))
    embeddings = LlmClient(Endpoint(settings.embed_base_url, settings.vllm_api_key, settings.embed_model,
                                    SEARCH_BUDGET_S))
    qdrant = QdrantClient(QdrantEndpoint(settings.qdrant_url, settings.qdrant_api_key or "",
                                         settings.qdrant_collection))
    retriever = Retriever(qdrant, embeddings.embed, Bm25(settings.bm25_language))

    context = ContextAgent(LlmTextExtractor(llm), VisionPlateReader(vision), codes,
                           injection=lambda value: find_injection(value, input_rules))
    knowledge = LlmKnowledgeAgent(retriever, llm, codes,
                                  injection=lambda value: find_injection(value, input_rules))
    policy = LlmPolicyAgent(llm, catalog)

    return AgentSet(
        agents=Agents(context=context, knowledge=knowledge, policy=policy),
        prompt_versions={"context": TEXT_EXTRACTION.version(TextExtraction.model_json_schema()),
                         "plate": PLATE_READING.version(PlateReading.model_json_schema()),
                         "knowledge": HYPOTHESIS.version(Hypothesis.model_json_schema()),
                         "policy": RULE_EVALUATION.version(RuleApplicability.model_json_schema())},
        chunker_version=f"sections-{MAX_CHARS}",
        retrieval_config_version=f"rrf-{CANDIDATES}-{FRAGMENT_LIMIT}",
    )
