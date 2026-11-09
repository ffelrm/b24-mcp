"""Клиент к методам AI Follow-up звонков: call.followup.* и syncconnector.followup.*.

Два источника с одним контрактом:
  call  — звонки Битрикс24 (модуль call):        call.followup.list / .get, скоуп `call`;
  sync  — встречи Синка (модуль syncconnector):  syncconnector.followup.list / .get,
          скоуп `syncconnector`.
Нумерации callId у источников разные и могут совпасть, поэтому get всегда
знает свой источник, а collect() помечает каждую строку полем `source`.

Отличия Синка от call: только данные владельца вебхука (participantId
игнорируется), любое AI-поле в select снижает лимит страницы до 20, есть поле
chatId, страница может прийти пустой при hasMore=true. Транспорт (b) ниже
оборачивает только модуль call — Синк всегда идёт вебхуком или OAuth.

Это методы REST **v3** (путь /rest/api/...), не v1. Ключевой нюанс URL, выясненный
эмпирически 2026-06-17 на облачном портале:

  Вебхук РАБОТАЕТ с v3, если префикс `api/` стоит ПЕРЕД uid/token:
      POST <portal>/rest/api/<uid>/<token>/call.followup.<method>   ✅
  Тупиковые формы (для памяти, чтобы не перебирать заново):
      /rest/<uid>/<token>/call.followup.list[.json]  → ERROR_METHOD_NOT_FOUND (v1-роутер)
      /rest/<uid>/<token>/api/call.followup.list     → ERROR_METHOD_NOT_FOUND
      /rest/api/call.followup.list?auth=<webhook-token> → ACCESSDENIED (это форма OAuth)
  Нужен скоуп `call` у вебхука (включается в правах входящего вебхука).

Это ДЕФОЛТНЫЙ транспорт — отдельных кредов не нужно, берётся из B24_WEBHOOK.

Опционально поддерживаются ещё два транспорта (если кред задан в .env):
  (a) OAuth локального приложения со скоупом `call` → access_token (+ refresh).
      Транспорт: POST <portal>/rest/api/<method>?auth=<access_token>, тело JSON.
  (b) кастомный rest.php-action со своим hash (если такой есть на твоём портале).
      Транспорт: POST <portal>/rest.php?action=<followups_action>, hash в теле.
      Action на стороне портала оборачивает \\Bitrix\\Call\\Service\\FollowUpReader
      и возвращает {status:"success", result:{...}} с тем же result, что v3.

Принципы те же, что у основного Client:
- stdlib-only (urllib);
- read-only (*.followup.list / .get не меняют данные);
- секреты (access_token / refresh_token / hash / client_secret) НИКОГДА не
  печатаются в stdout/stderr — даже в --debug;
- понятные ошибки.

Словарь select — ниже (VALID_SELECT).
"""
from __future__ import annotations

import http.client
import json
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .client import PortalError
from .config import Config

# --- Словарь select: поля, которые принимают оба источника ---
SELECT_META = {
    "callId", "callType", "initiatorId", "startDate", "endDate", "durationSeconds",
    "uuid", "participants", "tracks", "outcomes", "language", "version", "createdAt",
}
SELECT_BLOCKS = {"transcription", "overview", "summary", "insights", "evaluation"}
SELECT_SUBPATHS = {
    "transcription.language", "transcription.segments",
    "overview.topic", "overview.detailedTakeaways", "overview.meetingType",
    "overview.agenda", "overview.agreements", "overview.actionItems", "overview.meetings",
    "insights.speakerEvaluationAvailable", "insights.speakerAnalysis",
    "insights.meetingStrengths", "insights.meetingWeaknesses",
    "insights.speechStyleInfluence", "insights.engagementLevel",
    "insights.areasOfResponsibility", "insights.finalRecommendations",
    "evaluation.efficiencyValue", "evaluation.calendar", "evaluation.criteria",
}
VALID_SELECT = SELECT_META | SELECT_BLOCKS | SELECT_SUBPATHS
# Поля, которые есть только у Синка.
SELECT_SYNC_ONLY = {"chatId"}
# «Тяжёлые» поля снижают максимум pagination.limit до 20 (см. доку).
HEAVY_SELECT = {"transcription", "transcription.segments", "overview", "insights"}
# У Синка до 20 снижает ЛЮБОЕ AI-поле, включая overview.topic.
HEAVY_SELECT_SYNC = SELECT_BLOCKS | SELECT_SUBPATHS

