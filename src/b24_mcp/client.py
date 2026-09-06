"""Тонкий клиент над Bitrix24 REST.

Стратегические решения:
- stdlib-only (urllib) — чтобы не добавлять зависимости в инструмент личной памяти;
- read-only по умолчанию: write-методы доступны только когда вызывающий код передаёт
  allow_write=True в конструктор клиента. Это защита от случайного `.call("tasks.task.add", ...)`
  из команды-обзорщика;
- стандартный paging через `next`/`total` — list-методы возвращают все страницы;
- при ошибке поднимается B24Error с понятным сообщением, без печати хука.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

from .config import Config


# Методы, которые могут изменить состояние портала. Список не претендует на полноту,
# но покрывает всё, что нужно нам. Для случая «новый write-метод не в списке» — нам
# не критично, потому что мы и так не будем его вызывать из read-команд.
_WRITE_METHODS = {
    "tasks.task.add",
    "tasks.task.update",
    "tasks.task.delete",
    "tasks.task.complete",
    "tasks.task.start",
    "tasks.task.pause",
    "task.item.add",
    "task.item.update",
    "task.item.delete",
    "calendar.event.add",
    "calendar.event.update",
    "calendar.event.delete",
    "im.message.add",
    "im.message.update",
    "im.message.delete",
    "im.disk.file.commit",
    "im.chat.add",
    "im.notify.personal.add",
    "user.add",
    "user.update",
    # Диск — загрузка файлов меняет состояние портала, гейтим как write.
    "disk.folder.uploadfile",
    "disk.storage.uploadfile",
}


class B24Error(RuntimeError):
    def __init__(self, method: str, code: str, message: str) -> None:
        super().__init__(f"{method}: {code} — {message}")
        self.method = method
        self.code = code


class Client:
    def __init__(self, cfg: Config, *, allow_write: bool = False, timeout: float = 20.0) -> None:
        self._webhook = cfg.webhook
        self._allow_write = allow_write
        self._timeout = timeout
        self.user_id = cfg.user_id

    # --- low level ---
    def _open_post(self, url: str, body_pairs: list[tuple[str, Any]], method_label: str) -> dict[str, Any]:
        """POST form-urlencoded body_pairs на url, вернуть распарсенный payload.

        Общий транспорт для call() и call_batch(): retry на 5xx / сетевых, единый
        разбор HTTPError. НЕ интерпретирует payload["error"] — это делает вызывающий
        (у batch своя семантика result_error). Хук в сообщениях не светит.
        """
        body = urllib.parse.urlencode(body_pairs, doseq=True).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                try:
                    err_body = json.loads(e.read().decode("utf-8"))
                except Exception:
                    err_body = {}
                raise B24Error(
                    method_label,
                    err_body.get("error", f"HTTP_{e.code}"),
                    err_body.get("error_description", str(e.reason)),
                )
            except (TimeoutError, socket.timeout):
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise B24Error(method_label, "TIMEOUT", "превышено время ожидания ответа портала")
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise B24Error(method_label, "NETWORK", str(e.reason))
        raise B24Error(method_label, "NETWORK", "не удалось получить ответ портала")

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method in _WRITE_METHODS and not self._allow_write:
            raise B24Error(
                method,
                "WRITE_DISABLED",
                "вызов write-метода в read-only режиме: сервер выставляет только чтение",
            )
        url = self._webhook + method + ".json"
        payload = self._open_post(url, _flatten(params or {}), method)
        if "error" in payload:
            raise B24Error(
                method, payload["error"], payload.get("error_description", "")
            )
        return payload.get("result")

    def call_batch(
        self, cmds: dict[str, tuple[str, dict[str, Any] | None]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Нативный B24 `batch`: до 50 подкоманд за один HTTP-запрос.

        Схлопывает N round-trip'ов в N/50 — снимает главный источник таймаутов в
        песочнице (55+ личных чатов × отдельный im.dialog.messages.get). cmds:
        ключ → (method, params). Возвращает (result_by_key, error_by_key): у
        успешных ключ в result, у упавших — в error (подкоманда падает независимо,
        halt=0). Порядок ключей сохраняется. Ошибка одной подкоманды не рушит батч.
        """
        if not cmds:
            return {}, {}
        if len(cmds) > 50:
            raise ValueError(f"batch поддерживает до 50 подкоманд, дано {len(cmds)}")
        for key, (m, _p) in cmds.items():
            if m in _WRITE_METHODS and not self._allow_write:
                raise B24Error(m, "WRITE_DISABLED", "write-метод в read-only батче")
        pairs: list[tuple[str, Any]] = [("halt", "0")]
        for key, (method, params) in cmds.items():
            qs = urllib.parse.urlencode(_flatten(params or {}), doseq=True)
            pairs.append((f"cmd[{key}]", f"{method}?{qs}" if qs else method))
        url = self._webhook + "batch.json"
        payload = self._open_post(url, pairs, "batch")
        if "error" in payload:
            raise B24Error("batch", payload["error"], payload.get("error_description", ""))
        result = payload.get("result") or {}
        if not isinstance(result, dict):
            return {}, {}
        return (result.get("result") or {}), (result.get("result_error") or {})

    def call_list(self, method: str, params: dict[str, Any] | None = None) -> Iterator[Any]:
        """Универсальный пагинатор для list-методов.

        Bitrix24 возвращает либо list, либо dict (например, tasks.task.list → {tasks: [...]}).
        Метод сам понимает формат и итерирует. start/next — стандартные поля B24.
        """
        params = dict(params or {})
        start = 0
        # У tasks.task.list ключевое поле — `tasks`. У большинства других — root list.
        list_key = _list_key_for(method)
        while True:
            params["start"] = start
            url = self._webhook + method + ".json"
            body = urllib.parse.urlencode(_flatten(params), doseq=True).encode("utf-8")
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if "error" in payload:
                raise B24Error(
                    method, payload["error"], payload.get("error_description", "")
                )
            result = payload.get("result")
            if list_key and isinstance(result, dict):
                items = result.get(list_key, [])
            elif isinstance(result, list):
                items = result
            else:
                items = []
            for it in items:
                yield it
            nxt = payload.get("next")
            if nxt is None or not items:
                break
            start = nxt


def _list_key_for(method: str) -> str | None:
    """У некоторых методов result — это dict со списком внутри."""
    return {
        "tasks.task.list": "tasks",
    }.get(method)


def _flatten(d: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    """Битрикс ожидает params в виде filter[KEY]=value, select[0]=ID и т.д.

    Преобразуем вложенный dict/list в плоский список пар.
    """
    out: list[tuple[str, Any]] = []
    for k, v in d.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out.extend(_flatten(v, key))
        elif isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                ikey = f"{key}[{i}]"
                if isinstance(item, dict):
                    out.extend(_flatten(item, ikey))
                else:
                    out.append((ikey, _scalar(item)))
        else:
            out.append((key, _scalar(v)))
    return out


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "Y" if v else "N"
    return str(v)
