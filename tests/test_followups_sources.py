"""Фоллоу-апы из двух источников: звонки Б24 (call.followup) и Синк (syncconnector.followup).

Что пинится:
- адрес метода: источник sync ходит в syncconnector.followup.*, call — в call.followup.*;
- list по умолчанию сливает оба источника, метит строку полем source и сортирует по дате;
- отказ одного источника (нет скоупа, нет модуля) не роняет другой: строки
  остальных + статус в `sources`; отказ всех — ошибка, а не пустой список;
- API get без source идёт в call, MCP требует явный source;
- Синк не фильтрует по участнику — запрос с participantId не уходит вовсе;
- у Синка любое AI-поле снижает лимит страницы до 20;
- пустая страница при hasMore=true — не конец списка, застрявший курсор — конец.
"""
from __future__ import annotations

import http.client
import io
import json
import urllib.error
from dataclasses import replace
from datetime import date
from urllib.parse import urlsplit

import pytest

from helpers import CFG

from b24_mcp import api
from b24_mcp import followups_core as fc_mod
from b24_mcp.followups_core import FollowUpsClient, FollowUpsError, collect

SCOPE_ERROR = {"error": {
    "code": "BITRIX_REST_V3_EXCEPTION_INSUFFICIENTSCOPEEXCEPTION",
    "message": "Недостаточно прав доступа: отсутствует необходимый scope.",
}}
D_FROM, D_TO = date(2025, 3, 1), date(2025, 3, 9)

CALL_ITEM = {"callId": 101, "callType": 2, "startDate": "2025-03-03T08:00:00+00:00",
             "overview": {"topic": "Тестовая тема звонка"}}
SYNC_ITEM = {"callId": 7, "callType": 1, "chatId": 3001,
             "startDate": "2025-03-05T09:00:00+00:00",
             "overview": {"topic": "Тестовая встреча"}}


class Response(io.BytesIO):
    status = 200
    headers = {}


class Portal:
    """Подмена urlopen для REST v3: ответ по имени метода, журнал (путь, тело)."""

    def __init__(self, answers: dict[str, dict]):
        self.answers = answers
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, req, timeout=None):
        path = urlsplit(req.full_url).path
        body = json.loads(req.data or b"{}")
        self.calls.append((path, body))
        method = path.rsplit("/", 1)[-1]
        assert method in self.answers, f"неожиданный вызов {method}"
        return Response(json.dumps(self.answers[method]).encode())

    def methods(self) -> list[str]:
        return [p.rsplit("/", 1)[-1] for p, _ in self.calls]


def page(*items):
    return {"result": {"items": list(items), "hasMore": False, "afterCursor": None}}


@pytest.fixture
def v3(monkeypatch):
    def install(answers):
        p = Portal(answers)
        monkeypatch.setattr("urllib.request.urlopen", p)
        monkeypatch.setattr("b24_mcp.config.load", lambda: CFG)
        monkeypatch.setattr(fc_mod.time, "sleep", lambda _s: None)
        return p
    return install


# ── источники и адреса ────────────────────────────────────────────────────

def test_sync_source_calls_syncconnector_methods(v3):
    p = v3({"syncconnector.followup.get": {"result": {"item": {"callId": 7}}}})
    FollowUpsClient(CFG, source="sync").get(7)
    assert p.methods() == ["syncconnector.followup.get"]
    # v3-путь вебхука: префикс api/ перед uid/token.
    assert p.calls[0][0] == "/rest/api/1/test/syncconnector.followup.get"


def test_collect_merges_sources_by_date_and_marks_source(v3):
    v3({"call.followup.list": page(CALL_ITEM), "syncconnector.followup.list": page(SYNC_ITEM)})
    items, errors = collect(CFG, D_FROM, D_TO)
    assert errors == {}
    assert [(i["callId"], i["source"]) for i in items] == [(7, "sync"), (101, "call")]


