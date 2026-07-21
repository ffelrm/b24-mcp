"""MCP-фасад моста: бизнес-инструменты вместо сырых REST-вызовов.

Зачем поверх CLI (решение 2026-07-20):
- ЭРГОНОМИКА. Агент зовёт `tasks_list(role="responsible")` вместо
  90-символьной bash-строки с экранированием пробела в пути. Меньше токенов,
  уходит класс ошибок «сломал кавычки».
- ТОКЕН-ГИГИЕНА КАК БАРЬЕР, а не как правило. Вебхук читается внутри этого
  процесса и не покидает его: у модели нет инструмента прочитать .env и нет
  raw-REST, чтобы дёрнуть произвольный метод. Раньше это держалось на
  дисциплине («не делай cat .env»).
- ALLOWLIST. Наружу выставлено ровно то, что ниже. Клиент создаётся с
  allow_write=False, поэтому даже при ошибке в коде write-метод не уйдёт.
- ГОЧИ ЗАШИТЫ ОДИН РАЗ. Пагинация, batch, нормализация ответа, кламп лимитов
  живут в ядре, а не в памяти агента.

Что СОЗНАТЕЛЬНО осталось в CLI (и почему): тяжёлые резюмируемые прогоны —
полный прочёс IM, отчёты за квартал, backfill за период. MCP-вызов живёт внутри
одного tool-call: там лимит времени и негде держать чекпоинт. CLI с
чекпоинтами переживает обрыв, MCP — нет.

Запуск: .venv-mcp/bin/python mcp_server.py
Зависимости (mcp SDK, pydantic) живут в .venv-mcp; ядро b24_mcp остаётся
stdlib-only и продолжает работать без них.
"""
from __future__ import annotations

import functools
import inspect
import logging
import sys
import time
from pathlib import Path
from typing import Annotated, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402
from pydantic import Field  # noqa: E402

from b24_mcp import api, config  # noqa: E402
from b24_mcp.client import B24Error, Client  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,  # stdout занят протоколом MCP
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("b24-mcp")

mcp = FastMCP("b24-mcp")


def _client() -> Client:
    """Read-only клиент.

    config.load() при отсутствии/кривых правах .env делает sys.exit(2) — для CLI
    это правильно, но в долго живущем MCP-процессе убило бы сервер. Поэтому ловим
    SystemExit и превращаем в понятную ошибку инструмента.
    """
    try:
        cfg = config.load()
    except SystemExit:
        raise RuntimeError(
            "мост не настроен: не найден .env с B24_WEBHOOK (или у него права "
            "не 600). Проверь: ls -la .env — сам файл не показывай."
        ) from None
    return Client(cfg, allow_write=False)


def b24_tool(fn):
    """Регистрация инструмента + лог вызова с таймингом.

    В лог идут имя, аргументы, длительность и код ошибки Битрикса. Вебхук и
    токен не логируются никогда — их просто нет в этих значениях.
    """

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        started = time.monotonic()
        logger.info("tool.start %s kwargs=%s", fn.__name__, _short(kwargs))
        try:
            result = fn(*args, **kwargs)
        except B24Error as e:  # структурная ошибка портала — отдаём код как есть
            logger.warning(
                "tool.b24_error %s code=%s elapsed_ms=%d",
                fn.__name__, e.code, int((time.monotonic() - started) * 1000),
            )
            raise RuntimeError(f"Битрикс24 ответил ошибкой {e.code}: {e}") from None
        except Exception as e:
            logger.exception(
                "tool.error %s elapsed_ms=%d", fn.__name__,
                int((time.monotonic() - started) * 1000),
            )
            raise RuntimeError(f"{fn.__name__}: {e}") from None
        logger.info(
            "tool.done %s elapsed_ms=%d", fn.__name__,
            int((time.monotonic() - started) * 1000),
        )
        return result

    wrapped.__signature__ = inspect.signature(fn)
    return mcp.tool()(wrapped)


# ------------------------------------------------------------------ люди


@b24_tool
def whoami() -> dict[str, Any]:
    """Кто владелец подключённого вебхука. Быстрая проверка, что мост живой."""
    return api.whoami(_client())


