"""Тонкий клиент над Bitrix24 REST.

Стратегические решения:
- stdlib-only (urllib) — чтобы не добавлять зависимости в ядро;
- read-only по умолчанию и по белому списку: без allow_write=True уходят только
  методы из `_READ_METHODS` (и batch, целиком собранный из них). Любой другой
  метод, включая новый или неизвестный, считается записью и отсекается ДО сети;
- запись не повторяется после неясного исхода: таймаут, обрыв или 5xx у
  write-запроса дают B24Error UNKNOWN_OUTCOME вместо ретрая;
- стандартный paging через `next`/`total` — list-методы возвращают все страницы;
- при ошибке поднимается B24Error с понятным сообщением, без хука и токена.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

from .config import Config


# Только проверенные read-методы, которые вызывает этот пакет. Новый или
# неизвестный метод — запись: без allow_write он не уйдёт, а после неясного
# исхода не повторится. Имена точные (включая регистр Recent): никаких эвристик
# по get/list. Новый метод чтения — дописать сюда осознанно; полноту по коду
# пакета сторожит tests/test_read_allowlist.py.
_READ_METHODS = frozenset({
    "user.current",
    "user.get",
    "calendar.event.get",
    "sonet_group.get",
    "bizproc.task.list",
    "tasks.task.get",
    "tasks.task.list",
    "tasks.task.result.list",
    # На доске без стадий портал при первом вызове создаёт стадии по умолчанию;
    # tasks_list этот метод на такой доске не вызывает.
    "task.stages.get",
    "task.commentitem.getlist",
    "im.dialog.messages.get",
    "im.v2.Recent.tail",
    "im.v2.Recent.load",
})


def _is_read_request(method: str, pairs: list[tuple[str, Any]]) -> bool:
    if method != "batch":
        return method in _READ_METHODS
    # batch сам по себе не read. Разбираем именно строки команд, которые
    # будут отправлены: это покрывает и call_batch(), и сырой call("batch").
    commands = []
    for key, value in pairs:
        if key in {"halt", "start"}:
            continue
        # Не допускаем альтернативные формы cmd: PHP может нормализовать
        # скалярные/вложенные ключи иначе, чем наша проверка списка команд.
        if not re.fullmatch(r"cmd\[[^\[\]]*\]", key):
            return False
        commands.append(value)
    return bool(commands) and all(
        isinstance(command, str) and command.split("?", 1)[0] in _READ_METHODS
        for command in commands
    )


# --- Классификация отказов по причине -------------------------------------
#
# Зачем: вызывающий код обязан отличать «данных больше нет» от «канал до
# портала умер». Если оба случая приходят одним B24Error, цикл пагинации ловит
# его и делает `break` — и пустой результат выглядит как честное «активности
# нет», хотя данные просто не получены.
#
# Правило: kind == "transport", "auth" и "unknown_outcome" — фатальны, их НЕЛЬЗЯ
# гасить `break`/`continue`, прогон обязан упасть или пометиться неполным.
# "method" и "write" — локальны для конкретного вызова, деградация по одному
# объекту допустима.

KIND_TRANSPORT = "transport"   # сети/тоннеля/таймаута нет — портал недостижим
KIND_AUTH = "auth"             # хук невалиден, протух, не тот скоуп
KIND_METHOD = "method"         # портал ответил, но метод/параметры не приняты
KIND_WRITE = "write"           # локальный read-only гейт, до сети не дошли
KIND_UNKNOWN_OUTCOME = "unknown_outcome"  # write ушёл, ответа нет — МОГ выполниться

# Коды выхода по причинам, а не «1 на всё» — для скриптов поверх пакета.
# Они должны уметь отличать «сеть легла» (ретраить позже) от «хук протух»
# (звать человека) и от «метод не тот» (чинить код).
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_CONFIG = 2        # нет .env / хук не задан (поднимает config.load)
EXIT_TRANSPORT = 3     # сеть/тоннель/таймаут
EXIT_AUTH = 4          # авторизация, доступ, скоуп
EXIT_METHOD = 5        # ошибка метода API
EXIT_WRITE = 6         # попытка write в read-only режиме
EXIT_PARTIAL = 7       # прогон отработал, но данные неполные (не ошибка)
EXIT_UNKNOWN_OUTCOME = 8  # исход записи неизвестен: сверить, прежде чем повторять

_EXIT_BY_KIND = {
    KIND_TRANSPORT: EXIT_TRANSPORT,
    KIND_AUTH: EXIT_AUTH,
    KIND_METHOD: EXIT_METHOD,
    KIND_WRITE: EXIT_WRITE,
    KIND_UNKNOWN_OUTCOME: EXIT_UNKNOWN_OUTCOME,
}

# Коды портала, означающие проблему доступа, а не логики вызова.
_AUTH_CODES = {
    "NO_AUTH_FOUND", "INVALID_TOKEN", "INVALID_CREDENTIALS", "EXPIRED_TOKEN",
    "WRONG_AUTH_TYPE", "ACCESS_DENIED", "ACCESSDENIED", "INSUFFICIENT_SCOPE",
    "INVALID_REQUEST", "APPLICATION_NOT_FOUND", "HTTP_401", "HTTP_403",
}

_TRANSPORT_CODES = {"NETWORK", "TIMEOUT", "HTTP_502", "HTTP_503", "HTTP_504"}


def _classify(code: str) -> str:
    c = (code or "").upper()
    if c == "WRITE_DISABLED":
        return KIND_WRITE
    if c == "UNKNOWN_OUTCOME":
        return KIND_UNKNOWN_OUTCOME
    if c in _TRANSPORT_CODES:
        return KIND_TRANSPORT
    if c in _AUTH_CODES:
        return KIND_AUTH
    return KIND_METHOD


# Человекочитаемая подсказка «что это значит и что делать» — чтобы не гадать
# по коду ошибки.
HINT_BY_KIND = {
    KIND_TRANSPORT: (
        "портал недостижим (сеть/тоннель/таймаут). Данные НЕ получены — это не "
        "значит «активности нет». Если запуск из песочницы агента: проверь, что "
        "домен портала в списке разрешённых для сетевого доступа."
    ),
    KIND_AUTH: (
        "портал ответил, но отказал в доступе: хук невалиден, протух или у него "
        "нет нужного скоупа. Нужен новый inbound-вебхук / добавить скоуп."
    ),
    KIND_METHOD: (
        "портал ответил, но метод или параметры не приняты — это ошибка вызова, "
        "не сети. Смотри название метода и набор параметров."
    ),
    KIND_WRITE: (
        "write-метод в read-only режиме — гейт сработал ДО обращения к порталу."
    ),
    KIND_UNKNOWN_OUTCOME: (
        "запрос на запись ушёл, а ответа нет: действие МОГЛО выполниться. "
        "Автоповтора не было. Сверь результат на портале и повторяй, только "
        "если действия там нет."
    ),
}


_TRANSPORT_WORDS = ("сеть:", "network", "tunnel", "таймаут", "timeout", "connection")
_AUTH_WORDS = ("access", "доступ", "token", "токен", "истёк", "auth", "403", "401")


def classify_message(msg: str) -> str:
    """Классификация по тексту — для модулей со своим транспортом.

    `followups_core` ходит на портал мимо Client (другой транспорт: REST v3),
    поэтому кода ошибки у него нет — только текст. Чтобы причины были едиными
    по всему пакету, выводим их из сообщения.
    """
    m = (msg or "").lower()
    if any(w in m for w in _TRANSPORT_WORDS):
        return KIND_TRANSPORT
    if any(w in m for w in _AUTH_WORDS):
        return KIND_AUTH
    return KIND_METHOD


class PortalError(RuntimeError):
    """База для ошибок модулей со своим транспортом.

    Даёт тот же контракт, что и B24Error: kind / is_fatal / exit_code / hint —
    чтобы ошибки фоллоу-апов различались по причинам так же, как остальные.
    """

    @property
    def kind(self) -> str:
        return classify_message(str(self))

    @property
    def is_fatal(self) -> bool:
        return self.kind in (KIND_TRANSPORT, KIND_AUTH)

    @property
    def exit_code(self) -> int:
        return _EXIT_BY_KIND.get(self.kind, EXIT_GENERIC)

    @property
    def hint(self) -> str:
        return HINT_BY_KIND.get(self.kind, "")


class B24Error(RuntimeError):
    def __init__(self, method: str, code: str, message: str) -> None:
        super().__init__(f"{method}: {code} — {message}")
        self.method = method
        self.code = code
        self.kind = _classify(code)

    @property
    def is_fatal(self) -> bool:
        """Отказ канала, а не конкретного вызова.

        Такой отказ НЕЛЬЗЯ гасить `break`/`continue` в циклах пагинации: пустой
        результат после него неотличим от честного «данных нет».
        """
        return self.kind in (KIND_TRANSPORT, KIND_AUTH, KIND_UNKNOWN_OUTCOME)

    @property
    def exit_code(self) -> int:
        return _EXIT_BY_KIND.get(self.kind, EXIT_GENERIC)

    @property
    def hint(self) -> str:
        return HINT_BY_KIND.get(self.kind, "")


def _unknown_outcome(method: str, reason: str) -> B24Error:
    """Write ушёл, ответа нет: действие МОГЛО выполниться, повтор — только после сверки."""
    return B24Error(
        method,
        "UNKNOWN_OUTCOME",
        f"ответ портала не получен ({reason}) — действие МОГЛО выполниться; "
        "сверь результат перед повтором",
    )


def _decode_response(body: bytes, method: str, *, write: bool) -> Any:
    """Ответ записи должен явно подтверждать результат или ошибку API."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if write:
            # Ни тело, ни текст исключения парсера не включаем: там могут быть
            # секреты. Даже полученный HTTP-ответ не доказывает исход записи.
            raise _unknown_outcome(method, "ответ не является JSON в UTF-8") from None
        raise
    if write and (
        not isinstance(payload, dict) or not ("result" in payload or "error" in payload)
    ):
        raise _unknown_outcome(method, "ответ без результата или ошибки API")
    return payload


