"""Измерения сквозного сценария на узле (ADR-0007, план мощностей).

Не тест: собирает цифры для плана мощностей и раздела экономики. Запускается
против развёрнутого стека:

    python tests/eval/measure.py [число прогонов]

Первый прогон прогревает компиляцию грамматики схем на сервере инференса и
показателен только как «холодный».
"""

from __future__ import annotations

import datetime as dt
import os
import statistics
import sys
import time

import httpx

API = os.environ.get("API_URL", "http://api:8000")
TEXT = ("Площадка SPB-01: HP LaserJet Pro M4103dw, серийный HPL-M4103-77842, "
        "ошибка 50.2, печать остановилась")
USER = {"X-User-Id": "U-002"}


def seconds(started: str, finished: str) -> float:
    return (dt.datetime.fromisoformat(finished) - dt.datetime.fromisoformat(started)).total_seconds()


def run(client: httpx.Client) -> dict:
    began = time.perf_counter()
    created = client.post("/v1/requests", headers=USER, json={"text": TEXT, "impact": "среднее"})
    created.raise_for_status()
    request_id = created.json()["request_id"]
    analysis = time.perf_counter() - began

    began = time.perf_counter()
    confirmed = client.post(f"/v1/requests/{request_id}/confirm", headers=USER, json={"accept": True})
    confirmed.raise_for_status()
    confirmation = time.perf_counter() - began

    steps = client.get(f"/v1/requests/{request_id}/steps", headers=USER).json()["steps"]
    measured = {"разбор обращения": analysis, "подтверждение и черновик": confirmation,
                "сценарий целиком": analysis + confirmation}
    for step in steps:
        if step["finished_at"]:
            measured[f"шаг {step['step_name']}"] = seconds(step["started_at"], step["finished_at"])
    for step in steps:
        for call in step["tool_calls"]:
            measured[f"вызов {call['tool_name']}"] = measured.get(f"вызов {call['tool_name']}", 0.0) \
                + call["duration_ms"] / 1000
    measured["состояние"] = confirmed.json()["status"]
    return measured


def main() -> None:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    with httpx.Client(base_url=API, timeout=300) as client:
        results = [run(client) for _ in range(runs)]

    keys = [k for k in results[0] if k != "состояние"]
    interesting = [k for k in keys if k.startswith("вызов") or k.startswith("шаг ") or " " in k]
    print(f"\nПрогонов: {runs}; состояния: {[r['состояние'] for r in results]}\n")
    print(f"{'Измерение':44s} {'холодный':>10s} {'медиана':>10s} {'минимум':>10s}")
    for key in sorted(set(interesting)):
        values = [r[key] for r in results if key in r]
        if not values:
            continue
        warm = values[1:] or values
        print(f"{key:44s} {values[0]:10.2f} {statistics.median(warm):10.2f} {min(warm):10.2f}")


if __name__ == "__main__":
    main()