MENTION_FORMATS = ("bb", "html", "none")
DEFAULT_LIMIT = 50
MAX_LIMIT_LIGHT = 200
MAX_LIMIT_HEAVY = 20

# Источник → префикс метода и скоуп вебхука.
SOURCES = {"call": "call.followup", "sync": "syncconnector.followup"}
SOURCE_SCOPE = {"call": "call", "sync": "syncconnector"}
ALL_SOURCES = ("call", "sync")

# Дефолтный набор полей для команды list — дёшево и информативно.
DEFAULT_LIST_SELECT = [
    "callId", "callType", "startDate", "durationSeconds",
    "participants", "outcomes", "overview.topic", "overview.actionItems",
]


def default_list_select(source: str = "call") -> list[str]:
    """Поля строки списка. Синку добавляем chatId — по нему встреча связывается с чатом."""
    if source == "sync":
        return DEFAULT_LIST_SELECT + ["chatId"]
    return list(DEFAULT_LIST_SELECT)


class FollowUpsError(PortalError):
    """Ошибка *.followup.* . Текст уже очищен от секретов.

    Наследует PortalError → несёт kind/exit_code/hint: сеть, доступ и ошибка
    метода различаются так же, как у основного клиента.
    """


def _iso_from(d: date | datetime) -> str:
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%dT%H:%M:%SZ")
    return d.strftime("%Y-%m-%dT00:00:00Z")


def _iso_to(d: date | datetime) -> str:
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%dT%H:%M:%SZ")
    return d.strftime("%Y-%m-%dT23:59:59Z")


def validate_select(select: list[str] | None, source: str = "call") -> tuple[list[str], bool]:
    """Проверить select против словаря источника. Вернуть (очищенный список, heavy?).

    None/[] → ([], False) — метаданные по умолчанию.
    Неизвестное значение → FollowUpsError(invalid_select_field).
    """
    if not select:
        return [], False
    valid = VALID_SELECT | SELECT_SYNC_ONLY if source == "sync" else VALID_SELECT
    clean: list[str] = []
    for raw in select:
        f = str(raw).strip()
        if not f:
            continue
        if f not in valid:
            raise FollowUpsError(
                f"invalid_select_field: неизвестное поле '{f}'. "
                f"Допустимые — метаданные + transcription/overview/"
                f"summary/insights/evaluation и их dotted-подполя."
            )
        clean.append(f)
    heavy_set = HEAVY_SELECT_SYNC if source == "sync" else HEAVY_SELECT
    heavy = any(f in heavy_set for f in clean)
    return clean, heavy


def _cap_limit(limit: int | None, heavy: bool) -> int:
    cap = MAX_LIMIT_HEAVY if heavy else MAX_LIMIT_LIGHT
    if limit is None:
        return MAX_LIMIT_HEAVY if heavy else DEFAULT_LIMIT
    if limit < 1:
        return 1
    return min(limit, cap)


