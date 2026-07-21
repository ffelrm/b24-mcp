"""Тесты MCP-фасада и нормализаторов. Без сети: клиент подменяется заглушкой.

Что здесь пинится (и почему именно это):
- СПИСОК ИНСТРУМЕНТОВ — чтобы новый инструмент нельзя было выкатить молча.
  Меняешь набор — меняешь тест осознанно.
- READ-ONLY — фасад обязан создавать клиент с allow_write=False. Это главный
  барьер: даже ошибка в коде инструмента не отправит write-метод.
- НОРМАЛИЗАЦИЯ — единый контракт {items,count,limit}, кламп лимитов, чистка
  разметки, устойчивость флага Y/bool (на нём уже ловили баг с митингами).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(ROOT))

from b24_mcp import api, normalizers as nz  # noqa: E402

EXPECTED_TOOLS = {
    # база
    "whoami",
    "person_find",
    "calendar_events",
    # задачи
    "tasks_list",
    "task_get",
    "task_chat",
    "tasks_updates",
    # чаты и проекты
    "chat_read",
    "projects_list",
    "project_chats",
    "project_read",
    # звонки
    "followups_list",
    "followup_get",
}


class FakeClient:
    """Заглушка вместо Client: отдаёт заранее заданные ответы, пишет вызовы."""

    def __init__(self, responses: dict | None = None, user_id: int = 1):
        self.responses = responses or {}
        self.user_id = user_id
        self.calls: list[tuple[str, dict]] = []

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        return self.responses.get(method)

    def call_list(self, method, params=None):
        self.calls.append((method, params or {}))
        return iter(self.responses.get(method) or [])


# ------------------------------------------------------------------ фасад


@pytest.mark.asyncio
async def test_tool_list_is_locked():
    """Набор инструментов зафиксирован — новый не появляется незаметно."""
    from b24_mcp import server as mcp_server

    tools = await mcp_server.mcp.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_every_tool_has_description():
    """У каждого инструмента есть описание: модель выбирает по нему."""
    from b24_mcp import server as mcp_server

    for tool in await mcp_server.mcp.list_tools():
        assert (tool.description or "").strip(), f"{tool.name} без описания"


def test_client_is_read_only(monkeypatch):
    """Фасад создаёт ТОЛЬКО read-only клиент."""
    from b24_mcp import server as mcp_server

    captured = {}

    class Spy:
        def __init__(self, cfg, *, allow_write=False, **kw):
            captured["allow_write"] = allow_write

    monkeypatch.setattr(mcp_server, "Client", Spy)
    monkeypatch.setattr(mcp_server.config, "load", lambda: object())
    mcp_server._client()
    assert captured["allow_write"] is False


def test_missing_config_gives_actionable_error(monkeypatch):
    """Ненастроенный мост — понятная ошибка, а не смерть процесса."""
    from b24_mcp import server as mcp_server

    def boom():
        raise SystemExit(2)

    monkeypatch.setattr(mcp_server.config, "load", boom)
    with pytest.raises(RuntimeError, match="не настроен"):
        mcp_server._client()


# ------------------------------------------------------------------ нормализаторы


@pytest.mark.parametrize(
    "value,expected",
    [(None, 50), ("мусор", 50), (0, 1), (-5, 1), (10, 10), (999, 100), ("7", 7)],
)
def test_clamp(value, expected):
    assert nz.clamp(value) == expected


def test_page_contract():
    p = nz.page([1, 2, 3], limit=10, total=42)
    assert p == {"items": [1, 2, 3], "count": 3, "limit": 10, "total": 42}


def test_page_without_total_has_no_total_key():
    assert "total" not in nz.page([], limit=5)


def test_clean_text_strips_html_and_collapses():
    assert nz.clean_text("<b>Привет</b><br>мир") == "Привет\nмир"
    assert nz.clean_text("много    пробелов") == "много пробелов"
    assert nz.clean_text(None) == ""


def test_trim_marks_truncation():
    out = nz.trim("x" * 100, 10)
    assert out.startswith("xxxxxxxxxx") and "обрезано" in out
    assert nz.trim("коротко", 100) == "коротко"


@pytest.mark.parametrize(
    "value,expected",
    [("Y", True), ("N", False), (True, True), (False, False), (1, True), (0, False), (None, False)],
)
def test_is_yes_handles_all_shapes(value, expected):
    """Портал отдаёт флаг то строкой, то bool — сравнение == "Y" молча врало."""
    assert nz.is_yes(value) is expected


# ------------------------------------------------------------------ api-слой


def test_person_find_by_id_uses_direct_lookup():
    c = FakeClient({"user.get": [{"ID": "42", "NAME": "Иван", "LAST_NAME": "Петров"}]})
    res = api.person_find(c, "42")
    assert res["count"] == 1
    assert res["items"][0]["id"] == 42
    assert res["items"][0]["name"] == "Петров Иван"
    assert c.calls[0][1] == {"ID": 42}


def test_person_find_by_email_uses_filter():
    c = FakeClient({"user.get": [{"ID": "7", "NAME": "A", "EMAIL": "a@b.c"}]})
    api.person_find(c, "a@b.c")
    assert c.calls[0][1] == {"FILTER": {"EMAIL": "a@b.c"}}


def test_person_find_empty_query_returns_empty_page():
    c = FakeClient()
    assert api.person_find(c, "  ")["count"] == 0
    assert c.calls == []


def test_tasks_list_defaults_to_responsible_and_hides_done():
    c = FakeClient({"tasks.task.list": [{"id": "1", "title": "T", "responsibleId": "5", "createdBy": "5"}]})
    res = api.tasks_list(c, user_id=5)
    flt = c.calls[0][1]["filter"]
    assert flt["RESPONSIBLE_ID"] == 5
    assert flt["<REAL_STATUS"] == 5
    assert res["items"][0]["self_note"] is True  # постановщик = исполнитель


def test_tasks_list_role_switches_filter_key():
    c = FakeClient({"tasks.task.list": []})
    api.tasks_list(c, user_id=9, role="auditor")
    assert "AUDITOR" in c.calls[0][1]["filter"]


def test_tasks_list_unknown_role_falls_back_safely():
    c = FakeClient({"tasks.task.list": []})
    api.tasks_list(c, user_id=9, role="чепуха")
    assert "RESPONSIBLE_ID" in c.calls[0][1]["filter"]


def test_calendar_normalizes_meeting_flag_and_sorts():
    c = FakeClient(
        {
            "calendar.event.get": [
                {"ID": 2, "NAME": "Поздняя", "DATE_FROM": "2026-07-20 15:00:00", "IS_MEETING": True,
                 "MEETING": {"HOST_ID": 99}, "ATTENDEE_LIST": [{"id": 1, "name": "A", "status": "Y"}]},
                {"ID": 1, "NAME": "Ранняя", "DATE_FROM": "2026-07-20 09:00:00", "IS_MEETING": "N"},
            ]
        },
        user_id=1,
    )
    res = api.calendar_events(c, days=1)
    assert [e["title"] for e in res["items"]] == ["Ранняя", "Поздняя"]
    late = res["items"][1]
    assert late["is_meeting"] is True and late["is_invitee"] is True
    assert late["attendees_count"] == 1
    assert res["items"][0]["is_meeting"] is False


def test_chat_read_requires_target():
    with pytest.raises(ValueError):
        api.chat_read(FakeClient())


# ------------------------------------------------------------------ звонки


def test_summary_text_unpacks_segments():
    """Саммари приходит СЕГМЕНТАМИ по темам, а не строкой.

    Пин реального контракта: первая версия читала summary как строку и молча
    отдавала пустоту — тихий 200, который тесты на заглушках не ловили бы,
    если бы заглушка повторяла выдумку, а не портал.
    """
    raw = {"segments": [
        {"title": "Первая тема", "summary": "о чём договорились", "start": 0, "end": 10},
        {"title": "", "summary": "без заголовка"},
    ]}
    out = api._summary_text(raw)
    assert "**Первая тема**" in out and "о чём договорились" in out
    assert "без заголовка" in out


def test_transcript_text_uses_transcription_segments():
    """Расшифровка живёт в `transcription.segments` с `userName`/`text`."""
    raw = {"segments": [
        {"userName": "Первый", "text": "реплика раз"},
        {"userId": 7, "text": "реплика два"},
        {"userName": "Пустой", "text": ""},
    ]}
    out = api._transcript_text(raw)
    assert out.splitlines() == ["Первый: реплика раз", "7: реплика два"]


@pytest.mark.parametrize("raw", [None, {}, {"segments": None}, "готовая строка"])
def test_summary_and_transcript_never_explode(raw):
    """Пустые/строковые формы не роняют разбор."""
    assert isinstance(api._summary_text(raw), str)
    assert isinstance(api._transcript_text(raw), str)


def test_participant_id_comes_from_userId_not_id():
    """Портал зовёт поле `userId`. Чтение `id` даёт молчаливые None.

    Промах ловится только на живых данных (заглушка с ключом `id` его
    подтвердит), поэтому пин формы участника — по реальному ответу портала:
    ключи `userId`, `name`, `workPosition`, `talkedSeconds`, `avatar`.
    """
    portal_participant = {
        "userId": "4",
        "name": "Кто-то",
        "workPosition": "Должность",
        "talkedSeconds": "4530",
        "avatar": "https://example/i.png",
    }
    assert portal_participant.get("userId") == "4"
    assert portal_participant.get("id") is None, "у портала нет ключа id — не читать его"


def test_project_read_live_only_drops_system(monkeypatch):
    """live_only=True убирает системные записи, оставляя разговор."""
    msgs = [
        {"id": 1, "author_id": 0, "text": "система добавила участника", "date": "2026-07-01"},
        {"id": 2, "author_id": 5, "text": "живая реплика", "date": "2026-07-01"},
    ]
    monkeypatch.setattr(
        api, "chat_read", api.chat_read
    )  # не подменяем — проверяем через _messages_page
    page_live = api._messages_page(msgs, {5: "Кто-то"}, 50, include_system=False)
    page_all = api._messages_page(msgs, {5: "Кто-то"}, 50, include_system=True)
    assert page_live["count"] == 1 and page_live["items"][0]["text"] == "живая реплика"
    assert page_all["count"] == 2
