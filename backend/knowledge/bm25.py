"""Разреженное представление BM25 (ADR-0003, разреженная ветвь).

Считается на стороне клиента и кладётся в ту же точку Qdrant, что и плотный
вектор. Обратная частота документа (idf) считается самим Qdrant — коллекция
создана с `modifier: idf`. Поэтому:
- у фрагмента значения — насыщение частоты терма по формуле BM25;
- у запроса значения — единицы: вклад терма даёт idf на стороне хранилища.

Идентификатор терма — устойчивый хеш (blake2b), а не встроенный hash():
встроенный меняется от запуска к запуску, и индекс перестал бы совпадать
с запросом после перезапуска сервиса.

Токенизатор сохраняет коды ошибок целиком: «50.2», «13.B2.D1», «C6000» —
главный ключ к нужному разделу руководства, и разрывать их нельзя.
Словоформы приводятся стеммером: русский и английский по алфавиту токена;
токены с цифрами не стемминговать — это идентификаторы, а не слова.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import snowballstemmer

STOPWORDS_FILE = Path(__file__).with_name("stopwords.txt")
TOKEN = re.compile(r"[0-9a-zа-яё]+(?:[./-][0-9a-zа-яё]+)*")

K1 = 1.2
B = 0.75
# Средняя длина фрагмента в токенах. Значение фиксировано: пересчёт по корпусу
# менял бы значения уже загруженных точек при каждой догрузке.
AVG_LEN = 256.0


@dataclass(frozen=True)
class SparseVector:
    indices: list[int]
    values: list[float]

    def as_query(self) -> dict:
        return {"indices": self.indices, "values": self.values}


def term_id(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest(), "big")


@lru_cache(maxsize=1)
def stopwords() -> frozenset[str]:
    return frozenset(w.strip() for w in STOPWORDS_FILE.read_text(encoding="utf-8").split() if w.strip())


class Bm25:
    def __init__(self, language: str = "russian"):
        self._russian = snowballstemmer.stemmer(language).stemWord
        self._english = snowballstemmer.stemmer("english").stemWord
        self._stopwords = stopwords()

    def _stem(self, token: str) -> str:
        if any(c.isdigit() for c in token):
            return token          # код ошибки, артикул, обозначение модели
        if token[0] in "abcdefghijklmnopqrstuvwxyz":
            return self._english(token)
        return self._russian(token)

    def tokens(self, text: str) -> list[str]:
        found = TOKEN.findall((text or "").lower().replace("ё", "е"))
        return [self._stem(t) for t in found if t not in self._stopwords and len(t) > 1]

    def document(self, text: str) -> SparseVector:
        counts = Counter(self.tokens(text))
        if not counts:
            return SparseVector([], [])
        length = sum(counts.values())
        norm = K1 * (1 - B + B * length / AVG_LEN)
        items = sorted((term_id(token), tf * (K1 + 1) / (tf + norm)) for token, tf in counts.items())
        return SparseVector([i for i, _ in items], [round(v, 6) for _, v in items])

    def query(self, text: str) -> SparseVector:
        indices = sorted({term_id(token) for token in self.tokens(text)})
        return SparseVector(indices, [1.0] * len(indices))
