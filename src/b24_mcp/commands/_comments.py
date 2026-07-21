"""Чаты задач Битрикс24: выгрузка обсуждения задачи + скан по человеку.

КОНТРАКТ (снят эмпирически на портале Льва, июнь 2026):

- «Сок» задачи живёт в ЧАТЕ задачи, а не в легаси-комментах. У задачи есть поле
  `chatId` (tasks.task.get / tasks.task.list select CHAT_ID). Сам чат — это
  IM-диалог `chat<chatId>`, читается через `im.dialog.messages.get`.
- Легаси `task.commentitem.getlist` показывает ТОЛЬКО старые форумные комменты и
  отстаёт: на «Модерации с AI» он обрывался на окт-2025, а в чате — живой обмен
  до июня-2026. Поэтому источник по умолчанию — ЧАТ; commentitem оставлен
  фолбэком для редких задач без chatId.
- `im.dialog.messages.get` отдаёт окно сообщений ОТ НОВЫХ К СТАРЫМ (LIMIT, по
  умолчанию 20, потолок ~200). Глубже — пагинация через LAST_ID. Ответ:
  {messages:[{id, author_id, date, text, params, ...}], users:[{id,name}], files}.
- СИСТЕМНЫЕ сообщения (назначения, наблюдатели, смены сроков/статуса) имеют
  `author_id == 0`. Это чистый фильтр — живые реплики = author_id != 0.
- `ACTIVITY_DATE` задачи коррелирует именно с чатом (новое сообщение бампает
  активность), поэтому скан по ACTIVITY_DATE desc реально находит свежие треды.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta
from typing import Any

from ..client import B24Error, Client

# --- очистка BB-разметки сообщений ---
_BB_QUOTE = re.compile(r"\[QUOTE\].*?\[/QUOTE\]", re.IGNORECASE | re.DOTALL)
_BB_USER = re.compile(r"\[USER=\d+\]([^\[]*)\[/USER\]", re.IGNORECASE)
_BB_URL = re.compile(r"\[URL=([^\]]+)\]([^\[]*)\[/URL\]", re.IGNORECASE)
_BB_URL_BARE = re.compile(r"\[URL\]([^\[]*)\[/URL\]", re.IGNORECASE)
_BB_DISK = re.compile(r"\[DISK\s+FILE\s+ID=[^\]]+\](?:[^\[]*\[/DISK\])?", re.IGNORECASE)
_BB_TS = re.compile(r"\[TIMESTAMP=[^\]]+\]", re.IGNORECASE)
_BB_ANY = re.compile(r"\[/?[A-Za-z][^\]]*\]")
# артефакт цитаты-ответа в чате: "----- Имя [дата] #chat123/456 ..."
_QUOTE_ART = re.compile(r"-{6,}.*?#chat\d+/\d+", re.DOTALL)
# описание задачи может прийти как HTML (а не BBCode) — чистим теги
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_BR = re.compile(r"(?i)<br\s*/?>|</p\s*>|</div\s*>")

# легаси-системные строки (для фолбэка commentitem, где нет author_id)
_SYS_LINE = re.compile(
    r"(?ix)(?:"
    r"вы\s+(?:назначены|добавлены)\s+(?:исполнителем|соисполнителем|наблюдателем|постановщиком)"
    r"|вы\s+больше\s+не\s+(?:наблюдатель|исполнитель|соисполнитель|постановщик)"
    r"|необходимо\s+указать\s+крайний\s+срок"
    r"|(?:крайний\s+)?срок(?:\s+выполнения)?\s+задачи\s+(?:изменён|изменен|перенесён|перенесен|установлен|сдвинут)"
    r"|крайний\s+срок\s+(?:изменён|изменен|перенесён|перенесен|установлен|сдвинут)"
    r"|изменил[аи]?\s+крайний\s+срок"
    r"|задача\s+почти\s+просрочена|задача\s+просрочена"
    r"|завершите\s+задачу\s+или\s+передвиньте\s+срок|эффективность\s+будет\s+снижена"
    r"|задача\s+(?:создана|завершена|возобновлена|приостановлена|отложена|принята|перенесена)"
    r"|добавил[аи]?\s+наблюдател|изменил[аи]?\s+(?:название|описание|ответственн|важность|приоритет)"
    r"|(?:добавил|удалил|приложил)[аи]?\s+файл"
    r")"
)


def clean_bb(text: str, *, drop_quotes: bool = False) -> str:
    s = text or ""
    s = _QUOTE_ART.sub("「цитата」", s)
    s = _BB_QUOTE.sub("" if drop_quotes else "「цитата」 ", s)
    s = re.sub(r"-{6,}", " — ", s)  # артефакт-разделитель цитаты
    s = _BB_USER.sub(r"@\1", s)
    s = _BB_URL.sub(r"\2 (\1)", s)
    s = _BB_URL_BARE.sub(r"\1", s)
    s = _BB_DISK.sub("[файл]", s)
    s = _BB_TS.sub("", s)
    s = _BB_ANY.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _clean_description(raw: str) -> str:
    """Тело задачи (поле DESCRIPTION) → читаемый текст.

    B24 хранит описание либо BBCode, либо HTML (зависит от DESCRIPTION_IN_BBCODE).
    Сначала сводим переносы-теги к \\n, выкидываем остальные HTML-теги, затем
    общий clean_bb (BB-разметка + html.unescape + нормализация пробелов).
    """
    s = raw or ""
    s = _HTML_BR.sub("\n", s)
    s = _HTML_TAG.sub("", s)
    s = re.sub(r"\s*\[\*\]\s*", "\n- ", s)  # BBCode-пункты списка → читаемые буллеты
    return clean_bb(s)


def _fmt_dt(raw: str) -> str:
    try:
        return datetime.fromisoformat((raw or "").replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return (raw or "")[:16]


def _msg_date(m: dict[str, Any]) -> date | None:
    try:
        return datetime.fromisoformat((m.get("date") or "").replace("Z", "+00:00")).date()
    except Exception:
        return None


def _is_system(m: dict[str, Any]) -> bool:
    return str(m.get("author_id")) == "0"


def default_since(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


# --- доступ к чату задачи ---

def task_meta(client: Client, task_id: int) -> dict[str, Any]:
    """Шапка задачи для одиночного просмотра.

    Возвращает {chat_id, title, description, status, deadline}.
    chat_id=None → у задачи нет чата (старая), нужен фолбэк на легаси-комменты.

    DESCRIPTION/STATUS/DEADLINE тянутся ЯВНО — без них тело задачи терялось
    (баг до 2026-06-22: select был только ID/TITLE/CHAT_ID, и описание не печаталось
    нигде, даже у задачи без единой реплики в чате).
    """
    empty: dict[str, Any] = {"chat_id": None, "title": "", "description": "", "status": "", "deadline": ""}
    try:
        r = client.call("tasks.task.get", {
            "taskId": int(task_id),
            "select": ["ID", "TITLE", "CHAT_ID", "DESCRIPTION", "STATUS", "DEADLINE"],
        })
    except B24Error:
        return empty
    t = (r.get("task") if isinstance(r, dict) else None) or r or {}
    cid = t.get("chatId") or t.get("CHAT_ID")
    try:
        cid = int(cid) if cid else None
    except (TypeError, ValueError):
        cid = None
    return {
        "chat_id": cid,
        "title": t.get("title") or t.get("TITLE") or "",
        "description": _clean_description(t.get("description") or t.get("DESCRIPTION") or ""),
        "status": str(t.get("status") or t.get("STATUS") or ""),
        "deadline": t.get("deadline") or t.get("DEADLINE") or "",
    }


def fetch_chat(
    client: Client, chat_id: int, *, limit: int = 40, full: bool = False, cap: int = 600
) -> tuple[list[dict[str, Any]], dict[Any, str]]:
    """Сообщения чата в ХРОНОЛОГИЧЕСКОМ порядке (старые сверху) + карта id→имя.

    Без full — одно окно последних `limit` (потолок API ~200). С full — пагинация
    назад через LAST_ID до исчерпания или `cap` сообщений.
    """
    did = f"chat{chat_id}"
    users: dict[Any, str] = {}
    collected: list[dict[str, Any]] = []
    seen: set[Any] = set()
    window = min(max(1, limit), 200)
    last_id: int | None = None
    while True:
        params: dict[str, Any] = {"DIALOG_ID": did, "LIMIT": window}
        if last_id is not None:
            params["LAST_ID"] = last_id
        r = client.call("im.dialog.messages.get", params)
        if not isinstance(r, dict):
            break
        batch = r.get("messages", []) or []
        for u in (r.get("users") or []):
            users[u.get("id")] = u.get("name")
        new = [m for m in batch if m.get("id") not in seen]
        for m in new:
            seen.add(m.get("id"))
        collected.extend(new)
        if not full or not new or len(collected) >= cap:
            break
        last_id = min(int(m["id"]) for m in batch if m.get("id") is not None)
    # im отдаёт от новых к старым → разворачиваем в хронологию
    collected.sort(key=lambda m: int(m.get("id") or 0))
    return collected, users


def _author(m: dict[str, Any], users: dict[Any, str]) -> str:
    aid = m.get("author_id")
    return users.get(aid) or users.get(str(aid)) or f"user#{aid}"


def _msg_text(m: dict[str, Any], *, raw: bool, drop_quotes: bool = False) -> str:
    if raw:
        return m.get("text") or ""
    txt = clean_bb(m.get("text") or "", drop_quotes=drop_quotes)
    if not txt and (m.get("params") or {}).get("FILE_ID"):
        return "[вложение]"
    return txt


def _task_intro_block(description: str, status: str | None, deadline: str | None) -> list[str]:
    """Блок «Описание задачи» + краткая мета для одиночного вывода задачи.

    Пусто (нет описания/статуса/дедлайна) → [] (ничего не печатаем, без шума).
    Передаётся только из tasks-команды; projects-вызов render_chat его не задаёт.
    """
    if not (description or status or deadline):
        return []
    out: list[str] = []
    bits: list[str] = []
    if status:
        bits.append(f"статус: {status}")
    if deadline:
        bits.append(f"дедлайн: {deadline}")
    if bits:
        out.append("_" + " · ".join(bits) + "_")
    if description:
        if bits:
            out.append("")
        out.append("## 📋 Описание задачи")
        out.append(description)
    out.append("")
    out.append("---")
    return out


def render_chat(
    messages: list[dict[str, Any]],
    users: dict[Any, str],
    *,
    task_id: int,
    title: str,
    host: str | None,
    include_system: bool = False,
    raw: bool = False,
    description: str | None = None,
    status: str | None = None,
    deadline: str | None = None,
) -> str:
    lines: list[str] = []
    head = f"# Чат задачи {task_id}" + (f": {title}" if title else "")
    lines.append(head)

    shown: list[tuple[dict[str, Any], str]] = []
    sys_hidden = 0
    for m in messages:
        if _is_system(m) and not include_system:
            sys_hidden += 1
            continue
        text = _msg_text(m, raw=raw)
        if not text:
            continue
        shown.append((m, text))

    meta = f"{len(shown)} реплик"
    if messages:
        meta += f" · {_fmt_dt(messages[0].get('date',''))[:10]} → {_fmt_dt(messages[-1].get('date',''))[:10]}"
    if sys_hidden and not include_system:
        meta += f" · скрыто системных: {sys_hidden}"
    lines.append(meta)
    if host:
        lines.append(f"https://{host}/company/personal/user/0/tasks/task/view/{task_id}/")
    intro = _task_intro_block(description or "", status, deadline)
    if intro:
        lines.append("")
        lines.extend(intro)
    lines.append("")

    if not shown:
        lines.append("_живых реплик в окне нет (только системные — попробуй --all или увеличь --limit)_")
        return "\n".join(lines)

    for m, text in shown:
        tag = " ⚙️" if _is_system(m) else ""
        lines.append(f"**[{_fmt_dt(m.get('date',''))}] {_author(m, users)}{tag}**")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip()


# --- фолбэк: легаси-комменты для задач без chatId ---

def fetch_legacy_comments(client: Client, task_id: int) -> list[dict[str, Any]]:
    res = client.call("task.commentitem.getlist", {"TASKID": int(task_id)})
    return res if isinstance(res, list) else []


def render_legacy(
    comments: list[dict[str, Any]], *, task_id: int, title: str, host: str | None,
    include_system: bool, raw: bool,
    description: str | None = None, status: str | None = None, deadline: str | None = None,
) -> str:
    lines = [f"# Чат задачи {task_id}" + (f": {title}" if title else "") + "  _(легаси-комменты)_"]
    shown, sys_hidden = [], 0
    for c in comments:
        msg = c.get("POST_MESSAGE") or ""
        if raw:
            shown.append((c, msg)); continue
        cleaned = clean_bb(msg)
        keep = [ln for ln in cleaned.split("\n") if ln.strip() and not _SYS_LINE.search(ln)]
        text = "\n".join(keep).strip()
        if not text:
            if not include_system:
                sys_hidden += 1; continue
            text = cleaned or "_(системное)_"
        shown.append((c, text))
    meta = f"{len(shown)} реплик"
    if sys_hidden and not include_system:
        meta += f" · скрыто системных: {sys_hidden}"
    lines.append(meta)
    if host:
        lines.append(f"https://{host}/company/personal/user/0/tasks/task/view/{task_id}/")
    intro = _task_intro_block(description or "", status, deadline)
    if intro:
        lines.append("")
        lines.extend(intro)
    lines.append("")
    for c, text in shown:
        lines.append(f"**[{_fmt_dt(c.get('POST_DATE',''))}] {c.get('AUTHOR_NAME') or ('user#'+str(c.get('AUTHOR_ID')))}**")
        lines.append(text); lines.append("")
    return "\n".join([l for l in lines if l is not None]).rstrip()


# --- скан по человеку: свежие живые реплики по активным задачам ---

_ROLE_FIELD = {
    "responsible": "RESPONSIBLE_ID",
    "member": "MEMBER",
    "originator": "CREATED_BY",
    "auditor": "AUDITORS",
}


def recent_active_tasks(
    client: Client, user_ids: list[int], since: str, task_limit: int, *, role: str = "responsible"
) -> list[dict[str, Any]]:
    flt: dict[str, Any] = {">=ACTIVITY_DATE": since}
    field = _ROLE_FIELD.get(role, "RESPONSIBLE_ID")
    flt[field] = user_ids if len(user_ids) > 1 else user_ids[0]
    page = client.call("tasks.task.list", {
        "filter": flt,
        "select": ["ID", "TITLE", "CHAT_ID", "ACTIVITY_DATE", "RESPONSIBLE_ID"],
        "order": {"ACTIVITY_DATE": "desc"}, "start": 0,
    })
    tasks = page.get("tasks", []) if isinstance(page, dict) else []
    return tasks[: max(1, task_limit)]


def scan_chats(
    client: Client, user_ids: list[int], *, since: str, task_limit: int,
    title_by_target: str, host: str | None, role: str = "responsible", msg_window: int = 50,
) -> str:
    since_date = datetime.fromisoformat(since).date()
    tasks = recent_active_tasks(client, user_ids, since, task_limit, role=role)

    blocks: list[str] = []
    scanned = 0
    for t in tasks:
        tid = t.get("id") or t.get("ID")
        cid = t.get("chatId") or t.get("CHAT_ID")
        if not tid or not cid:
            continue
        scanned += 1
        try:
            msgs, users = fetch_chat(client, int(cid), limit=msg_window)
        except B24Error:
            continue
        fresh = [
            m for m in msgs
            if not _is_system(m) and (_msg_date(m) and _msg_date(m) >= since_date) and _msg_text(m, raw=False, drop_quotes=True)
        ]
        if not fresh:
            continue
        ttl = (t.get("title") or t.get("TITLE") or "")[:70]
        blk = [f"## задача {tid}: {ttl}  ({len(fresh)} свежих)"]
        if host:
            blk.append(f"https://{host}/company/personal/user/0/tasks/task/view/{tid}/")
        for m in fresh:
            txt = _msg_text(m, raw=False, drop_quotes=True).replace("\n", " ")
            txt = re.sub(r"\s{2,}", " ", txt)
            if len(txt) > 280:
                txt = txt[:280].rstrip() + "…"
            blk.append(f"- **[{_fmt_dt(m.get('date',''))}] {_author(m, users)}:** {txt}")
        blocks.append("\n".join(blk))

    header = (f"# Чаты задач: {title_by_target}\n"
              f"с {since} · проверено активных задач: {scanned} · с живым обсуждением: {len(blocks)}\n")
    if not blocks:
        return header + "\n_тихо — свежих реплик в окне нет_"
    return header + "\n" + "\n\n".join(blocks)