def test_collect_max_items_is_for_the_whole_list(v3):
    v3({"call.followup.list": page(CALL_ITEM), "syncconnector.followup.list": page(SYNC_ITEM)})
    items, _ = collect(CFG, D_FROM, D_TO, max_items=1)
    assert [i["callId"] for i in items] == [7]


def test_list_select_asks_chat_id_only_from_sync(v3):
    p = v3({"call.followup.list": page(), "syncconnector.followup.list": page()})
    collect(CFG, D_FROM, D_TO)
    selects = {path.rsplit("/", 1)[-1]: body["select"] for path, body in p.calls}
    assert "chatId" not in selects["call.followup.list"]
    assert "chatId" in selects["syncconnector.followup.list"]


def test_chat_id_is_valid_select_only_for_sync():
    assert fc_mod.validate_select(["chatId"], "sync") == (["chatId"], False)
    with pytest.raises(FollowUpsError, match="invalid_select_field"):
        fc_mod.validate_select(["chatId"], "call")


def test_sync_page_limit_drops_to_20_with_any_ai_field(v3):
    p = v3({"call.followup.list": page(), "syncconnector.followup.list": page()})
    collect(CFG, D_FROM, D_TO)
    limits = {path.rsplit("/", 1)[-1]: b["pagination"]["limit"] for path, b in p.calls}
    # В списке есть overview.topic: для call это лёгкое поле, для Синка — уже AI.
    assert limits == {"call.followup.list": 50, "syncconnector.followup.list": 20}


def test_sync_rejects_participant_filter_without_request(v3):
    p = v3({"call.followup.list": page(CALL_ITEM)})
    items, errors = collect(CFG, D_FROM, D_TO, participant_id=5)
    assert [i["source"] for i in items] == ["call"]
    assert "sync" in errors and "участник" in str(errors["sync"])
    assert p.methods() == ["call.followup.list"]
    assert p.calls[0][1]["filter"]["participantId"] == 5


def test_hash_transport_does_not_capture_sync():
    """Кастомный rest.php-action оборачивает только модуль call — Синк идёт вебхуком."""
    cfg = replace(CFG, followups_hash="fixture")
    assert FollowUpsClient(cfg, source="call").transport == "action"
    assert FollowUpsClient(cfg, source="sync").transport == "webhook"


def test_unknown_source_is_rejected():
    with pytest.raises(FollowUpsError):
        FollowUpsClient(CFG, source="zoom")
    with pytest.raises(ValueError):
        api.followups_list(None, source="zoom")


# ── отказы источников ─────────────────────────────────────────────────────

def test_scope_error_names_missing_scope(v3):
    v3({"syncconnector.followup.list": SCOPE_ERROR})
    with pytest.raises(FollowUpsError) as error:
        list(FollowUpsClient(CFG, source="sync").list(D_FROM, D_TO))
    text = str(error.value)
    assert "syncconnector.followup.list" in text and "`syncconnector`" in text


def test_network_error_text_is_sanitized(monkeypatch):
    """Текст сетевой ошибки может нести путь с токеном — в ошибку он не попадает."""
    monkeypatch.setattr(fc_mod.time, "sleep", lambda _s: None)
    cfg = replace(CFG, webhook="https://portal.invalid/rest/1/s3cr3t/")

    def urlopen(req, timeout=None):
        raise urllib.error.URLError("refused at /rest/api/1/s3cr3t/call.followup.list")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(FollowUpsError) as error:
        list(FollowUpsClient(cfg).list(D_FROM, D_TO))
    assert "s3cr3t" not in str(error.value)
    assert error.value.is_fatal


def test_mcp_list_reports_source_status(v3):
    v3({"call.followup.list": page(CALL_ITEM), "syncconnector.followup.list": SCOPE_ERROR})
    res = api.followups_list(None, days=7)
    assert [r["source"] for r in res["items"]] == ["call"]
    assert res["sources"]["call"] == "ok"
    assert "syncconnector" in res["sources"]["sync"]


