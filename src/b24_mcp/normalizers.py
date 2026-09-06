"""Единый контракт ответов + чистка текста.

Зачем отдельный модуль (введён с MCP-фасадом):
- CLI печатает markdown для ЧЕЛОВЕКА; MCP отдаёт структуру для МОДЕЛИ. Контракт
  один на все list-инструменты — модель не угадывает форму ответа и не парсит
  markdown;
- чистка текста (BB-разметка, HTML) собрана в одном месте. BB-часть
  переиспользует `clean_bb` из commands/_comments.py — единственный источник
  правды по BB-разметке Битрикса (ленивый импорт, чтобы не плодить циклы);
- лимиты клампятся здесь же: модель не может случайно попросить 10000 записей.

stdlib-only, как и остальное ядро.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_BR = re.compile(r"<br\s*/?>", re.I)
_SPACES = re.compile(r"[ \t]{2,}")
_NEWLINES = re.compile(r"\n{3,}")

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


def clamp(
    value: Any, *, lo: int = 1, hi: int = MAX_LIMIT, default: int = DEFAULT_LIMIT
) -> int:
    """Привести лимит к безопасному диапазону.

    None / мусор → default. Выход за границы → ближайшая граница. Никаких
    исключений: клампить надёжнее, чем падать на кривом аргументе модели.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def page(
    items: Iterable[Any], *, limit: int, total: int | None = None
) -> dict[str, Any]:
    """Единый контракт list-ответа: {items, count, limit}.

    `count` — сколько реально отдано (len(items)), а не сколько есть на портале;
    `total`, если известен, добавляется отдельным полем, чтобы не путать.
    """
    lst = list(items)
    out: dict[str, Any] = {"items": lst, "count": len(lst), "limit": limit}
    if total is not None:
        out["total"] = total
    return out


def clean_text(text: str | None, *, drop_quotes: bool = False) -> str:
    """BB-разметка + HTML → читаемый текст.

    BB отдаём в clean_bb (там же обработка [USER=]/[URL]/[DISK FILE]/цитат),
    сверху снимаем HTML (в полях задач и отчётов приходит <br> и теги) и
    схлопываем лишние пробелы/переводы строк.
    """
    if not text:
        return ""
    try:  # ленивый импорт: единственный источник правды по BB — _comments
        from .commands._comments import clean_bb

        s = clean_bb(text, drop_quotes=drop_quotes)
    except Exception:  # чистка никогда не должна ронять ответ
        s = text
    s = _HTML_BR.sub("\n", s)
    s = _HTML_TAG.sub("", s)
    s = _SPACES.sub(" ", s)
    s = _NEWLINES.sub("\n\n", s)
    return s.strip()


def trim(text: str | None, max_len: int | None = None) -> str:
    """Обрезать длинный текст с явной пометкой, что он усечён."""
    s = text or ""
    if max_len is None or len(s) <= max_len:
        return s
    return s[:max_len].rstrip() + f"… [обрезано, всего {len(s)} симв.]"


def is_yes(value: Any) -> bool:
    """Нормализовать булев флаг Битрикса.

    Контракт-ловушка: одно и то же поле приходит то строкой "Y"/"N", то
    настоящим bool, то 1/0 — зависит от метода и от того, владелец ты или
    участник. Сравнение `== "Y"` молча врёт (митинг превращался в личный блок).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().upper() in {"Y", "YES", "TRUE", "1"}
