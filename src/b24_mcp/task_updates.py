"""Апдейты чатов задач через im.v2.Recent.load (без обхода всех задач).

Зачем: обход `tasks.task.list` с ролью MEMBER по владельцу хука падает по таймауту —
он звал `tasks.task.list` с MEMBER (у активного руководителя member-задач тысячи) и читал чат каждой.
Здесь источник обновлений — recent-хвост хука (`im.v2.Recent.load`): берём только
чаты `type=='tasksTask'` с активностью в окне и читаем их живые реплики. Активных
в окне — десятки, а не тысячи → не падаем. Основные апдейты и так в чатах задач.

Новые задачи (созданные в окне) добираем отдельным дешёвым `tasks.task.list` с
фильтром `>=CREATED_DATE` (узкая выборка), чтобы не терять появившиеся задачи —
это «забирать сами задачи новые, если появляются».

`im.v2.Recent.load` — recent текущего пользователя (= владелец хука), новейшие
по `dateLastActivity` первыми. Чат задачи несёт `entityId` = ID задачи. Read-only.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .client import Client
from .commands import _comments as cm

RECENT_METHOD = "im.v2.Recent.load"
TASK_CHAT_TYPE = "tasksTask"


def _recent_page(client: Client, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    """Одна страница im.v2.Recent.load. cursor = dateLastActivity последнего элемента."""
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["filter"] = {"lastMessageDate": cursor}
    res = client.call(RECENT_METHOD, params)
    return res if isinstance(res, dict) else {}


def recent_task_chats(
    client: Client, *, since: date, max_pages: int = 12, limit: int = 50, cap: int = 80,
) -> list[dict[str, Any]]:
    """Чаты задач с активностью >= since (новейшие первыми, без дублей).

    Каждый: {chatId, dialogId, taskId, name, date, isNew, lastAuthor, lastText}.
    recent НЕ строго отсортирован по дате (закреплённые чаты вклиниваются), поэтому
    нельзя обрывать пагинацию на первом «старом» элементе — пропускаем вне-оконные
    поштучно (continue) и останавливаемся, только если ВСЯ страница старше окна
    (закреплённые не заполнят целую страницу) либо кончились страницы / cap.
    """
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    msgs_by_id: dict[Any, dict[str, Any]] = {}
    users: dict[Any, str] = {}
    cursor: str | None = None
    since_iso = since.isoformat()
    for _ in range(max_pages):
        res = _recent_page(client, limit=limit, cursor=cursor)
        items = res.get("recentItems", []) or []
        if not items:
            break
        chats = {ch.get("id"): ch for ch in res.get("chats", []) if ch.get("id") is not None}
        for m in res.get("messages", []) or []:
            msgs_by_id[m.get("id")] = m
        for u in res.get("users", []) or []:
            users[u.get("id")] = u.get("name")
        page_in_window = False
        for it in items:
            d = it.get("dateLastActivity") or ""
            if d[:10] and d[:10] >= since_iso:
                page_in_window = True
            cid = it.get("chatId")
            ch = chats.get(cid, {})
            if ch.get("type") != TASK_CHAT_TYPE or cid in seen:
                continue
            if d[:10] and d[:10] < since_iso:
                continue  # этот чат вне окна (но страница может содержать свежие)
            seen.add(cid)
            lm = msgs_by_id.get(it.get("messageId"), {})
            aid = lm.get("authorId")
            out.append({
                "chatId": cid,
                "dialogId": it.get("dialogId"),
                "taskId": ch.get("entityId"),
                "name": ch.get("name") or "(без имени)",
                "date": d,
                "isNew": bool(ch.get("isNew")),
                "lastAuthor": (users.get(aid) or ("система" if str(aid) == "0" else f"user#{aid}")),
                "lastText": cm.clean_bb(lm.get("text") or ""),
            })
        if len(out) >= cap:
            break
        cursor = items[-1].get("dateLastActivity")
        if not page_in_window or not res.get("hasNextPage"):
            break
    return out


def new_tasks_in_window(client: Client, uid: int, since: date, *, cap: int = 60) -> list[dict[str, Any]]:
    """Задачи, где uid — участник (MEMBER), созданные >= since. Узкая выборка (не падает)."""
    flt = {"MEMBER": uid, ">=CREATED_DATE": since.isoformat()}
    out: list[dict[str, Any]] = []
    for t in client.call_list("tasks.task.list", {
        "filter": flt,
        "select": ["ID", "TITLE", "STATUS", "RESPONSIBLE_ID", "CREATED_BY", "CREATED_DATE", "DEADLINE"],
        "order": {"CREATED_DATE": "desc"},
    }):
        out.append(t)
        if len(out) >= cap:
            break
    return out


def collect(
    client: Client, *, since: date, max_pages: int = 12, msg_limit: int = 40,
    read_cap: int = 50, want_new: bool = True, deep: bool = False,
) -> dict[str, Any]:
    """Сбор апдейтов: активные чаты задач в окне + новые задачи окна.

    Дёшево (deep=False, дефолт): по каждому активному чату задачи берём его ПОСЛЕДНЮЮ
    реплику — она уже приходит в recent (без отдельного запроса). Быстро (~секунды),
    не упирается в таймаут даже при сотнях member-задач.

    Глубоко (deep=True): дочитываем полную пачку живых реплик в окне по каждому чату
    (read_cap ограничивает число чатов), для детального разбора — медленнее.
    """
    chats = recent_task_chats(client, since=since, max_pages=max_pages)
    since_iso = since.isoformat()
    all_users: dict[Any, str] = {}
    blocks: list[dict[str, Any]] = []
    if deep:
        for c in chats[:read_cap]:
            msgs, users = cm.fetch_chat(client, int(c["chatId"]), limit=msg_limit)
            all_users.update(users)
            fresh = [
                m for m in msgs
                if not cm._is_system(m) and cm._msg_date(m)
                and (m.get("date", "")[:10] >= since_iso)
                and cm._msg_text(m, raw=False, drop_quotes=True)
            ]
            blocks.append({**c, "fresh": fresh})
        with_talk = sum(1 for b in blocks if b["fresh"])
    else:
        blocks = [{**c, "fresh": None} for c in chats]
        with_talk = sum(1 for c in chats if c.get("lastText"))
    new_tasks = new_tasks_in_window(client, client.user_id, since) if (want_new and client.user_id) else []
    return {
        "since": since_iso,
        "deep": deep,
        "chats": blocks,
        "new_tasks": new_tasks,
        "users": all_users,
        "stats": {"active": len(chats), "with_talk": with_talk, "new": len(new_tasks)},
    }


def render(data: dict[str, Any], *, uid: int | None = None, host: str | None = None) -> str:
    """Человекочитаемый дайджест апдейтов."""
    st = data["stats"]
    deep = data.get("deep")
    talk_label = "с живым обсуждением" if deep else "с непустой репликой"
    lines = [
        f"# Апдейты чатов задач (im.v2.Recent.load), с {data['since']}",
        f"активных чатов задач в окне: {st['active']} · {talk_label}: {st['with_talk']} · новых задач: {st['new']}",
        "",
    ]
    nt = data.get("new_tasks") or []
    if nt:
        lines.append("## 🆕 Новые задачи в окне")
        for t in nt:
            tid = t.get("id") or t.get("ID")
            title = (t.get("title") or t.get("TITLE") or "")[:90]
            rid = t.get("responsibleId") or t.get("RESPONSIBLE_ID")
            cby = t.get("createdBy") or t.get("CREATED_BY")
            cd = (t.get("createdDate") or t.get("CREATED_DATE") or "")[:10]
            lines.append(f"- **#{tid}** {title} — исполнитель {rid}, поставил {cby}, создана {cd}")
        lines.append("")

    if not data["chats"]:
        lines.append("_активных чатов задач за окно нет_")
        return "\n".join(lines)

    lines.append("## Чаты задач с активностью (новейшие первыми)")
    for c in data["chats"]:
        tid = c.get("taskId")
        head = f"### {c['name']}  (задача #{tid}, chat {c['chatId']})"
        if c.get("isNew"):
            head += "  🆕"
        lines.append("")
        lines.append(head)
        if host and tid and uid:
            lines.append(f"<https://{host}/company/personal/user/{uid}/tasks/task/view/{tid}/>")
        if deep and c.get("fresh") is not None:
            if c["fresh"]:
                for m in c["fresh"]:
                    who = cm._author(m, data["users"])
                    when = (m.get("date") or "")[:16].replace("T", " ")
                    txt = cm._msg_text(m, raw=False, drop_quotes=True)
                    lines.append(f"- **[{when}] {who}:** {txt}")
            else:
                lines.append("- _(в окне только системная активность)_")
        else:
            when = (c.get("date") or "")[:16].replace("T", " ")
            last = c.get("lastText") or "_(системная активность)_"
            lines.append(f"- **[{when}] {c.get('lastAuthor')}:** {last}")
    return "\n".join(lines)
