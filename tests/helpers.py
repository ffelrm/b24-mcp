"""Общие заглушки тестов: конфиг без секретов и фейковый портал вместо сети."""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from b24_mcp.config import Config  # noqa: E402

# Портал в зоне .invalid: настоящий запрос по такому адресу никуда не уйдёт.
CFG = Config(webhook="https://portal.invalid/rest/1/test/", user_id=1)


class FakePortal:
    """Подмена `urllib.request.urlopen`: сценарий исходов по методам + журнал POST.

    `script[method]` — очередь исходов метода: исключение бросается, словарь
    уходит JSON-ом как ответ портала. POST сверх сценария — провал теста:
    именно так выглядел бы лишний повтор.
    """

    def __init__(self, script: dict[str, list]):
        self.script = {m: list(q) for m, q in script.items()}
        self.posts: list[str] = []
        self.bodies: list[dict[str, list[str]]] = []
        self.timeouts: dict[str, float | None] = {}

    def __call__(self, req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1].removesuffix(".json")
        self.posts.append(method)
        self.bodies.append(parse_qs((req.data or b"").decode("utf-8")))
        self.timeouts[method] = timeout
        queue = self.script.get(method) or []
        if not queue:
            raise AssertionError(f"лишний POST {method}: сценарий исчерпан")
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return io.BytesIO(json.dumps(outcome).encode("utf-8"))


def http_error(code: int, body: dict | None = None) -> urllib.error.HTTPError:
    fp = io.BytesIO(json.dumps(body).encode("utf-8")) if body else io.BytesIO(b"")
    return urllib.error.HTTPError("https://portal.invalid/", code, "err", {}, fp)
