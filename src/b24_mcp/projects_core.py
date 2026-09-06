"""Чаты-проекты Битрикс24 (новая фича «Проекты» = коллабы).

Проект = чат типа `collab`. Проект ГРУППИРУЕТ чаты: у него есть главный чат
(`chat<collabId>`) и дочерние чаты (задачи, календарные синки, под-чаты, Copilot),
которые достаются `im.v2.Recent.tail` с `filter[parentId]=<collabId>`.

Источник списка — `im.v2.Recent.tail` (V2 модуля `im`, scope `im` у хука есть):
recent-хвост пользователя, новейшие по `dateLastActivity` первыми, без
`filter[recentSection]` (= все разделы). Элементы — лёгкие ссылки; сами объекты
чатов/сообщений/юзеров приходят отдельными коллекциями (PopupData), сшиваем по id.

Чтение сообщений внутри чата проекта — переиспользуем `_comments.fetch_chat`
(`im.dialog.messages.get`, та же механика, что у чатов задач). Контракт снят
эмпирически 2026-06-17. Read-only.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .client import Client
from .commands import _comments as cm

COLLAB_TYPE = "collab"


def _recent_page(
    client: Client, *, limit: int = 50,
    cursor: str | None = None, parent_id: int | None = None,
) -> dict[str, Any]:
    """Одна страница im.v2.Recent.tail. cursor = dateLastActivity последнего элемента."""
    params: dict[str, Any] = {"limit": limit}
    flt: dict[str, Any] = {}
    if cursor:
        flt["lastMessageDate"] = cursor
    if parent_id:
        flt["parentId"] = int(parent_id)
    if flt:
        params["filter"] = flt
    res = client.call("im.v2.Recent.tail", params)
    return res if isinstance(res, dict) else {}


def list_projects(client: Client, *, want: int = 10, max_pages: int = 14) -> list[dict[str, Any]]:
    """Последние collab-проекты по активности (новейшие первыми, без дублей).

    Каждый элемент: chatId, dialogId, name, date, desc, lastAuthor, lastText.
    """
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    msgs: dict[Any, dict[str, Any]] = {}
    users: dict[Any, str] = {}
    cursor: str | None = None
    for _ in range(max_pages):
        if len(out) >= want:
            break
        res = _recent_page(client, limit=50, cursor=cursor)
        items = res.get("recentItems", []) or []
        if not items:
            break
        chats = {ch.get("id"): ch for ch in res.get("chats", []) if ch.get("id") is not None}
        for m in res.get("messages", []) or []:
            msgs[m.get("id")] = m
        for u in res.get("users", []) or []:
            users[u.get("id")] = u.get("name")
        for it in items:
            cid = it.get("chatId")
            ch = chats.get(cid, {})
            if ch.get("type") == COLLAB_TYPE and cid not in seen:
                seen.add(cid)
                lm = msgs.get(it.get("messageId"), {})
                aid = lm.get("authorId")
                out.append({
                    "chatId": cid,
                    "dialogId": it.get("dialogId"),
                    "name": ch.get("name") or "(без имени)",
                    "date": it.get("dateLastActivity"),
                    "desc": cm.clean_bb(ch.get("description") or ""),
                    "lastAuthor": (users.get(aid) or ("система" if str(aid) == "0" else f"user#{aid}")),
                    "lastText": cm.clean_bb(lm.get("text") or ""),
                    "unread": bool(it.get("unread")),
                })
                if len(out) >= want:
                    break
        cursor = items[-1].get("dateLastActivity")
        if not res.get("hasNextPage"):
            break
    return out


def all_projects(client: Client, *, max_pages: int = 40) -> list[dict[str, Any]]:
    """Все collab-проекты в recent (для июньского прочёса). Больше страниц, без лимита want."""
    return list_projects(client, want=10**6, max_pages=max_pages)


def child_chats(client: Client, parent_chat_id: int, *, max_pages: int = 6) -> list[dict[str, Any]]:
    """Дочерние чаты проекта: [{chatId, type, name, date}]. Главный чат проекта НЕ включается."""
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(max_pages):
        res = _recent_page(client, limit=50, cursor=cursor, parent_id=parent_chat_id)
        items = res.get("recentItems", []) or []
        if not items:
            break
        chats = {ch.get("id"): ch for ch in res.get("chats", []) if ch.get("id") is not None}
        for it in items:
            cid = it.get("chatId")
            if cid in seen:
                continue
            seen.add(cid)
            ch = chats.get(cid, {})
            out.append({
                "chatId": cid,
                "dialogId": it.get("dialogId"),
                "type": ch.get("type"),
                "name": ch.get("name") or "(без имени)",
                "date": it.get("dateLastActivity"),
            })
        cursor = items[-1].get("dateLastActivity")
        if not res.get("hasNextPage"):
            break
    return out


def read_window(
    client: Client, chat_id: int, *, since: date | None = None,
    limit: int = 50, full: bool = False, cap: int = 600,
) -> tuple[list[dict[str, Any]], dict[Any, str]]:
    """Сообщения чата (хронологически) + карта id→имя. since → только с этой даты."""
    msgs, users = cm.fetch_chat(client, int(chat_id), limit=limit, full=full, cap=cap)
    if since:
        msgs = [m for m in msgs if (cm._msg_date(m) and cm._msg_date(m) >= since)]
    return msgs, users


def fresh_live(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Живые (не системные) сообщения с непустым текстом — в хронологии."""
    return [
        m for m in messages
        if not cm._is_system(m) and cm._msg_text(m, raw=False, drop_quotes=True)
    ]
