"""Группы и канбан: карточка группы, колонки, задачи группы. Без сети."""
from unittest.mock import Mock

import pytest

from helpers import CFG

from b24_mcp import api
from b24_mcp.client import B24Error, Client

GROUP_ID = 501
OWNER_ID = 7

STAGES = {
    "12": {"ID": "12", "TITLE": "На ревью", "SORT": "300", "SYSTEM_TYPE": "", "COLOR": "abc"},
    "11": {"ID": "11", "TITLE": "Новые", "SORT": "100", "SYSTEM_TYPE": "NEW"},
}
GROUP = [{"ID": str(GROUP_ID), "NAME": "Тестовая группа", "OWNER_ID": str(OWNER_ID),
          "PROJECT": "Y", "CLOSED": "N", "VISIBLE": "Y", "OPENED": "N"}]


class Fake:
    """Заглушка клиента: отвечает по методу и пишет журнал вызовов."""

    user_id = 1

    def __init__(self, stages=None, statuses=("5", "6", "2", "3")):
        self.calls = []
        self.stages = STAGES if stages is None else stages
        self.statuses = statuses
        self.consumed = 0

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "task.stages.get":
            return self.stages
        if method == "sonet_group.get":
            return GROUP
        if method == "user.get":
            return []
        raise AssertionError(method)

    def call_list(self, method, params):
        self.calls.append((method, params))
        for n, status in enumerate(self.statuses):
            self.consumed += 1
            yield {"id": str(200 - n), "status": status, "stageId": "0" if n == 1 else "12",
                   "priority": "2", "closedDate": "", "createdBy": str(OWNER_ID),
                   "deadline": "2025-12-01", "title": "Тест"}


# ── карточка группы и колонки ─────────────────────────────────────────────

def test_stage_shapes_and_group():
    c = Fake()
    stages = api.group_stages(c, GROUP_ID)
    assert [x["id"] for x in stages] == [11, 12]  # по SORT, а не по ключу
    assert stages[0]["is_new"] and stages[1]["sort"] == 300 and stages[1]["color"] == "abc"
    # Портал отдаёт то dict по строковому ID, то список — форма одна.
    assert api.group_stages(Fake(stages=list(STAGES.values())), GROUP_ID) == stages
    g = api.group_get(c, GROUP_ID)
    assert g["id"] == GROUP_ID and g["owner_id"] == OWNER_ID and g["is_project"] is True
    assert not g["closed"] and g["visible"] and not g["opened"]
    assert ("sonet_group.get", {"FILTER": {"ID": GROUP_ID}}) in c.calls
    assert ("task.stages.get", {"entityId": GROUP_ID}) in c.calls


def test_missing_group():
    class Empty(Fake):
        def call(self, method, params=None):
            return []

    with pytest.raises(B24Error, match="группа не найдена или нет доступа"):
        api.group_get(Empty(), GROUP_ID)


def test_owner_name_is_optional():
    class RestrictedOwner(Fake):
        def call(self, method, params=None):
            if method == "user.get":
                raise B24Error(method, "ACCESS_DENIED", "нет доступа к имени")
            return super().call(method, params)

    g = api.group_get(RestrictedOwner(), GROUP_ID)
    assert g["owner_id"] == OWNER_ID and "owner_name" not in g

    class NamedOwner(Fake):
        def call(self, method, params=None):
            if method == "user.get":
                return [{"NAME": "Иван", "LAST_NAME": "Иванов"}]
            return super().call(method, params)

    assert api.group_get(NamedOwner(), GROUP_ID)["owner_name"] == "Иванов Иван"


def test_stage_for_id_zero_is_new_and_missing_is_none():
    stages = api.group_stages(Fake(), GROUP_ID)
    assert api.stage_for_id(stages, "0")["id"] == 11
    assert api.stage_for_id(stages, "12")["title"] == "На ревью"
    for missing in (None, "", "мусор", "999"):
        assert api.stage_for_id(stages, missing) is None


# ── задачи группы ─────────────────────────────────────────────────────────

