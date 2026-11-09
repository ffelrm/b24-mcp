"""Задания согласования: только чтение, ссылка от базы портала, отказ — не пустота."""
from dataclasses import replace

import pytest

from helpers import CFG

from b24_mcp import api
from b24_mcp.client import B24Error, Client

ITEM = {
    "ID": "15", "NAME": "Согласовать заявку", "DOCUMENT_NAME": "Тестовый документ",
    "DOCUMENT_URL": "/company/personal/bizproc/15/", "MODIFIED": "2025-01-10T10:00:00+00:00",
}


@pytest.mark.parametrize("document_url, expected", [
    ("https://other.example/doc/15", "https://other.example/doc/15"),
    ("http://other.example/doc/15", "http://other.example/doc/15"),
    ("/company/doc/15", "https://portal.invalid/company/doc/15"),
    ("company/doc/15", "https://portal.invalid/company/doc/15"),
])
def test_approvals_document_url(portal, document_url, expected):
    portal({"bizproc.task.list": [{"result": [{**ITEM, "DOCUMENT_URL": document_url}]}]})
    assert api.approvals(Client(CFG))[0]["url"] == expected


def test_approvals_url_never_carries_token(portal):
    portal({"bizproc.task.list": [{"result": [ITEM]}]})
    url = api.approvals(Client(CFG))[0]["url"]
    assert "/rest/" not in url and "test" not in url


def test_approvals_filter_and_select(portal):
    net = portal({"bizproc.task.list": [{"result": [ITEM]}]})
    api.approvals(Client(CFG))
    body = net.bodies[0]
    assert body["filter[USER_ID]"] == [str(CFG.user_id)]
    assert body["filter[STATUS]"] == ["0"]
    assert {body[f"select[{i}]"][0] for i in range(5)} == set(ITEM)
    assert net.posts == ["bizproc.task.list"]


def test_approvals_mcp_uses_read_client(portal, monkeypatch):
    from b24_mcp import server as mcp_server

    net = portal({"bizproc.task.list": [{"result": [ITEM]}]})
    monkeypatch.setattr(mcp_server, "_client", lambda: Client(CFG))
    assert mcp_server.approvals()[0]["url"] == "https://portal.invalid" + ITEM["DOCUMENT_URL"]
    assert net.posts == ["bizproc.task.list"]


def test_approvals_reads_all_pages(portal):
    net = portal({"bizproc.task.list": [
        {"result": [ITEM], "next": 50},
        {"result": [{**ITEM, "ID": "16"}]},
    ]})
    assert [item["ID"] for item in api.approvals(Client(CFG))] == ["15", "16"]
    assert net.posts == ["bizproc.task.list", "bizproc.task.list"]


def test_approvals_empty_is_empty(portal):
    portal({"bizproc.task.list": [{"result": []}]})
    assert api.approvals(Client(CFG)) == []


def test_approvals_requires_owner_before_network(portal):
    net = portal({})
    with pytest.raises(B24Error) as error:
        api.approvals(Client(replace(CFG, user_id=None)))
    assert error.value.code == "USER_ID_REQUIRED"
    assert net.posts == []


def test_approvals_access_error_is_not_empty(portal):
    """Нет скоупа bizproc — ошибка доступа, а не «ничего не ждёт решения»."""
    portal({"bizproc.task.list": [{"error": "ACCESS_DENIED"}]})
    with pytest.raises(B24Error) as error:
        api.approvals(Client(CFG))
    assert error.value.code == "ACCESS_DENIED" and error.value.is_fatal
