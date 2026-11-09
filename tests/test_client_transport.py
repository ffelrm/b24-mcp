"""Транспорт клиента: ретраи чтения, неясный исход записи, причины отказов.

Сервер выставляет только чтение, но механика клиента общая: если клиент
когда-нибудь создадут с allow_write=True, write-запрос после таймаута, обрыва
или 5xx НЕ повторяется — он мог выполниться, и повтор задвоил бы действие.
Такой исход — B24Error UNKNOWN_OUTCOME. Read идемпотентен, его ретраи остаются.

Тесты офлайн: `urllib.request.urlopen` подменён. В сценариях после сбоя лежит
успешный ответ — ровно то, что получил бы лишний повтор.
"""
from __future__ import annotations

import http.client
import urllib.error

import pytest

from helpers import CFG, http_error

from b24_mcp import client as client_mod
from b24_mcp.client import B24Error, Client
from b24_mcp.followups_core import FollowUpsError


# ── чтение: ретраи остаются ────────────────────────────────────────────────

def test_read_timeout_is_retried(portal):
    net = portal({"im.dialog.messages.get": [
        TimeoutError("timed out"),
        {"result": {"messages": [{"id": 1}]}},
    ]})

    got = Client(CFG).call("im.dialog.messages.get", {"DIALOG_ID": "chat1"})

    assert got == {"messages": [{"id": 1}]}
    assert net.posts == ["im.dialog.messages.get", "im.dialog.messages.get"]


def test_read_5xx_is_retried(portal):
    net = portal({"tasks.task.get": [http_error(502), http_error(503), {"result": {"task": {"id": 1}}}]})

    got = Client(CFG).call("tasks.task.get", {"taskId": 1})

    assert got == {"task": {"id": 1}}
    assert net.posts == ["tasks.task.get"] * 3


def test_read_network_error_is_retried(portal):
    net = portal({"user.get": [
        urllib.error.URLError(ConnectionResetError(54, "Connection reset by peer")),
        {"result": [{"ID": "1"}]},
    ]})

    assert Client(CFG).call("user.get", {"ID": 1}) == [{"ID": "1"}]
    assert net.posts == ["user.get", "user.get"]


def test_read_list_goes_through_shared_transport(portal):
    """call_list ходит через общий транспорт: ретрай и классифицированная ошибка,
    а не сырой URLError с трейсбеком."""
    net = portal({"user.get": [TimeoutError(), TimeoutError(), TimeoutError()]})

    with pytest.raises(B24Error) as ei:
        list(Client(CFG).call_list("user.get", {}))

    assert ei.value.code == "TIMEOUT" and ei.value.kind == client_mod.KIND_TRANSPORT
    assert net.posts == ["user.get"] * 3


@pytest.mark.parametrize("failure", [
    http.client.RemoteDisconnected,
    http.client.HTTPException,
    OSError,
])
def test_read_disconnect_is_safe_b24_error(portal, failure):
    """Обрыв чтения классифицирован, исходный текст с токеном скрыт; ретрая нет."""
    net = portal({"user.get": [failure("reset at /rest/1/SECRET/user.get.json")]})

    with pytest.raises(B24Error) as error:
        Client(CFG).call("user.get", {"ID": 1})
    assert error.value.code == "NETWORK"
    assert error.value.kind == client_mod.KIND_TRANSPORT
    assert str(error.value).endswith(f"обрыв соединения: {failure.__name__}")
    assert "SECRET" not in str(error.value)
    assert error.value.__suppress_context__
    assert net.posts == ["user.get"]


# ── запись: исход неизвестен, повтора нет ─────────────────────────────────

def test_write_timeout_sends_exactly_one_post(portal):
    net = portal({"im.message.add": [TimeoutError("timed out"), {"result": 100}]})
    writer = Client(CFG, allow_write=True, timeout=60.0)

    with pytest.raises(B24Error) as ei:
        writer.call("im.message.add", {"DIALOG_ID": "chat1", "MESSAGE": "текст"})

    assert net.posts == ["im.message.add"]
    assert ei.value.code == "UNKNOWN_OUTCOME"
    assert "действие МОГЛО выполниться; сверь результат перед повтором" in str(ei.value)


UNCLEAR = {
    "timeout": lambda: TimeoutError("timed out"),
    "http-500": lambda: http_error(500),
    "http-502": lambda: http_error(502),
    "http-503": lambda: http_error(503),
    "http-504": lambda: http_error(504),
    # Любой 5xx: за прокси-кодами вроде 524 бэкенд мог успеть выполнить запрос.
    "http-524": lambda: http_error(524),
    "network": lambda: urllib.error.URLError(ConnectionResetError(54, "Connection reset by peer")),
    # Обрыв на чтении ответа urllib отдаёт сырым, мимо URLError: запрос уже ушёл целиком.
    "disconnect": lambda: http.client.RemoteDisconnected("Remote end closed connection without response"),
}


@pytest.mark.parametrize("failure", list(UNCLEAR.values()), ids=list(UNCLEAR))
def test_write_is_not_retried_after_unclear_failure(portal, failure):
    net = portal({"tasks.task.add": [failure(), {"result": {"task": {"id": 1}}}]})

    with pytest.raises(B24Error) as ei:
        Client(CFG, allow_write=True).call("tasks.task.add", {"fields": {"TITLE": "т"}})

    assert net.posts == ["tasks.task.add"]
    assert ei.value.code == "UNKNOWN_OUTCOME"


