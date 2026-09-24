"""Демонстрационный интерфейс поверх API платформы (Streamlit).

Назначение — показать сквозной сценарий на защите: обращение, разбор,
рекомендация, решение человека, черновик в учётной системе.

Интерфейс ничего не вычисляет. Он отправляет обращение в API и показывает
ответ как есть: правило, гипотезу, гарантию, исполнение и срочность
определяет платформа, здесь они только выводятся. Своего состояния, кроме
идентификатора текущего обращения, интерфейс не хранит.

Запуск:
    API_URL=http://127.0.0.1:8000 streamlit run ui/app.py
"""

from __future__ import annotations

import base64
import os

import httpx
import streamlit as st

API_URL = os.environ.get("API_URL", "http://127.0.0.1:8000")
TIMEOUT = 300.0

# Пользователи демонстрации: права берутся из реестра платформы, здесь —
# только выбор, от чьего имени работать.
USERS = {
    "U-002 — диспетчер, Санкт-Петербург": "U-002",
    "U-019 — представитель партнёра CUST-008": "U-019",
    "U-008 — сервис-менеджер": "U-008",
}

EXAMPLES = {
    "Ошибка 50.2 на МФУ HP": "Площадка SPB-01: HP LaserJet Pro M4103dw, серийный HPL-M4103-77842, "
                             "ошибка 50.2, печать остановилась",
    "Правило, требующее оценки": "HP LaserJet Pro M4103dw, серийный HPL-M4103-77842: ошибка 49.4C.02. "
                                 "Ошибка появляется после отправки конкретного PDF. После очистки очереди и запуска "
                                 "без USB/LAN ошибка не возникает.",
    "Без серийного номера": "HP LaserJet Pro M4103dw, ошибка 50.2, не печатает",
    "Признак опасности": "На принтер HP HPL-M4103-77842 протечка с потолка, всё залито",
}

STATUS_TONE = {
    "ОжиданиеПодтверждения": "info",
    "ОжиданиеСогласования": "info",
    "ЧерновикСоздан": "success",
    "Эскалировано": "warning",
    "РекомендацияБезФактов": "warning",
    "Отклонено": "error",
}


def client() -> httpx.Client:
    return httpx.Client(base_url=API_URL, timeout=TIMEOUT)


def headers() -> dict:
    return {"X-User-Id": st.session_state["user_id"]}


def call(method: str, path: str, **kwargs) -> tuple[int, dict]:
    """Единственный способ получить данные: интерфейс сам ничего не считает."""
    try:
        with client() as api:
            response = api.request(method, path, headers=headers(), **kwargs)
    except httpx.HTTPError as error:
        st.error(f"Платформа недоступна: {error}")
        return 0, {}
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"detail": response.text}


def show_status(body: dict) -> None:
    status = body.get("status", "—")
    tone = STATUS_TONE.get(status, "info")
    getattr(st, tone)(f"**Состояние: {status}**" + (f"\n\n{body['status_reason']}" if body.get("status_reason") else ""))


def show_equipment(body: dict) -> None:
    identification = body.get("identification") or {}
    st.subheader("Оборудование")
    columns = st.columns(4)
    columns[0].metric("Оборудование", body.get("equipment_id") or "не определено")
    columns[1].metric("Серийный номер", identification.get("serial_number") or "—")
    columns[2].metric("Достоверность", identification.get("confidence_level") or "—")
    columns[3].metric("Источник", identification.get("source") or "—")
    if identification.get("discrepancy_flag"):
        st.warning("Признак расхождения: указанные данные не совпали с карточкой оборудования")
    risks = body.get("risk_signals") or []
    if risks:
        st.warning("Признаки риска по гарантии: " + ", ".join(sorted({r["kind"] for r in risks})))


def show_hypothesis(recommendation: dict) -> None:
    st.subheader("Гипотеза причины")
    if recommendation.get("sources_restricted"):
        st.info(recommendation.get("restriction_notice") or "Обоснование доступно сотрудникам сервисной организации.")
        return
    st.write(recommendation.get("hypothesis") or "—")
    st.caption(f"Уровень обоснования: {recommendation.get('evidence_level') or '—'}")
    sources = recommendation.get("sources") or []
    if sources:
        st.markdown("**Источники**")
        for source in sources:
            st.markdown(f"- `{source['reference']}` — {source['title']} / {source['section_title']} "
                        f"({source['confidentiality_level']})")


