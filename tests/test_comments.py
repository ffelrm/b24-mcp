"""Чистка сообщений и результаты задачи. Без сети."""
import pytest

from b24_mcp import api
from b24_mcp.client import B24Error
from b24_mcp.commands._comments import clean_bb, fetch_task_results

SEP = "-" * 30


def _quote(header: str, body: str) -> str:
    return f"{SEP}\n{header}\n{body}\n{SEP}"


THREAD = "\n".join([
    _quote("Первый Автор [01.01.2025 10:00] #chat12/34", "первая цитата"),
    "ответ один",
    _quote("Второй Автор [01.01.2025 11:00] #chat12/35", "вторая цитата"),
    "ответ два",
])


def test_reply_between_quotes_survives():
    """Ответ между двумя цитатами — не цитата: терять его нельзя."""
    out = clean_bb(THREAD)
    assert "ответ один" in out and "ответ два" in out
    assert "первая цитата" not in out and "вторая цитата" not in out
    assert out.count("「цитата」") == 2


def test_drop_quotes_leaves_only_replies():
    out = clean_bb(THREAD, drop_quotes=True)
    assert "цитата" not in out
    assert "ответ один" in out and "ответ два" in out


def test_separator_without_anchor_is_not_a_quote():
    """Строка из дефисов без якоря #chat — просто разделитель в тексте."""
    out = clean_bb(f"до\n{SEP}\nпосле")
    assert "до" in out and "после" in out and "цитата" not in out


class _Results:
    """Заглушка клиента: tasks.task.result.list отвечает заданным исходом."""

    def __init__(self, outcome):
        self.outcome = outcome

    def call(self, method, params=None):
        assert method == "tasks.task.result.list"
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_results_missing_method_is_empty():
    err = B24Error("tasks.task.result.list", "ERROR_METHOD_NOT_FOUND", "нет метода")
    assert fetch_task_results(_Results(err), 1) == []


@pytest.mark.parametrize("code", ["ACCESS_DENIED", "NETWORK", "TIMEOUT"])
def test_results_fatal_error_is_not_swallowed(code):
    """Отказ доступа или сети — не «результатов нет»: пустой ответ соврал бы."""
    with pytest.raises(B24Error) as error:
        fetch_task_results(_Results(B24Error("tasks.task.result.list", code, "")), 1)
    assert error.value.code == code


def test_task_result_tool_surfaces_fatal_error():
    class Client:
        user_id = 1

        def call(self, method, params=None):
            if method == "tasks.task.get":
                return {"task": {"id": "1", "title": "Задача"}}
            raise B24Error(method, "ACCESS_DENIED", "нет доступа")

    with pytest.raises(B24Error):
        api.task_result(Client(), 1)