class FollowUpsClient:
    def __init__(self, cfg: Config, *, source: str = "call", timeout: float = 30.0) -> None:
        if source not in SOURCES:
            raise FollowUpsError(f"неизвестный источник '{source}': допустимы {', '.join(SOURCES)}")
        self._cfg = cfg
        self._source = source
        self._prefix = SOURCES[source]
        self._portal = cfg.portal
        self._timeout = timeout
        # Вебхук как ДЕФОЛТНЫЙ транспорт v3: /rest/api/<uid>/<token>/<method>.
        # Ключ: префикс `api/` идёт ПЕРЕД uid/token, а не после (проверено 2026-06-17).
        _wh = (cfg.webhook or "").rstrip("/")
        _wp = urllib.parse.urlparse(_wh).path.strip("/").split("/")
        self._wh_uid = _wp[-2] if len(_wp) >= 2 else ""
        self._wh_token = _wp[-1] if _wp else ""
        # In-memory креды (могут обновиться при refresh).
        self._access_token = cfg.oauth_access_token
        self._refresh_token = cfg.oauth_refresh_token
        self._client_id = cfg.oauth_client_id
        self._client_secret = cfg.oauth_client_secret
        self._token_url = cfg.oauth_token_url or "https://oauth.bitrix.info/oauth/token/"
        self._hash = cfg.followups_hash
        self._action = cfg.followups_action
        self._env_path = cfg.env_path

    # ---------- секреты ----------
    def _sanitize(self, s: str) -> str:
        out = s or ""
        for sec in (self._wh_token, self._access_token, self._refresh_token,
                    self._hash, self._client_secret):
            if sec:
                out = out.replace(sec, "<redacted>")
        return out

    @property
    def transport(self) -> str:
        # OAuth / кастомный action — только если кред явно задан в .env.
        # Иначе дефолт — вебхук (он умеет v3 через /rest/api/<uid>/<token>/).
        if self._access_token:
            return "oauth"
        # rest.php-action оборачивает только модуль call — Синк через него не ходит.
        if self._hash and self._source == "call":
            return "action"
        if self._wh_uid and self._wh_token:
            return "webhook"
        return "none"

    def _ensure_transport(self) -> None:
        if self.transport == "none":
            raise FollowUpsError(
                "нет транспорта: невалидный B24_WEBHOOK в .env, и не задан "
                "ни OAuth-токен (B24_OAUTH_ACCESS_TOKEN), ни FOLLOWUPS_HASH. "
                f"Нужен scope `{SOURCE_SCOPE[self._source]}` у вебхука — см. README, "
                "раздел про фоллоу-апы."
            )

    # ---------- низкий уровень ----------
    def _post(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Один вызов <prefix>.<method>. Возвращает result (dict). Транспорт-агностично."""
        self._ensure_transport()
        if self.transport == "oauth":
            return self._post_oauth_v3(method, params)
        if self.transport == "action":
            return self._post_action(method, params)
        return self._post_webhook_v3(method, params)

    def _send_v3(self, url: str, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST JSON на v3-эндпоинт. Возвращает (http_status, payload). 5xx — ретраим.
        HTTP-ошибки НЕ кидаем (отдаём код + тело), чтобы oauth мог рефрешнуть на 401."""
        data = json.dumps(params, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return e.code, self._read_err(e)
            except (TimeoutError, urllib.error.URLError) as e:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise FollowUpsError(self._sanitize(f"сеть: {getattr(e, 'reason', e)}"))
            except (http.client.HTTPException, OSError) as e:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise FollowUpsError(self._sanitize(f"сеть: {type(e).__name__}")) from None
            except ValueError:
                raise FollowUpsError("ответ портала не JSON") from None
        raise FollowUpsError("v3: не удалось получить ответ")

    def _result_or_raise(self, payload: dict[str, Any], method: str = "") -> dict[str, Any]:
        if isinstance(payload, dict) and "error" in payload:
            err = payload["error"]
            where = f"{self._prefix}.{method}: " if method else ""
            if isinstance(err, dict):
                code = str(err.get("code") or "")
                msg = f"{where}{code}: {err.get('message', '')}"
                validation = err.get("validation")
                if isinstance(validation, list) and validation and isinstance(validation[0], dict):
                    msg += f" (поле {validation[0].get('field')})"
                if "SCOPE" in code.upper():
                    msg += (f" — у вебхука нет скоупа `{SOURCE_SCOPE[self._source]}`: "
                            f"добавь его в правах входящего вебхука")
                raise FollowUpsError(self._sanitize(msg))
            raise FollowUpsError(self._sanitize(
                f"{where}{err}: {payload.get('error_description', '')}"))
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            raise FollowUpsError("неожиданный ответ v3: нет result-объекта")
        return result

    def _post_webhook_v3(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """ДЕФОЛТ: вебхук на v3 — /rest/api/<uid>/<token>/<prefix>.<method>."""
        url = f"{self._portal}/rest/api/{self._wh_uid}/{self._wh_token}/{self._prefix}.{method}"
        _status, payload = self._send_v3(url, params)
        return self._result_or_raise(payload, method)

    def _post_oauth_v3(self, method: str, params: dict[str, Any], *, _retry: bool = True) -> dict[str, Any]:
        """Альтернатива: OAuth локального приложения — /rest/api/<prefix>.<m>?auth=<token>."""
        q = urllib.parse.urlencode({"auth": self._access_token})
        url = f"{self._portal}/rest/api/{self._prefix}.{method}?{q}"
        status, payload = self._send_v3(url, params)
        if status == 401 and _retry and self._can_refresh():
            self._refresh_access_token()
            return self._post_oauth_v3(method, params, _retry=False)
        return self._result_or_raise(payload, method)

    def _post_action(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Транспорт (b): кастомный rest.php-action портала (опциональная альтернатива вебхуку).

        Контракт action: принимает {hash, mode: "list"|"get", ...params}, возвращает
        {status:"success", result:{items|item,...}} с тем же result, что REST v3.
        """
        url = f"{self._portal}/rest.php?action={urllib.parse.quote(self._action)}"
        body = {"hash": self._hash, "mode": method, **params}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if e.code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                body = self._read_err(e)
                raise FollowUpsError(self._sanitize(
                    f"HTTP {e.code}: {body.get('result') or body.get('message') or e.reason}"))
            except (TimeoutError, urllib.error.URLError) as e:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise FollowUpsError(self._sanitize(f"сеть: {getattr(e, 'reason', e)}"))
            except (http.client.HTTPException, OSError) as e:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise FollowUpsError(self._sanitize(f"сеть: {type(e).__name__}")) from None
            except ValueError:
                raise FollowUpsError("ответ портала не JSON") from None
        if payload.get("status") != "success":
            msg = payload.get("result") or payload.get("message") or "unknown error"
            raise FollowUpsError(self._sanitize(str(msg)))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise FollowUpsError("неожиданный ответ action: result не dict")
        return result

    def _read_err(self, e: urllib.error.HTTPError) -> dict[str, Any]:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {}

    # ---------- OAuth refresh ----------
    def _can_refresh(self) -> bool:
        return bool(self._refresh_token and self._client_id and self._client_secret)

    def _refresh_access_token(self) -> None:
        """Обновить access_token по refresh_token. Записать новые токены в .env (mode 600).

        Секреты не печатаются. Bitrix может ротировать refresh_token — сохраняем оба.
        """
        if not self._can_refresh():
            raise FollowUpsError("access_token истёк, а refresh-кред неполный "
                                 "(нужны B24_OAUTH_REFRESH_TOKEN/CLIENT_ID/CLIENT_SECRET)")
        q = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
        })
        req = urllib.request.Request(f"{self._token_url}?{q}", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                tok = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = self._read_err(e)
            raise FollowUpsError(self._sanitize(
                f"refresh не удался (HTTP {e.code}): {body.get('error_description') or body.get('error') or e.reason}"))
        except (TimeoutError, urllib.error.URLError) as e:
            raise FollowUpsError(self._sanitize(f"refresh: сеть: {getattr(e, 'reason', e)}"))
        except (http.client.HTTPException, OSError) as e:
            raise FollowUpsError(self._sanitize(f"сеть: {type(e).__name__}")) from None
        except ValueError:
            raise FollowUpsError("ответ портала не JSON") from None
        new_access = tok.get("access_token")
        if not new_access:
            raise FollowUpsError("refresh: портал не вернул access_token")
        self._access_token = new_access
        if tok.get("refresh_token"):
            self._refresh_token = tok["refresh_token"]
        self._persist_tokens()

    def _persist_tokens(self) -> None:
        if not self._env_path:
            return
        updates = {"B24_OAUTH_ACCESS_TOKEN": self._access_token or ""}
        if self._refresh_token:
            updates["B24_OAUTH_REFRESH_TOKEN"] = self._refresh_token
        _update_env_file(Path(self._env_path), updates)

    # ---------- высокий уровень ----------
    def list(
        self,
        date_from: date | datetime,
        date_to: date | datetime,
        *,
        select: list[str] | None = None,
        participant_id: int | None = None,
        order: str = "desc",
        limit: int | None = None,
        max_items: int | None = None,
        mention_format: str = "none",
    ) -> Iterator[dict[str, Any]]:
        """Итерировать Follow-up за период (курсорная пагинация под капотом)."""
        if participant_id and self._source == "sync":
            # Портал примет participantId и молча вернёт звонки владельца вебхука —
            # они выглядели бы как звонки участника.
            raise FollowUpsError(
                "Синк не фильтрует по участнику: список всегда владельца вебхука")
        clean_select, heavy = validate_select(select, self._source)
        page_limit = _cap_limit(limit, heavy)
        if mention_format not in MENTION_FORMATS:
            mention_format = "none"
        order_dir = "asc" if str(order).lower() == "asc" else "desc"

        flt: dict[str, Any] = {"startDate": {"from": _iso_from(date_from), "to": _iso_to(date_to)}}
        if participant_id:
            flt["participantId"] = int(participant_id)

        after_cursor: dict[str, Any] | None = None
        yielded = 0
        while True:
            params: dict[str, Any] = {
                "filter": flt,
                "order": {"startDate": order_dir},
                "pagination": {"limit": page_limit},
                "mentionFormat": mention_format,
            }
            if clean_select:
                params["select"] = clean_select
            if after_cursor:
                params["pagination"]["afterCursor"] = after_cursor
            result = self._post("list", params)
            items = result.get("items") or []
            for it in items:
                yield it
                yielded += 1
                if max_items and yielded >= max_items:
                    return
            # Пустая страница при hasMore=true — не конец списка: идём дальше по
            # курсору. Стоп — только hasMore=false или курсор не сдвинулся.
            if not result.get("hasMore"):
                return
            next_cursor = result.get("afterCursor")
            if not next_cursor or next_cursor == after_cursor:
                return
            after_cursor = next_cursor

    def get(
        self,
        call_id: int,
        *,
        select: list[str] | None = None,
        mention_format: str = "none",
    ) -> dict[str, Any]:
        """Полный Follow-up по одному звонку. select=None → полный объект."""
        if mention_format not in MENTION_FORMATS:
            mention_format = "none"
        params: dict[str, Any] = {"callId": int(call_id), "mentionFormat": mention_format}
        if select is not None:
            clean_select, _ = validate_select(select, self._source)
            params["select"] = clean_select  # [] → только метаданные
        result = self._post("get", params)
        item = result.get("item")
        if not isinstance(item, dict):
            raise FollowUpsError("неожиданный ответ: нет item-объекта")
        return item


def _start_key(item: dict[str, Any]) -> datetime:
    raw = str(item.get("startDate") or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def collect(
    cfg: Config,
    date_from: date | datetime,
    date_to: date | datetime,
    *,
    sources: tuple[str, ...] | list[str] = ALL_SOURCES,
    participant_id: int | None = None,
    order: str = "desc",
    max_items: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, FollowUpsError]]:
    """Фоллоу-апы нескольких источников одним списком: (items, errors).

    Источники опрашиваются независимо: отказ одного (нет скоупа, на портале
    нет модуля) не роняет другой — его ошибка уходит в errors[source], строки
    остальных в items. Каждая строка помечена полем `source`. Порядок — по
    startDate через все источники, max_items — на весь список.
    """
    items: list[dict[str, Any]] = []
    errors: dict[str, FollowUpsError] = {}
    for src in sources:
        fc = None
        try:
            fc = FollowUpsClient(cfg, source=src)
            for it in fc.list(date_from, date_to, select=default_list_select(src),
                              participant_id=participant_id, order=order,
                              max_items=max_items):
                it["source"] = src
                items.append(it)
        except FollowUpsError as e:
            errors[src] = e
        finally:
            # OAuth: refresh у одного источника меняет и access, и refresh token
            # (старый refresh одноразовый). Следующий источник берёт свежие,
            # иначе получит invalid_grant и потеряет свои строки.
            if fc is not None:
                cfg = replace(cfg, oauth_access_token=fc._access_token,
                              oauth_refresh_token=fc._refresh_token)
    items.sort(key=_start_key, reverse=str(order).lower() != "asc")
    if max_items:
        items = items[:max_items]
    return items, errors


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Точечно обновить ключи в .env, сохранив остальные строки и mode 600.

    Значения не логируются. Если ключа нет — добавляется в конец.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    remaining = dict(updates)
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in remaining:
                out.append(f"{k}={remaining.pop(k)}")
                continue
        out.append(raw)
    for k, v in remaining.items():
        out.append(f"{k}={v}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 600
    os.replace(tmp, path)