def show_decision(recommendation: dict, policy: dict | None) -> None:
    st.subheader("Решение платформы")
    left, right = st.columns(2)

    with left:
        st.markdown("**Правило**")
        st.write(f"{recommendation.get('rule_id')} — {recommendation.get('rule_category')}, "
                 f"уровень {recommendation.get('rule_level')}")
        if recommendation.get("prescribed_actions"):
            st.caption(recommendation["prescribed_actions"])
        if policy is not None:
            st.markdown("**Оценка правила (Policy Agent)**")
            st.write(f"{policy.get('result')} — {policy.get('reason')}")
            if policy.get("evidence"):
                st.caption("Основания: " + ", ".join(policy["evidence"]))

        warranty = recommendation.get("warranty") or {}
        st.markdown("**Гарантия (предварительно)**")
        st.write(f"{warranty.get('status') or '—'}"
                 + (" — предварительная квалификация" if warranty.get("preliminary") else ""))
        if warranty.get("basis"):
            st.caption(warranty["basis"])

    with right:
        st.markdown("**Исполнение**")
        st.write(f"Способ: {recommendation.get('execution_mode')} ({recommendation.get('classifier_value')})")
        st.write(f"Тип: {recommendation.get('fulfillment_type') or '—'}, "
                 f"сервисный центр: {recommendation.get('service_center_id') or '—'}, "
                 f"исполнитель: {recommendation.get('technician_id') or '—'}")
        if recommendation.get("fulfillment_basis"):
            st.caption(recommendation["fulfillment_basis"])

        urgency = recommendation.get("urgency") or {}
        st.markdown("**Срочность**")
        st.write(f"{urgency.get('level') or '—'}, реакция в течение {urgency.get('reaction_target_hours') or '—'} ч")

        approval = recommendation.get("approval") or {}
        st.markdown("**Согласование**")
        st.write(approval.get("level") or "не требуется")
        if approval.get("basis"):
            st.caption("; ".join(approval["basis"]))


def show_draft(body: dict) -> None:
    draft = body.get("draft")
    if not draft:
        return
    st.success(f"Черновик заказа в учётной системе: **{draft['external_document_id']}**")
    st.caption(f"Ключ идемпотентности: {draft['idempotency_key']}; состояние: {draft['status']}")


def show_technical(request_id: str, body: dict, status: int, payload: dict) -> None:
    """Трассировка: доступна только служебным ролям — это решает платформа,
    интерфейс лишь показывает её ответ."""
    with st.expander("Технические подробности"):
        if status == 403:
            st.info("Трассировка доступна сотрудникам сервисной организации.")
            return
        if status != 200:
            st.warning(f"Шаги недоступны: {payload}")
            return

        recommendation = body.get("recommendation") or {}
        agents = recommendation.get("agents") or []
        if agents:
            st.markdown("**Агенты**")
            st.table([{"агент": a["agent"], "заглушка": "да" if a["stub"] else "нет"} for a in agents])

        steps = payload["steps"]
        st.markdown("**Шаги обращения**")
        st.table([{
            "№": step["seq"],
            "шаг": step["step_name"],
            "исполнитель": step["actor"],
            "статус": step["status"],
            "вызовы": ", ".join(c["tool_name"] for c in step["tool_calls"]) or "—",
        } for step in steps])

        version = steps[0]["artifact_version"] if steps else {}
        if version:
            st.caption(f"Версия набора артефактов: {version['id']}")
            st.code(f"правила: {version['rules_version']}\nкорпус: {version['corpus_version']}\n"
                    f"запросы: {version['prompt_version']}", language="text")

        selected = st.selectbox("Снимок шага", [s["step_name"] for s in steps], key=f"step-{request_id}")
        st.json(next(s for s in steps if s["step_name"] == selected)["state_snapshot"])