def test_mcp_list_all_sources_down_is_an_error_not_empty(v3):
    v3({"call.followup.list": SCOPE_ERROR, "syncconnector.followup.list": SCOPE_ERROR})
    with pytest.raises(FollowUpsError):
        api.followups_list(None, days=7)


def test_mcp_list_single_source(v3):
    p = v3({"syncconnector.followup.list": page(SYNC_ITEM)})
    res = api.followups_list(None, days=7, source="sync")
    assert p.methods() == ["syncconnector.followup.list"]
    assert res["sources"] == {"sync": "ok"}
    row = res["items"][0]
    assert (row["call_id"], row["source"], row["chat_id"]) == (7, "sync", 3001)


# ── get ───────────────────────────────────────────────────────────────────

def test_api_get_defaults_to_call(v3):
    p = v3({"call.followup.get": {"result": {"item": {"callId": 101}}}})
    res = api.followup_get(None, 101)
    assert p.methods() == ["call.followup.get"]
    assert res["source"] == "call" and res["chat_id"] is None


def test_mcp_get_passes_source(v3):
    p = v3({"syncconnector.followup.get": {"result": {"item": {
        "callId": 7, "chatId": 3001, "overview": {"topic": "Тестовая встреча"}}}}})
    res = api.followup_get(None, 7, source="sync")
    assert p.methods() == ["syncconnector.followup.get"]
    assert (res["source"], res["chat_id"], res["topic"]) == ("sync", 3001, "Тестовая встреча")


# ── пагинация ─────────────────────────────────────────────────────────────