@b24_tool
def person_find(
    query: Annotated[str, Field(description="ID, email или часть имени/фамилии")],
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Найти человека на портале: должность, отдел, контакты, активен ли.

    Зови ЭТО до обращения по ID — в крупной компании есть однофамильцы.
    """
    return api.person_find(_client(), query, limit=limit)


# ------------------------------------------------------------------ календарь


@b24_tool
def calendar_events(
    days: Annotated[int, Field(ge=1, le=31, description="Сколько дней от start")] = 1,
    user_id: Annotated[int | None, Field(ge=1)] = None,
    start: Annotated[str | None, Field(description="YYYY-MM-DD, по умолчанию сегодня")] = None,
) -> dict[str, Any]:
    """События календаря: время, участники, кто host.

    `is_meeting` + `attendees` дают отличить структурную встречу от личного
    блока. Одиночное событие с проектным названием — спроси владельца, с кем
    это, не классифицируй молча.
    """
    return api.calendar_events(_client(), user_id=user_id, days=days, start=start)


# ------------------------------------------------------------------ задачи


@b24_tool
def tasks_list(
    user_id: Annotated[int | None, Field(ge=1)] = None,
    role: Annotated[
        str, Field(description="responsible | originator | auditor | accomplice | member")
    ] = "responsible",
    include_done: bool = False,
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    """Задачи человека в выбранной роли.

    Дефолт `responsible` = «висит лично на нём». У руководителя почти все
    видимые задачи чужие (он постановщик/наблюдатель) — без роли список шумит.
    Поле `self_note` помечает «заметки сам себе» (постановщик = исполнитель).
    """
    return api.tasks_list(
        _client(), user_id=user_id, role=role, include_done=include_done, limit=limit
    )


@b24_tool
def task_get(task_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
    """Шапка задачи: тело, статус, дедлайн, есть ли чат обсуждения."""
    return api.task_get(_client(), task_id)


@b24_tool
def task_chat(
    task_id: Annotated[int, Field(ge=1)],
    limit: Annotated[int, Field(ge=1, le=200)] = 40,
    full: Annotated[bool, Field(description="Вся история, а не последнее окно")] = False,
    include_system: Annotated[bool, Field(description="Показать системные записи")] = False,
) -> dict[str, Any]:
    """Обсуждение внутри задачи — там суть, а не в полях.

    Системные записи (назначения, смены сроков) по умолчанию скрыты.
    """
    return api.task_chat(
        _client(), task_id, limit=limit, full=full, include_system=include_system
    )


@b24_tool
def tasks_updates(
    days: Annotated[int, Field(ge=1, le=30)] = 2,
    deep: Annotated[bool, Field(description="Дочитать треды, а не последнюю реплику")] = False,
) -> dict[str, Any]:
    """Что шевелится в СВОИХ задачах за окно + какие появились новые.

    Идёт по recent-хвосту, а не по списку своих member-задач (их тысячи →
    таймаут). Дёшево: последняя реплика уже приходит в recent.
    """
    return api.tasks_updates(_client(), days=days, deep=deep)


# ------------------------------------------------------------------ чаты и проекты


@b24_tool
def chat_read(
    chat_id: Annotated[int | None, Field(ge=1, description="Групповой/проектный чат")] = None,
    user_id: Annotated[int | None, Field(ge=1, description="Личка с человеком")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    full: bool = False,
    include_system: bool = False,
) -> dict[str, Any]:
    """Прочитать диалог, где ты участник: групповой чат или личку.

    Вложения отдаются списком (имя, размер, disk_id) — ссылки на скачивание
    сюда не попадают, они могут нести auth-параметр.
    """
    return api.chat_read(
        _client(), chat_id=chat_id, user_id=user_id, limit=limit,
        full=full, include_system=include_system,
    )


@b24_tool
def projects_list(limit: Annotated[int, Field(ge=1, le=50)] = 10) -> dict[str, Any]:
    """Проекты-коллабы по свежести активности.

    Проект группирует чаты (задачи, синки, под-чаты); их содержания обычно нет
    в заметках — это прямой слой слепых пятен.
    """
    return api.projects_list(_client(), limit=limit)


@b24_tool
def project_chats(project_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
    """Из чего состоит проект: дочерние чаты (задачи, синки, под-чаты).

    Главный чат проекта сюда не входит — это и есть `project_id`.
    """
    return api.project_chats(_client(), project_id)


@b24_tool
def project_read(
    chat_id: Annotated[int, Field(ge=1, description="Чат проекта: главный или дочерний")],
    days: Annotated[int | None, Field(ge=1, le=90, description="Окно; без него — последнее")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    live_only: Annotated[bool, Field(description="Отбросить системные записи")] = True,
) -> dict[str, Any]:
    """Прочитать обсуждение в чате проекта."""
    return api.project_read(
        _client(), chat_id, days=days, limit=limit, live_only=live_only
    )


# ------------------------------------------------------------------ звонки


@b24_tool
def followups_list(
    days: Annotated[int, Field(ge=1, le=90)] = 7,
    user_id: Annotated[int | None, Field(ge=1, description="Только под админскими правами")] = None,
    max_items: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Завершённые звонки с ГОТОВЫМ AI-разбором за окно.

    Звонки без разбора портал не возвращает — пустой список это норма, а не сбой.
    """
    return api.followups_list(_client(), days=days, user_id=user_id, max_items=max_items)


@b24_tool
def followup_get(
    call_id: Annotated[int, Field(ge=1)],
    transcript: Annotated[bool, Field(description="Добавить полную расшифровку (тяжело)")] = False,
) -> dict[str, Any]:
    """Разбор звонка: тема, повестка, договорённости, action items, участники.

    ⚠️ `action_items` — СЫРЬЁ портала, а не готовый список твоих задач. Кому
    принадлежит задача, решай по говорящему и адресату: «ты сделай» → владелец
    адресат, «я сделаю» → владелец говорящий. Для этого бери `transcript=True`.
    """
    return api.followup_get(_client(), call_id, transcript=transcript)


def _short(value: Any, limit: int = 300) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def main() -> None:
    """Точка входа: `b24-mcp` или `python -m b24_mcp.server`."""
    logger.info("b24-mcp: старт (read-only, %d инструментов)", 13)
    mcp.run()


if __name__ == "__main__":
    main()
