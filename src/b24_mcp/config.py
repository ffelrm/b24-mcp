"""Безопасная загрузка конфигурации из .env.

Принципы:
- .env читается с проверкой прав файла (должны быть 600);
- хук НИКОГДА не печатается в stdout/stderr — даже в --debug;
- если .env отсутствует — CLI пишет понятную ошибку и инструкцию.
"""
from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    webhook: str  # https://<портал>/rest/<id>/<token>/
    user_id: int | None  # владелец хука; нужен для команд «свои…»
    # Опциональные поля для альтернативного API: getWorkReports.
    # Это кастомный action в rest.php (модуль bxsupport), отдельная авторизация
    # через персональный hash. Не путать с REST-вебхуком выше.
    # Hash берётся с https://<portal>/services/openlines/bx_rest.php
    work_reports_hash: str | None = None
    work_reports_portal: str | None = None  # https://<портал> (без хвостового /)
    # --- Транспорт для call.followup.* (REST v3) ---
    # Входящий вебхук НЕ авторизуется роутером /rest/api/ (проверено 2026-06-17:
    # аноним и токен дают идентичный ACCESSDENIED). Поэтому фоллоу-апам нужен
    # ОДИН из двух кредов:
    #   (a) OAuth локального приложения со скоупом `call` → access_token (+ refresh);
    #   (b) кастомный rest.php-action (как getWorkReports) → персональный hash.
    oauth_access_token: str | None = None
    oauth_refresh_token: str | None = None
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_token_url: str | None = None  # def: https://oauth.bitrix.info/oauth/token/
    followups_hash: str | None = None    # для транспорта (b)
    followups_portal: str | None = None  # переопределяет portal для фоллоу-апов
    followups_action: str = "getCallFollowups"  # имя action в rest.php (b)
    env_path: str | None = None  # путь к .env — для write-back при refresh токена

    @property
    def safe_repr(self) -> str:
        """Безопасное представление для логов: без токена."""
        # https://<портал>/rest/<id>/<redacted>/
        parts = self.webhook.rstrip("/").split("/")
        if len(parts) >= 5:
            return f"{'/'.join(parts[:5])}/<redacted>/"
        return "<webhook>"

    @property
    def portal(self) -> str:
        """База портала (scheme://host) без хвостового слэша.

        Приоритет: followups_portal → work_reports_portal → выводим из вебхука.
        Никаких секретов — только scheme+host.
        """
        if self.followups_portal:
            return self.followups_portal.rstrip("/")
        if self.work_reports_portal:
            return self.work_reports_portal.rstrip("/")
        # https://<портал>/rest/<id>/<token>/ → https://<портал>
        from urllib.parse import urlparse
        u = urlparse(self.webhook)
        return f"{u.scheme}://{u.netloc}"


def _project_root() -> Path:
    # config.py → b24_mcp/ → корень репо
    return Path(__file__).resolve().parent.parent


def _candidate_env_paths() -> list[Path]:
    """В порядке приоритета."""
    paths: list[Path] = []
    if env := os.environ.get("B24_MCP_ENV"):
        paths.append(Path(env).expanduser())
    # рядом с репо
    paths.append(_project_root() / ".env")
    # XDG-конфиг
    paths.append(Path.home() / ".config" / "b24-mcp" / ".env")
    return paths


def _parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            data[k] = v
    return data


def _check_permissions(path: Path) -> None:
    """Проверка, что .env не читается group/other.

    На macOS это страховка от случайного `chmod 644`.
    """
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
        sys.stderr.write(
            f"[b24-mcp] ОТКАЗ: {path} читается group/other (mode={oct(mode)}).\n"
            f"  Поставь права 600: chmod 600 {path}\n"
        )
        sys.exit(2)


def load() -> Config:
    chosen: Path | None = None
    for p in _candidate_env_paths():
        if p.exists():
            chosen = p
            break

    if chosen is None:
        env_webhook = os.environ.get("B24_WEBHOOK")
        env_uid = os.environ.get("B24_USER_ID")
        if env_webhook:
            return Config(
                webhook=env_webhook.rstrip("/") + "/",
                user_id=int(env_uid) if env_uid else None,
            )
        sys.stderr.write(
            "[b24-mcp] .env не найден. Создай файл и положи туда хук.\n"
            "  cp .env.example .env && chmod 600 .env\n"
            "  Поиск шёл по: B24_MCP_ENV, <пакет>/.env, ~/.config/b24-mcp/.env\n"
        )
        sys.exit(2)

    _check_permissions(chosen)
    data = _parse_env_file(chosen)
    webhook = data.get("B24_WEBHOOK", "").strip()
    if not webhook.startswith("https://") or "/rest/" not in webhook:
        sys.stderr.write(
            f"[b24-mcp] В {chosen} нет валидного B24_WEBHOOK "
            "(ожидается URL вида https://<portal>/rest/<id>/<token>/).\n"
        )
        sys.exit(2)
    uid_raw = data.get("B24_USER_ID", "").strip()
    wr_hash = data.get("WORK_REPORTS_HASH", "").strip() or None
    wr_portal = data.get("WORK_REPORTS_PORTAL", "").strip().rstrip("/") or None

    def g(key: str) -> str | None:
        return data.get(key, "").strip() or None

    return Config(
        webhook=webhook.rstrip("/") + "/",
        user_id=int(uid_raw) if uid_raw.isdigit() else None,
        work_reports_hash=wr_hash,
        work_reports_portal=wr_portal,
        oauth_access_token=g("B24_OAUTH_ACCESS_TOKEN"),
        oauth_refresh_token=g("B24_OAUTH_REFRESH_TOKEN"),
        oauth_client_id=g("B24_OAUTH_CLIENT_ID"),
        oauth_client_secret=g("B24_OAUTH_CLIENT_SECRET"),
        oauth_token_url=g("B24_OAUTH_TOKEN_URL"),
        followups_hash=g("FOLLOWUPS_HASH"),
        followups_portal=(g("FOLLOWUPS_PORTAL") or "").rstrip("/") or None,
        followups_action=g("FOLLOWUPS_ACTION") or "getCallFollowups",
        env_path=str(chosen),
    )