def test_group_filters_and_limit():
    c = Fake()
    rows = api.group_tasks(c, GROUP_ID, limit=2)
    # Завершённая (5) отброшена, лимит — по принятым строкам, лишнего не тянем.
    assert [t["status"] for t in rows] == ["6", "2"] and c.consumed == 3
    p = next(p for m, p in c.calls if m == "tasks.task.list")
    assert p["filter"] == {"GROUP_ID": GROUP_ID, "!REAL_STATUS": 5}
    assert not {"STATUS", "!STATUS", "RESPONSIBLE_ID"} & p["filter"].keys()
    assert p["order"] == {"ID": "desc"}
    assert {"PRIORITY", "STAGE_ID", "CLOSED_DATE", "CREATED_BY", "DEADLINE"} <= set(p["select"])
    assert rows[0]["stageName"] == "Новые"  # stageId 0 = NEW-колонка
    assert sum(m == "task.stages.get" for m, _ in c.calls) == 1

    c = Fake()
    api.group_tasks(c, GROUP_ID, user_id=OWNER_ID, include_done=True, limit=1)
    p = next(p for m, p in c.calls if m == "tasks.task.list")
    assert p["filter"] == {"GROUP_ID": GROUP_ID, "RESPONSIBLE_ID": OWNER_ID} and c.consumed == 1


def test_default_limit_is_50():
    c = Fake(statuses=["2"] * 200)
    assert len(api.group_tasks(c, GROUP_ID)) == 50 and c.consumed == 50


def test_tasks_list_by_group():
    c = Fake()
    result = api.tasks_list(c, group_id=GROUP_ID, limit=2)
    t = result["items"][0]
    assert result["count"] == 2 and t["stage_id"] == "0" and t["stage_name"] == "Новые"
    assert result["items"][1]["stage_name"] == "На ревью"
    assert {"priority", "closed_date", "created_by", "deadline"} <= t.keys()
    # Без group_id фильтр владельца хука остаётся, а stage_name не появляется.
    c = Fake()
    plain = api.tasks_list(c, user_id=OWNER_ID, limit=1)
    assert "stage_name" not in plain["items"][0]
    assert next(p for m, p in c.calls if m == "tasks.task.list")["filter"]["RESPONSIBLE_ID"] == OWNER_ID


def test_tasks_list_group_allows_only_responsible_role():
    c = Fake()
    with pytest.raises(B24Error) as error:
        api.tasks_list(c, group_id=GROUP_ID, role="originator")
    assert error.value.code == "INVALID_ARGUMENT"
    assert c.calls == []


def test_limit_stops_real_pagination(portal):
    """Через настоящий клиент: при лимите вторая страница не запрашивается."""
    net = portal({
        "task.stages.get": [{"result": STAGES}],
        "tasks.task.list": [{"result": {"tasks": [{"id": str(n), "status": "2"} for n in range(50)]},
                             "next": 50}],
    })
    assert len(api.group_tasks(Client(CFG), GROUP_ID, limit=2)) == 2
    assert net.posts == ["tasks.task.list"]


# ── MCP-инструменты ───────────────────────────────────────────────────────

def test_group_tools_share_api_and_no_implicit_user(monkeypatch):
    from b24_mcp import server as mcp_server

    client = Fake()
    monkeypatch.setattr(mcp_server, "_client", lambda: client)
    for name, api_name in [("group_get", "group_get"), ("task_stages", "group_stages")]:
        target = Mock(return_value={"ok": True})
        monkeypatch.setattr(mcp_server.api, api_name, target)
        assert getattr(mcp_server, name)(GROUP_ID) == {"ok": True}
        target.assert_called_once_with(client, GROUP_ID)
    target = Mock(return_value={"items": []})
    monkeypatch.setattr(mcp_server.api, "tasks_list", target)
    assert mcp_server.tasks_list(group_id=GROUP_ID, limit=2) == {"items": []}
    target.assert_called_once_with(client, user_id=None, group_id=GROUP_ID,
                                   role="responsible", include_done=False, limit=2)


@pytest.mark.parametrize("stage_ids", [[], [0, "0", None, ""], ["0", "12"]])
def test_group_tasks_only_reads_stages_after_nonzero_task(portal, stage_ids):
    net = portal({
        "tasks.task.list": [{"result": {"tasks": [
            {"id": str(i), "status": "2", "stageId": sid} for i, sid in enumerate(stage_ids)
        ]}}],
        "task.stages.get": [{"result": STAGES}],
    })
    rows = api.group_tasks(Client(CFG), GROUP_ID)
    if "12" in stage_ids:
        assert net.posts == ["tasks.task.list", "task.stages.get"]
        assert [t["stageName"] for t in rows] == ["Новые", "На ревью"]
    else:
        assert net.posts == ["tasks.task.list"]
        assert all(t["stageName"] == "" for t in rows)


def test_nonzero_stage_beyond_limit_does_not_trigger_stage_read(portal):
    net = portal({"tasks.task.list": [{"result": {"tasks": [
        {"id": "1", "status": "2", "stageId": "0"},
        {"id": "2", "status": "2", "stageId": "12"},
    ]}}]})
    assert api.group_tasks(Client(CFG), GROUP_ID, limit=1)[0]["stageName"] == ""
    assert net.posts == ["tasks.task.list"]
