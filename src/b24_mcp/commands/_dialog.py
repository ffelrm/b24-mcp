"""Чтение произвольного IM-диалога Битрикс24 — личного (1-1) и группового.

В отличие от `_comments.py` (чат ЗАДАЧИ, резолвится task→chatId→`chat<chatId>`),
здесь читается ЛЮБОЙ диалог, где владелец хука участник, по сырому DIALOG_ID:
  - группа:  `chat<chatId>`   (b24 chat <chatId>)
  - личка:   `<userId>`       (b24 chat --user <id|имя>)

`im.dialog.messages.get` отдаёт {messages, users, files}. Собираем все три;
`files` — карта fileId→метаданные вложения (имя/размер/автор/дата), накапливаем
по страницам пагинации (одно окно отдаёт файлы только своих сообщений).
Контракт окна/пагинации — тот же, что в `_comments.fetch_chat`. Read-only.
"""
from __future__ import annotations

from typing import Any

from ..client import Client


def fetch_dialog(
    client: Client, dialog_id: str, *, limit: int = 50, full: bool = False, cap: int = 600
) -> tuple[list[dict[str, Any]], dict[Any, str], dict[Any, dict[str, Any]]]:
    """Сообщения (хронологически, старые сверху) + карта id→имя + карта fileId→метаданные.

    `dialog_id` — сырой DIALOG_ID: `chat<N>` для группы, `<N>` для лички.
    Без `full` — одно окно последних `limit` (потолок API ~200). С `full` —
    пагинация назад через LAST_ID до исчерпания или `cap` сообщений.
    """
    users: dict[Any, str] = {}
    files: dict[Any, dict[str, Any]] = {}
    collected: list[dict[str, Any]] = []
    seen: set[Any] = set()
    window = min(max(1, limit), 200)
    last_id: int | None = None
    while True:
        params: dict[str, Any] = {"DIALOG_ID": dialog_id, "LIMIT": window}
        if last_id is not None:
            params["LAST_ID"] = last_id
        r = client.call("im.dialog.messages.get", params)
        if not isinstance(r, dict):
            break
        batch = r.get("messages", []) or []
        for u in (r.get("users") or []):
            users[u.get("id")] = u.get("name")
        fl = r.get("files")
        if isinstance(fl, dict):
            files.update(fl)
        elif isinstance(fl, list):
            for f in fl:
                if isinstance(f, dict) and f.get("id") is not None:
                    files[f["id"]] = f
        new = [m for m in batch if m.get("id") not in seen]
        for m in new:
            seen.add(m.get("id"))
        collected.extend(new)
        if not full or not new or len(collected) >= cap:
            break
        ids = [int(m["id"]) for m in batch if m.get("id") is not None]
        if not ids:
            break
        last_id = min(ids)
    # im отдаёт от новых к старым → разворачиваем в хронологию
    collected.sort(key=lambda m: int(m.get("id") or 0))
    return collected, users, files


def file_names_for(msg: dict[str, Any], files: dict[Any, dict[str, Any]]) -> list[str]:
    """Имена вложений, привязанных к сообщению (через params.FILE_ID)."""
    fids = (msg.get("params") or {}).get("FILE_ID") or msg.get("files") or []
    if not isinstance(fids, list):
        fids = [fids]
    out: list[str] = []
    for fid in fids:
        meta = files.get(fid) or files.get(str(fid)) or {}
        out.append(meta.get("name") or f"file#{fid}")
    return out