def policy_verdict(status: int, payload: dict) -> dict | None:
    """Оценка правила лежит в шаге policy: для ролей без доступа к трассировке
    её нет, и в интерфейсе она не появляется."""
    if status != 200:
        return None
    step = next((s for s in payload["steps"] if s["step_name"] == "policy"), None)
    return (step["state_snapshot"].get("output") or None) if step else None


def submit(text: str, impact: str, image) -> None:
    payload = {"text": text, "impact": impact}
    if image is not None:
        payload["image_base64"] = base64.b64encode(image.getvalue()).decode()
        payload["image_content_type"] = image.type
    with st.spinner("Платформа разбирает обращение…"):
        status, body = call("POST", "/v1/requests", json=payload)
    if status != 201:
        st.error(f"Обращение не принято ({status}): {body.get('detail')}")
        return
    st.session_state["request_id"] = body["request_id"]
    st.session_state["body"] = body


def decide(accept: bool) -> None:
    request_id = st.session_state["request_id"]
    with st.spinner("Записываем решение…"):
        status, body = call("POST", f"/v1/requests/{request_id}/confirm", json={"accept": accept})
    if status != 200:
        st.error(f"Решение не принято ({status}): {body.get('detail')}")
        return
    st.session_state["body"] = body


# --- страница ---------------------------------------------------------------------------------------------

st.set_page_config(page_title="Платформа сервисных обращений", page_icon="🛠", layout="wide")
st.title("Мультиагентная платформа разбора сервисных обращений")
st.caption(f"Демонстрация поверх API платформы: {API_URL}")

with st.sidebar:
    st.header("Демонстрация")
    label = st.selectbox("Пользователь", list(USERS), key="user_label")
    changed = st.session_state.get("user_id") != USERS[label]
    st.session_state["user_id"] = USERS[label]
    st.caption("Права берутся из реестра платформы по идентификатору пользователя.")
    # Смена пользователя — повод перечитать обращение от его имени: что он
    # увидит, решает платформа.
    if changed and st.session_state.get("request_id"):
        status, body = call("GET", f"/v1/requests/{st.session_state['request_id']}")
        if status == 200:
            st.session_state["body"] = body
        else:
            st.session_state.pop("body", None)
            st.warning(f"Обращение этому пользователю недоступно ({status}).")
    if st.button("Новое обращение", use_container_width=True):
        st.session_state.pop("request_id", None)
        st.session_state.pop("body", None)

st.subheader("Новое обращение")
example = st.selectbox("Пример обращения", ["— свой текст —", *EXAMPLES])
default_text = EXAMPLES.get(example, "")
text = st.text_area("Текст обращения", value=default_text, height=100,
                    placeholder="Опишите неисправность так, как её сообщил клиент")
left, right = st.columns([2, 3])
impact = left.selectbox("Влияние на работу", ["высокое", "среднее", "низкое"], index=1)
image = right.file_uploader("Фотография идентификационной таблички", type=["jpg", "jpeg", "png"])
if image is not None:
    right.image(image, width=260)

if st.button("Отправить обращение", type="primary", disabled=not text.strip()):
    submit(text, impact, image)

body = st.session_state.get("body")
if body:
    # Шаги запрашиваются один раз: из них берутся оценка правила и трассировка.
    steps_status, steps_payload = call("GET", f"/v1/requests/{body['request_id']}/steps")

    st.divider()
    show_status(body)
    show_equipment(body)

    recommendation = body.get("recommendation")
    if recommendation:
        show_hypothesis(recommendation)
        show_decision(recommendation, policy_verdict(steps_status, steps_payload))

        if body["status"] == "ОжиданиеПодтверждения":
            st.subheader("Решение человека")
            st.caption("Черновик в учётной системе создаётся только после подтверждения.")
            confirm, reject, _ = st.columns([1, 1, 4])
            if confirm.button("Подтвердить", type="primary", use_container_width=True):
                decide(True)
                st.rerun()
            if reject.button("Отклонить", use_container_width=True):
                decide(False)
                st.rerun()
        elif body["status"] == "ОжиданиеСогласования":
            st.info("Сумма выше порога: требуется согласование сервис-менеджера.")

    show_draft(body)
    show_technical(body["request_id"], body, steps_status, steps_payload)
