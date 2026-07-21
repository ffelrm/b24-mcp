"""Библиотечный слой: бизнес-операции с нормализованным ответом.

Отличие от `commands/*` — те печатают markdown для человека и живут в argparse;
здесь чистые функции, возвращающие структуру для модели (MCP-фасад). Тяжёлая
логика НЕ дублируется: слои переиспользуются как есть
(`projects_core`, `task_updates`, `commands/_comments`, `commands/_dialog`).

Правило слоя: наружу отдаём только нормализованные поля. Никаких сырых
payload'ов Битрикса целиком — модель не должна зависеть от формы REST-ответа.
stdlib-only.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from . import normalizers as nz
from .client import Client

# ---------------------------------------------------------------- люди


def whoami(client: Client) -> dict[str, Any]:
    """Владелец вебхука. Смоук связи: если это отвечает — мост живой."""
    u = client.call("user.current") or {}
    return _person(u)


def _person(u: dict[str, Any]) -> dict[str, Any]:
    name = " ".join(
        filter(None, [u.get("LAST_NAME"), u.get("NAME"), u.get("SECOND_NAME")])
    ).strip()
    return {
        "id": int(u["ID"]) if str(u.get("ID", "")).isdigit() else u.get("ID"),
        "name": name or None,
        "position": u.get("WORK_POSITION") or None,
        "department_ids": u.get("UF_DEPARTMENT") or [],
        "email": u.get("EMAIL") or None,
        "active": u.get("ACTIVE") is not False,
        "last_activity": u.get("LAST_ACTIVITY_DATE") or None,
    }


def person_find(client: Client, query: str, *, limit: Any = 10) -> dict[str, Any]:
    """Резолв человека по ID / email / части имени.

    Всегда ищи человека ЭТИМ методом до обращения по ID: в крупной компании
    однофамильцы, и угаданный ID уводит разбор не туда.
    """
    lim = nz.clamp(limit, hi=50, default=10)
    q = (query or "").strip()
    if not q:
        return nz.page([], limit=lim)
    if q.isdigit():
        res = client.call("user.get", {"ID": int(q)})
        hits = res if isinstance(res, list) else []
    elif "@" in q:
        hits = list(client.call_list("user.get", {"FILTER": {"EMAIL": q}}))
    else:
        found: dict[int, dict] = {}
        for token in re.split(r"\s+", q):
            if not token:
                continue
            for field in ("LAST_NAME", "NAME"):
                for u in client.call_list("user.get", {"FILTER": {f"%{field}": token}}):
                    try:
                        found[int(u["ID"])] = u
                    except (KeyError, TypeError, ValueError):
                        continue
        hits = list(found.values())
    return nz.page([_person(u) for u in hits[:lim]], limit=lim, total=len(hits))


# ---------------------------------------------------------------- календарь


def calendar_events(
    client: Client, *, user_id: int | None = None, days: Any = 1, start: str | None = None
) -> dict[str, Any]:
    """События владельца (или указанного человека) на N дней вперёд.

    Ключевое — отличить ВСТРЕЧУ от личного блока. Отдаём и флаг, и участников:
    решение о классификации принимает потребитель, а не мы за него.
    """
    uid = int(user_id) if user_id else (client.user_id or 0)
    n = nz.clamp(days, lo=1, hi=31, default=1)
    d0 = date.fromisoformat(start) if start else date.today()
    d1 = d0 + timedelta(days=n - 1)
    raw = (
        client.call(
            "calendar.event.get",
            {
                "type": "user",
                "ownerId": uid,
                "from": d0.isoformat(),
                "to": (d1 + timedelta(days=1)).isoformat(),
            },
        )
        or []
    )
    out = []
    for e in raw if isinstance(raw, list) else []:
        meeting = e.get("MEETING") or {}
        attendees = []
        for a in e.get("ATTENDEE_LIST") or []:
            attendees.append(
                {
                    "id": a.get("id"),
                    "name": a.get("name") or None,
                    "status": a.get("status"),
                }
            )
        host_id = meeting.get("HOST_ID") or e.get("CREATED_BY")
        out.append(
            {
                "id": e.get("ID"),
                "title": nz.clean_text(e.get("NAME")),
                "from": e.get("DATE_FROM"),
                "to": e.get("DATE_TO"),
                "is_meeting": nz.is_yes(e.get("IS_MEETING")),
                "is_invitee": bool(host_id) and str(host_id) != str(uid),
                "host_id": host_id,
                "host_name": meeting.get("HOST_NAME") or None,
                "attendees_count": len(attendees),
                "attendees": attendees,
                "location": e.get("LOCATION") or None,
            }
        )
    out.sort(key=lambda x: str(x.get("from") or ""))
    return nz.page(out, limit=len(out))


# ---------------------------------------------------------------- задачи

_ROLE_FILTER = {
    "responsible": "RESPONSIBLE_ID",
    "originator": "CREATED_BY",
    "auditor": "AUDITOR",
    "accomplice": "ACCOMPLICE",
    "member": "MEMBER",
}


def tasks_list(
    client: Client,
    *,
    user_id: int | None = None,
    role: str = "responsible",
    include_done: bool = False,
    limit: Any = 50,
) -> dict[str, Any]:
    """Задачи человека в выбранной роли.

    Дефолт `responsible` = «висит лично на исполнителе» — правильный ответ на
    вопрос «что на нём». У руководителя почти все видимые задачи чужие (он
    постановщик/наблюдатель), поэтому без фильтра роли список бесполезен.
    """
    uid = int(user_id) if user_id else (client.user_id or 0)
    lim = nz.clamp(limit, default=50)
    key = _ROLE_FILTER.get((role or "").lower(), "RESPONSIBLE_ID")
    flt: dict[str, Any] = {key: uid}
    if not include_done:
        flt["<REAL_STATUS"] = 5  # 5 = завершена
    rows: list[dict[str, Any]] = []
    for t in client.call_list(
        "tasks.task.list",
        {
            "filter": flt,
            "select": [
                "ID", "TITLE", "STATUS", "DEADLINE", "RESPONSIBLE_ID",
                "CREATED_BY", "GROUP_ID", "ACTIVITY_DATE",
            ],
            "order": {"ACTIVITY_DATE": "desc"},
        },
    ):
        rows.append(
            {
                "id": int(t["id"]) if str(t.get("id", "")).isdigit() else t.get("id"),
                "title": nz.clean_text(t.get("title")),
                "status": t.get("status"),
                "deadline": t.get("deadline") or None,
                "responsible_id": t.get("responsibleId"),
                "created_by": t.get("createdBy"),
                "group_id": t.get("groupId") or None,
                "activity_date": t.get("activityDate") or None,
                "self_note": str(t.get("createdBy")) == str(t.get("responsibleId")),
            }
        )
        if len(rows) >= lim:
            break
    return nz.page(rows, limit=lim)


def task_get(client: Client, task_id: int) -> dict[str, Any]:
    """Шапка задачи: тело + статус + дедлайн + есть ли чат обсуждения."""
    from .commands._comments import task_meta

    meta = task_meta(client, int(task_id)) or {}
    return {
        "id": int(task_id),
        "title": nz.clean_text(meta.get("title")),
        "description": nz.clean_text(meta.get("description")),
        "status": meta.get("status"),
        "deadline": meta.get("deadline") or None,
        "chat_id": meta.get("chat_id"),
        "has_chat": bool(meta.get("chat_id")),
    }


def task_chat(
    client: Client,
    task_id: int,
    *,
    limit: Any = 40,
    full: bool = False,
    include_system: bool = False,
) -> dict[str, Any]:
    """Обсуждение внутри задачи — там, где живёт суть, а не в полях.

    Источник — ЧАТ задачи (не легаси-комменты: они отстают). Системные
    сообщения (author_id 0: назначения, смены сроков) по умолчанию скрыты.
    """
    from .commands._comments import fetch_chat, task_meta

    lim = nz.clamp(limit, hi=200, default=40)
    meta = task_meta(client, int(task_id)) or {}
    chat_id = meta.get("chat_id")
    head = {
        "task_id": int(task_id),
        "title": nz.clean_text(meta.get("title")),
        "description": nz.trim(nz.clean_text(meta.get("description")), 2000),
        "status": meta.get("status"),
        "deadline": meta.get("deadline") or None,
    }
    if not chat_id:
        return {**head, "has_chat": False, **nz.page([], limit=lim)}
    msgs, users = fetch_chat(client, int(chat_id), limit=lim, full=bool(full))
    return {**head, "has_chat": True, **_messages_page(msgs, users, lim, include_system)}


def tasks_updates(client: Client, *, days: Any = 2, deep: bool = False) -> dict[str, Any]:
    """Свежие чаты СВОИХ задач за окно — без обхода тысяч задач.

    Идёт по recent-хвосту вебхука, а не по списку member-задач (у активного
    руководителя их тысячи → таймаут). Дёшево: последняя реплика уже в recent.
    """
    from .task_updates import collect

    n = nz.clamp(days, lo=1, hi=30, default=2)
    since = date.today() - timedelta(days=n - 1)
    data = collect(client, since=since, deep=bool(deep)) or {}
    chats = []
    for c in data.get("chats") or []:
        chats.append(
            {
                "task_id": c.get("taskId"),
                "chat_id": c.get("chatId"),
                "title": nz.clean_text(c.get("name")),
                "date": c.get("date"),
                "is_new": bool(c.get("isNew")),
                "last_author": c.get("lastAuthor") or None,
                "last_text": nz.trim(nz.clean_text(c.get("lastText")), 400),
            }
        )
    new_tasks = [
        {
            "id": t.get("id"),
            "title": nz.clean_text(t.get("title")),
            "created": t.get("createdDate") or t.get("created") or None,
        }
        for t in (data.get("new_tasks") or [])
    ]
    return {
        "window_days": n,
        "since": since.isoformat(),
        "active_chats": nz.page(chats, limit=len(chats)),
        "new_tasks": nz.page(new_tasks, limit=len(new_tasks)),
    }


# ---------------------------------------------------------------- чаты


def _messages_page(
    msgs: list[dict[str, Any]],
    users: dict[Any, str],
    limit: int,
    include_system: bool,
) -> dict[str, Any]:
    from .commands._comments import _is_system

    out = []
    for m in msgs:
        system = _is_system(m)
        if system and not include_system:
            continue
        author_id = m.get("author_id")
        out.append(
            {
                "id": m.get("id"),
                "date": m.get("date"),
                "author_id": author_id,
                "author": users.get(author_id) or (None if system else str(author_id)),
                "system": system,
                "text": nz.trim(nz.clean_text(m.get("text")), 4000),
            }
        )
    return nz.page(out, limit=limit)


def chat_read(
    client: Client,
    *,
    chat_id: int | None = None,
    user_id: int | None = None,
    limit: Any = 50,
    full: bool = False,
    include_system: bool = False,
) -> dict[str, Any]:
    """Любой диалог, где ты участник: групповой чат или личка.

    `chat_id` — групповой/проектный чат; `user_id` — личка с человеком.
    Ссылки на скачивание файлов сюда НЕ попадают: они могут нести auth-параметр.
    """
    from .commands._dialog import fetch_dialog

    lim = nz.clamp(limit, hi=200, default=50)
    if chat_id:
        dialog_id = f"chat{int(chat_id)}"
    elif user_id:
        dialog_id = str(int(user_id))
    else:
        raise ValueError("нужен chat_id (группа) или user_id (личка)")
    msgs, users, files = fetch_dialog(client, dialog_id, limit=lim, full=bool(full))
    body = _messages_page(msgs, users, lim, include_system)
    attachments = [
        {
            "disk_id": f.get("id"),
            "name": f.get("name"),
            "size": f.get("size"),
            "author_id": f.get("authorId"),
            "date": f.get("date"),
        }
        for f in (files or {}).values()
    ]
    return {"dialog_id": dialog_id, **body, "attachments": attachments}


# ---------------------------------------------------------------- проекты


def projects_list(client: Client, *, limit: Any = 10) -> dict[str, Any]:
    """Проекты-коллабы по активности — слой, которого обычно нет в заметках."""
    from .projects_core import list_projects

    lim = nz.clamp(limit, hi=50, default=10)
    rows = []
    for p in list_projects(client, want=lim) or []:
        rows.append(
            {
                "project_id": p.get("chatId"),
                "name": nz.clean_text(p.get("name")),
                "date": p.get("date"),
                "description": nz.trim(nz.clean_text(p.get("desc")), 500),
                "last_author": p.get("lastAuthor") or None,
                "last_text": nz.trim(nz.clean_text(p.get("lastText")), 400),
            }
        )
    return nz.page(rows, limit=lim)


def project_chats(client: Client, project_id: int) -> dict[str, Any]:
    """Из чего состоит проект: дочерние чаты (задачи, синки, под-чаты).

    Главный чат проекта сюда не входит — он и есть `project_id`.
    """
    from .projects_core import child_chats

    rows = []
    for ch in child_chats(client, int(project_id)) or []:
        rows.append(
            {
                "chat_id": ch.get("chatId"),
                "type": ch.get("type"),
                "name": nz.clean_text(ch.get("name")),
                "date": ch.get("date"),
            }
        )
    return {"project_id": int(project_id), **nz.page(rows, limit=len(rows))}


def project_read(
    client: Client,
    chat_id: int,
    *,
    days: Any = None,
    limit: Any = 50,
    live_only: bool = True,
) -> dict[str, Any]:
    """Обсуждение чата проекта (главного или дочернего).

    `live_only` отбрасывает системные записи — обычно нужен именно разговор.
    """
    from .projects_core import fresh_live, read_window

    lim = nz.clamp(limit, hi=200, default=50)
    since = None
    if days:
        n = nz.clamp(days, lo=1, hi=90, default=7)
        since = date.today() - timedelta(days=n - 1)
    msgs, users = read_window(client, int(chat_id), since=since, limit=lim)
    if live_only:
        msgs = fresh_live(msgs)
    body = _messages_page(msgs, users, lim, include_system=not live_only)
    return {"chat_id": int(chat_id), "since": since.isoformat() if since else None, **body}


# ---------------------------------------------------------------- звонки


def followups_list(
    client: Client, *, days: Any = 7, user_id: int | None = None, max_items: Any = 20
) -> dict[str, Any]:
    """Завершённые звонки с ГОТОВЫМ AI-разбором за окно.

    Звонки без разбора портал не отдаёт — пустой список это норма, а не сбой.
    Фильтр по участнику работает только под админскими правами.
    """
    from . import config as _config
    from .followups_core import DEFAULT_LIST_SELECT, FollowUpsClient

    n = nz.clamp(days, lo=1, hi=90, default=7)
    cap = nz.clamp(max_items, hi=100, default=20)
    d_to = date.today()
    d_from = d_to - timedelta(days=n - 1)
    fc = FollowUpsClient(_config.load())
    rows = []
    for it in fc.list(
        d_from, d_to, select=DEFAULT_LIST_SELECT, participant_id=user_id, max_items=cap
    ):
        ov = it.get("overview") or {}
        rows.append(
            {
                "call_id": it.get("callId"),
                "started": it.get("startDate"),
                "duration_sec": it.get("durationSeconds"),
                "call_type": it.get("callType"),
                "topic": nz.trim(nz.clean_text(ov.get("topic")), 300),
                # Портал зовёт поле userId (не id) — и в списке, и в разборе.
                # На `id` получаются молчаливые None, а без id участника не
                # связать с карточкой человека.
                "participants": [
                    {"id": p.get("userId"), "name": p.get("name"),
                     "position": p.get("workPosition") or None}
                    for p in (it.get("participants") or [])
                ],
                "action_items_count": len(ov.get("actionItems") or []),
            }
        )
    return {"window_days": n, "since": d_from.isoformat(), **nz.page(rows, limit=cap)}


def followup_get(
    client: Client, call_id: int, *, transcript: bool = False
) -> dict[str, Any]:
    """Разбор одного звонка: обзор, договорённости, action items, участники.

    `transcript=True` добавляет полную расшифровку — она тяжёлая, бери её
    только когда нужна атрибуция реплик или точная цитата.

    ⚠️ `action_items` — это СЫРЬЁ портала, а не готовый список твоих задач.
    Кому принадлежит задача, решается по говорящему и адресату в расшифровке.
    """
    from . import config as _config
    from .followups_core import FollowUpsClient

    fc = FollowUpsClient(_config.load())
    item = fc.get(int(call_id)) or {}
    ov = item.get("overview") or {}
    out: dict[str, Any] = {
        "call_id": item.get("callId") or int(call_id),
        "started": item.get("startDate"),
        "duration_sec": item.get("durationSeconds"),
        "topic": nz.clean_text(ov.get("topic")),
        "meeting_type": nz.clean_text((ov.get("meetingType") or {}).get("title")),
        "agenda": nz.clean_text((ov.get("agenda") or {}).get("explanation")),
        "agreements": [
            {"text": nz.clean_text(a.get("agreement")), "quote": nz.clean_text(a.get("quote"))}
            for a in (ov.get("agreements") or [])
            if isinstance(a, dict)
        ],
        "action_items": [
            {"text": nz.clean_text(a.get("actionItem")), "quote": nz.clean_text(a.get("quote"))}
            for a in (ov.get("actionItems") or [])
            if isinstance(a, dict)
        ],
        "takeaways": nz.trim(nz.clean_text(ov.get("detailedTakeaways")), 4000),
        "outcomes": [nz.clean_text(o) for o in (item.get("outcomes") or []) if isinstance(o, str)],
        "participants": [
            {
                "id": p.get("userId"),
                "name": p.get("name"),
                "position": p.get("workPosition") or None,
                "talked_sec": p.get("talkedSeconds"),
            }
            for p in (item.get("participants") or [])
        ],
        "efficiency": (item.get("evaluation") or {}).get("efficiencyValue"),
        "summary": nz.trim(_summary_text(item.get("summary")), 6000),
    }
    if transcript:
        out["transcript"] = nz.trim(_transcript_text(item.get("transcription")), 60000)
    return out


def _summary_text(summary: Any) -> str:
    """Саммари приходит как `{segments: [{title, summary, start, end}]}`.

    Не строкой и не плоским списком — сегментами по темам встречи.
    """
    if isinstance(summary, str):
        return nz.clean_text(summary)
    segments = (summary or {}).get("segments") if isinstance(summary, dict) else None
    parts = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        title = nz.clean_text(seg.get("title"))
        body = nz.clean_text(seg.get("summary"))
        parts.append(f"**{title}**\n{body}".strip() if title else body)
    return "\n\n".join(p for p in parts if p)


def _transcript_text(transcription: Any) -> str:
    """Расшифровка — `{segments: [{userName, text, start, end}]}`.

    Поле называется `transcription` (не `transcript`) — на этом легко
    промахнуться и получить молча пустой результат.
    """
    if isinstance(transcription, str):
        return nz.clean_text(transcription)
    segments = (transcription or {}).get("segments") if isinstance(transcription, dict) else None
    lines = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        who = seg.get("userName") or seg.get("userId") or ""
        txt = nz.clean_text(seg.get("text"))
        if txt:
            lines.append(f"{who}: {txt}".strip(": "))
    return "\n".join(lines)