def test_empty_page_with_has_more_is_not_the_end(monkeypatch):
    """Страница может прийти пустой при hasMore=true — листаем дальше."""
    pages = [
        {"result": {"items": [], "hasMore": True, "afterCursor": {"id": 1}}},
        {"result": {"items": [SYNC_ITEM], "hasMore": False, "afterCursor": None}},
    ]
    sent = []

    def urlopen(req, timeout=None):
        sent.append(json.loads(req.data))
        return Response(json.dumps(pages[len(sent) - 1]).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    items = list(FollowUpsClient(CFG, source="sync").list(D_FROM, D_TO))
    assert [i["callId"] for i in items] == [7]
    assert sent[1]["pagination"]["afterCursor"] == {"id": 1}


def test_stuck_cursor_stops_paging(monkeypatch):
    """Портал вернул тот же курсор — выходим, а не крутимся вечно."""
    stuck = {"result": {"items": [], "hasMore": True, "afterCursor": {"id": 1}}}
    sent = []

    def urlopen(req, timeout=None):
        sent.append(1)
        assert len(sent) <= 3, "пагинация зациклилась"
        return Response(json.dumps(stuck).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert list(FollowUpsClient(CFG, source="sync").list(D_FROM, D_TO)) == []
    assert len(sent) == 2


@pytest.mark.parametrize("bad_source", ["call", "sync"])
@pytest.mark.parametrize("failure", ["disconnect", "json", "unicode"])
@pytest.mark.parametrize("facade", [False, True], ids=["collect", "api"])
def test_failed_source_preserves_other_rows(monkeypatch, bad_source, failure, facade):
    calls = []

    def urlopen(req, timeout=None):
        method = urlsplit(req.full_url).path.rsplit("/", 1)[-1]
        calls.append(method)
        if method == fc_mod.SOURCE_SCOPE[bad_source] + ".followup.list":
            if failure == "disconnect":
                raise http.client.RemoteDisconnected("reset /rest/api/1/SECRET/method")
            return Response(b"not JSON" if failure == "json" else b"\xff")
        item = SYNC_ITEM if bad_source == "call" else CALL_ITEM
        return Response(json.dumps(page(item)).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr("b24_mcp.config.load", lambda: CFG)
    monkeypatch.setattr(fc_mod.time, "sleep", lambda _s: None)
    good_source = "sync" if bad_source == "call" else "call"
    if facade:
        res = api.followups_list(None, days=7)
        assert [row["source"] for row in res["items"]] == [good_source]
        assert res["sources"][good_source] == "ok"
        warning = res["sources"][bad_source]
    else:
        items, errors = collect(CFG, D_FROM, D_TO)
        assert [row["source"] for row in items] == [good_source]
        assert set(errors) == {bad_source}
        warning = str(errors[bad_source])
    assert "SECRET" not in warning
    assert ("сеть: RemoteDisconnected" if failure == "disconnect" else "ответ портала не JSON") in warning
    assert calls.count(fc_mod.SOURCE_SCOPE[bad_source] + ".followup.list") == (3 if failure == "disconnect" else 1)
    assert calls.count(fc_mod.SOURCE_SCOPE[good_source] + ".followup.list") == 1


@pytest.mark.parametrize("transport", ["webhook", "action", "refresh"])
@pytest.mark.parametrize("failure", [http.client.RemoteDisconnected, http.client.HTTPException, OSError,
                                    "json", "unicode"])
def test_followup_transports_wrap_network_and_invalid_json(monkeypatch, transport, failure):
    cfg = replace(CFG, followups_hash="HASH_SECRET", followups_action="read",
                  oauth_refresh_token="REFRESH_SECRET", oauth_client_id="client",
                  oauth_client_secret="CLIENT_SECRET")
    client = FollowUpsClient(cfg)
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req)
        if failure == "json":
            return Response(b"not JSON")
        if failure == "unicode":
            return Response(b"\xff")
        raise failure("reset /rest/api/1/SECRET/method")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr(fc_mod.time, "sleep", lambda _s: None)
    with pytest.raises(FollowUpsError) as error:
        if transport == "refresh":
            client._refresh_access_token()
        elif transport == "action":
            client._post_action("list", {})
        else:
            client._send_v3("https://portal.invalid/rest/api/1/test/call.followup.list", {})
    malformed = isinstance(failure, str)
    assert str(error.value) == ("ответ портала не JSON" if malformed else f"сеть: {failure.__name__}")
    assert len(calls) == (1 if malformed or transport == "refresh" else 3)


@pytest.mark.parametrize("transport", ["webhook", "action"])
def test_followup_disconnect_retries_can_recover(monkeypatch, transport):
    client = FollowUpsClient(replace(CFG, followups_hash="hash", followups_action="read"))
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req)
        if len(calls) == 1:
            raise http.client.RemoteDisconnected("closed")
        return Response(json.dumps({"status": "success", "result": {"items": []}}).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr(fc_mod.time, "sleep", lambda _s: None)
    if transport == "action":
        assert client._post_action("list", {}) == {"items": []}
    else:
        assert client._send_v3("https://portal.invalid/rest/api/1/test/call.followup.list", {})[0] == 200
    assert len(calls) == 2


def test_collect_passes_refreshed_oauth_tokens_to_next_source(monkeypatch):
    """Refresh у первого источника ротирует refresh token: второй берёт свежие."""
    cfg = replace(CFG, oauth_access_token="OLD_ACCESS", oauth_refresh_token="OLD_REFRESH",
                  oauth_client_id="client", oauth_client_secret="CLIENT_SECRET")
    seen = {}

    def fake_list(self, *_a, **_kw):
        seen[self._source] = (self._access_token, self._refresh_token)
        if self._source == "call":
            self._access_token, self._refresh_token = "NEW_ACCESS", "NEW_REFRESH"
        yield {"callId": 1 if self._source == "call" else 2,
               "startDate": "2026-11-09T10:00:00+03:00"}

    monkeypatch.setattr(FollowUpsClient, "list", fake_list)
    items, errors = collect(cfg, D_FROM, D_TO, sources=("call", "sync"))
    assert errors == {}
    assert sorted(i["source"] for i in items) == ["call", "sync"]
    assert seen["call"] == ("OLD_ACCESS", "OLD_REFRESH")
    assert seen["sync"] == ("NEW_ACCESS", "NEW_REFRESH")