class Client:
    def __init__(self, cfg: Config, *, allow_write: bool = False, timeout: float = 20.0) -> None:
        self._webhook = cfg.webhook
        self._allow_write = allow_write
        self._timeout = timeout
        self.user_id = cfg.user_id
        # База портала (scheme://host) без пути и токена — для абсолютных ссылок.
        u = urllib.parse.urlparse(cfg.webhook or "")
        self.portal = f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""

    # --- low level ---
    def _open_post(
        self,
        url: str,
        body_pairs: list[tuple[str, Any]],
        method_label: str,
    ) -> dict[str, Any]:
        """POST form-urlencoded body_pairs на url, вернуть распарсенный payload.

        Общий транспорт для call(), call_list() и call_batch(): retry на 5xx /
        сетевых, единый разбор HTTPError. НЕ интерпретирует payload["error"] — это
        делает вызывающий (у batch своя семантика result_error). Хук не светит.

        Метод вне _READ_METHODS (или batch с такой командой) — повтора НЕТ.
        После таймаута, сетевой ошибки или 5xx запрос мог дойти и выполниться,
        и второй такой же задвоит действие (например, отправит то же сообщение
        дважды). Такой исход — B24Error UNKNOWN_OUTCOME: сверка и решение о
        повторе остаются за человеком. Read идемпотентен, его ретраи остаются.
        """
        # Единственная точка допуска к сети и выбора retry для всех REST-путей.
        write = not _is_read_request(method_label, body_pairs)
        if write and not self._allow_write:
            raise B24Error(method_label, "WRITE_DISABLED", "write-метод в read-only режиме")
        body = urllib.parse.urlencode(body_pairs, doseq=True).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return _decode_response(resp.read(), method_label, write=write)
            except urllib.error.HTTPError as e:
                # Для write любой 5xx — исход неизвестен: за прокси-кодами вроде
                # 524 бэкенд мог успеть выполнить запрос.
                if write and e.code >= 500:
                    raise _unknown_outcome(method_label, f"HTTP {e.code}")
                if e.code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                try:
                    try:
                        error_body = e.read()
                    except Exception as read_error:
                        # Даже 4xx не подтверждает отказ, если тело не прочитано.
                        # Не переносим текст исключения: в нём могут быть секреты.
                        if write:
                            raise _unknown_outcome(
                                method_label, f"чтение ответа: {type(read_error).__name__}"
                            ) from None
                        raise
                    err_body = _decode_response(error_body, method_label, write=write)
                except B24Error:
                    raise
                except Exception:
                    err_body = {}
                raise B24Error(
                    method_label,
                    err_body.get("error", f"HTTP_{e.code}"),
                    err_body.get("error_description", str(e.reason)),
                )
            except (TimeoutError, socket.timeout):
                if write:
                    raise _unknown_outcome(method_label, f"таймаут {self._timeout:g} с")
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise B24Error(method_label, "TIMEOUT", "превышено время ожидания ответа портала")
            except urllib.error.URLError as e:
                if write:
                    raise _unknown_outcome(method_label, f"сеть: {e.reason}")
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise B24Error(method_label, "NETWORK", str(e.reason))
            except http.client.InvalidURL:
                # Кривой URL хука ловится ДО отправки — исход известен.
                raise B24Error(
                    method_label, "CONFIG",
                    "некорректный URL вебхука: проверь B24_WEBHOOK в .env, сам URL не показывай",
                ) from None
            except (OSError, http.client.HTTPException) as e:
                # Обрыв на чтении ответа (RemoteDisconnected, reset) urllib отдаёт
                # сырым, мимо URLError: запрос к этому моменту ушёл целиком. Для
                # read ошибка классифицируется как NETWORK. В причину только имя класса:
                # текст исключений http.client бывает с путём, то есть с токеном.
                if write:
                    raise _unknown_outcome(method_label, f"сеть: {type(e).__name__}")
                raise B24Error(
                    method_label, "NETWORK", f"обрыв соединения: {type(e).__name__}"
                ) from None
        raise B24Error(method_label, "NETWORK", "не удалось получить ответ портала")

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
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

        Схлопывает N round-trip'ов в N/50 — снимает главный источник таймаутов
        при обходе многих диалогов. cmds: ключ → (method, params). Возвращает
        (result_by_key, error_by_key): у успешных ключ в result, у упавших — в
        error (подкоманда падает независимо, halt=0). Порядок ключей сохраняется.
        Ошибка одной подкоманды не рушит батч.
        """
        if not cmds:
            return {}, {}
        if len(cmds) > 50:
            raise ValueError(f"batch поддерживает до 50 подкоманд, дано {len(cmds)}")
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
            # Через общий транспорт: read-only гейт, retry на 5xx и
            # классифицированный B24Error вместо сырого urllib.error.URLError.
            payload = self._open_post(url, _flatten(params), method)
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