def test_unknown_outcome_text_never_carries_exception_text(portal):
    """Текст сетевого исключения в строку ошибки не идёт: у части исключений
    http.client в нём полный путь запроса, то есть токен хука."""
    portal({"im.message.add": [ConnectionResetError(54, "reset at /rest/1/SECRET/im.message.add.json")]})

    with pytest.raises(B24Error) as ei:
        Client(CFG, allow_write=True).call("im.message.add", {"DIALOG_ID": "1", "MESSAGE": "т"})

    assert ei.value.code == "UNKNOWN_OUTCOME"
    assert "SECRET" not in str(ei.value)


@pytest.mark.parametrize("method,allow_write", [("user.get", False), ("im.message.add", True)])
def test_invalid_url_is_safe_config_error(portal, method, allow_write):
    """Кривой URL ловится до отправки: CONFIG для чтения и записи, без токена."""
    net = portal({method: [http.client.InvalidURL("bad URL /rest/1/SECRET/method.json")]})

    with pytest.raises(B24Error) as error:
        Client(CFG, allow_write=allow_write).call(method)
    assert error.value.code == "CONFIG"
    assert "некорректный URL вебхука" in str(error.value)
    assert "SECRET" not in str(error.value)
    assert error.value.__suppress_context__
    assert net.posts == [method]


def test_write_definite_refusal_stays_definite(portal):
    """4xx с телом ошибки — портал ответил и отказал. Исход известен, код сохраняется."""
    net = portal({"tasks.task.add": [
        http_error(400, {"error": "ERROR_ARGUMENT", "error_description": "нет TITLE"}),
    ]})

    with pytest.raises(B24Error) as ei:
        Client(CFG, allow_write=True).call("tasks.task.add", {"fields": {}})

    assert net.posts == ["tasks.task.add"]
    assert ei.value.code == "ERROR_ARGUMENT"


def test_batch_with_write_is_not_retried(portal):
    net = portal({"batch": [
        TimeoutError("timed out"),
        {"result": {"result": {"m": 1}, "result_error": {}}},
    ]})

    with pytest.raises(B24Error) as ei:
        Client(CFG, allow_write=True).call_batch(
            {"m": ("im.message.add", {"DIALOG_ID": "chat1", "MESSAGE": "т"})}
        )

    assert net.posts == ["batch"]
    assert ei.value.code == "UNKNOWN_OUTCOME"


def test_read_only_batch_is_retried(portal):
    net = portal({"batch": [
        TimeoutError("timed out"),
        {"result": {"result": {"a": {"x": 1}}, "result_error": {}}},
    ]})

    ok, errs = Client(CFG).call_batch({"a": ("im.dialog.messages.get", {"DIALOG_ID": "chat1"})})

    assert ok == {"a": {"x": 1}} and errs == {}
    assert net.posts == ["batch", "batch"]


# ── причины отказов ────────────────────────────────────────────────────────

def test_unknown_outcome_has_its_own_exit_code_and_hint():
    """Код «сеть легла» значит «повтори позже». Здесь повтор и есть опасность,
    поэтому у неясного исхода свой код и своя подсказка."""
    e = B24Error("im.message.add", "UNKNOWN_OUTCOME", "ответ портала не получен")
    taken = {
        client_mod.EXIT_OK, client_mod.EXIT_GENERIC, client_mod.EXIT_CONFIG,
        client_mod.EXIT_TRANSPORT, client_mod.EXIT_AUTH, client_mod.EXIT_METHOD,
        client_mod.EXIT_WRITE, client_mod.EXIT_PARTIAL,
    }

    assert e.exit_code == client_mod.EXIT_UNKNOWN_OUTCOME
    assert e.exit_code not in taken
    assert e.is_fatal  # гасить break/continue нельзя, как и транспортный отказ
    assert "МОГЛО" in e.hint


@pytest.mark.parametrize("code, kind, fatal", [
    ("NETWORK", client_mod.KIND_TRANSPORT, True),
    ("TIMEOUT", client_mod.KIND_TRANSPORT, True),
    ("HTTP_503", client_mod.KIND_TRANSPORT, True),
    ("ACCESS_DENIED", client_mod.KIND_AUTH, True),
    ("INSUFFICIENT_SCOPE", client_mod.KIND_AUTH, True),
    ("ERROR_METHOD_NOT_FOUND", client_mod.KIND_METHOD, False),
    ("WRITE_DISABLED", client_mod.KIND_WRITE, False),
])
def test_error_kind_by_portal_code(code, kind, fatal):
    e = B24Error("user.get", code, "")
    assert e.kind == kind
    assert e.is_fatal is fatal
    assert e.hint == client_mod.HINT_BY_KIND[kind]


@pytest.mark.parametrize("msg, kind", [
    ("сеть: connection refused", client_mod.KIND_TRANSPORT),
    ("ACCESSDENIED: access denied", client_mod.KIND_AUTH),
    ("invalid_select_field: неизвестное поле", client_mod.KIND_METHOD),
])
def test_portal_error_kind_by_message(msg, kind):
    """У фоллоу-апов свой транспорт и нет кода — причина выводится из текста."""
    e = FollowUpsError(msg)
    assert e.kind == kind
    assert e.exit_code == client_mod._EXIT_BY_KIND[kind]


def test_client_portal_base_has_no_token():
    assert Client(CFG).portal == "https://portal.invalid"
    assert "test" not in Client(CFG).portal
