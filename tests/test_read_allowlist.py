"""Чтение — по белому списку; все входы транспорта закрыты по умолчанию.

Белый список совпадает с тем, что пакет реально вызывает: лишнего в нём нет,
а новый метод без осознанной записи в `_READ_METHODS` не уйдёт из read-only
клиента вовсе (WRITE_DISABLED ещё до сети).
"""
import ast
import re
from pathlib import Path

import pytest

from helpers import CFG

from b24_mcp import client as client_mod
from b24_mcp.client import B24Error, Client

PKG = Path(__file__).resolve().parent.parent / "src" / "b24_mcp"

# Фоллоу-апы (call.* / syncconnector.*) идут своим транспортом v3 мимо Client,
# поэтому в скан их скоупы не входят.
_SCOPES = ("im", "imbot", "imopenlines", "tasks", "task", "user", "disk", "calendar",
           "department", "crm", "sonet_group", "lists", "entity", "timeman", "bizproc")
_METHOD = re.compile(rf"(?:{'|'.join(_SCOPES)})(?:\.[A-Za-z0-9_]+)+")
_READ_VERBS = ("get", "list", "tail", "load", "current")


def _methods_in_code() -> set[str]:
    """Имена методов в коде пакета. client.py пропускаем: там сам белый список,
    и с ним сверка стала бы тавтологией."""
    found: set[str] = set()
    for path in PKG.rglob("*.py"):
        if path.name == "client.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and _METHOD.fullmatch(node.value):
                found.add(node.value)
    return found


def test_allowlist_matches_code():
    """Каждый метод из кода — в белом списке, и в списке нет ничего сверх кода."""
    found = _methods_in_code()
    assert {"user.get", "tasks.task.list", "im.dialog.messages.get"} <= found  # скан видит вызовы
    assert found == set(client_mod._READ_METHODS), sorted(found ^ set(client_mod._READ_METHODS))


def test_allowlist_has_only_read_verbs():
    for method in client_mod._READ_METHODS:
        assert method.rsplit(".", 1)[-1].lower().startswith(_READ_VERBS), method


@pytest.mark.parametrize("method", sorted(client_mod._READ_METHODS))
def test_read_method_goes_without_allow_write(portal, method):
    net = portal({method: [{"result": []}]})
    assert Client(CFG).call(method) == []
    assert net.posts == [method]


@pytest.mark.parametrize("entry", ["call", "call_list", "call_batch", "raw_batch"])
@pytest.mark.parametrize("method", ["im.message.add", "future.method.execute", "user.get.json", "batch"])
def test_non_read_is_gated_before_network(portal, entry, method):
    net = portal({})
    c = Client(CFG)
    with pytest.raises(B24Error) as error:
        if entry == "call_list":
            list(c.call_list(method))
        elif entry == "call_batch":
            c.call_batch({"one": (method, {})})
        elif entry == "raw_batch":
            c.call("batch", {"cmd": {"read": "user.get?ID=1", "one": method + "?ID=1"}})
        else:
            c.call(method)
    assert error.value.code == "WRITE_DISABLED"
    assert error.value.kind == client_mod.KIND_WRITE
    assert net.posts == []


@pytest.mark.parametrize("entry", ["call", "call_list", "call_batch", "raw_batch"])
def test_unknown_method_never_retried(portal, entry):
    """Неизвестный метод считается записью и при allow_write=True: без повтора."""
    method = "future.method.execute"
    endpoint = "batch" if "batch" in entry else method
    net = portal({endpoint: [TimeoutError(), {"result": []}]})
    c = Client(CFG, allow_write=True)
    with pytest.raises(B24Error) as error:
        if entry == "call_list":
            list(c.call_list(method))
        elif entry == "call_batch":
            c.call_batch({"one": (method, {})})
        elif entry == "raw_batch":
            c.call("batch", {"cmd": {"one": method + "?ID=1"}})
        else:
            c.call(method)
    assert error.value.code == "UNKNOWN_OUTCOME"
    assert net.posts == [endpoint]


@pytest.mark.parametrize("entry", ["call", "call_list", "call_batch"])
def test_read_batch_is_allowed_and_retried(portal, entry):
    result = {"result": {"one": [{"ID": "1"}]}, "result_error": {}}
    net = portal({"batch": [TimeoutError(), {"result": result}]})
    c = Client(CFG)
    if entry == "call_batch":
        assert c.call_batch({"one": ("user.get", {"ID": 1})}) == (result["result"], {})
    elif entry == "call_list":
        assert list(c.call_list("batch", {"cmd": {"one": "user.get?ID=1"}})) == []
    else:
        assert c.call("batch", {"cmd": {"one": "user.get?ID=1"}}) == result
    assert net.posts == ["batch", "batch"]


@pytest.mark.parametrize("key", ["cmd", "cmd[hidden]suffix", "cmd[hidden][nested]"])
def test_ambiguous_batch_command_keys_fail_closed(portal, key):
    # PHP может разобрать кривые ключи как команды batch: такие ключи не
    # пропускаем молча рядом с корректной командой чтения.
    net = portal({})
    with pytest.raises(B24Error) as error:
        Client(CFG).call("batch", {"cmd[safe]": "user.get?ID=1", key: "im.message.add?MESSAGE=x"})
    assert error.value.code == "WRITE_DISABLED"
    assert net.posts == []
