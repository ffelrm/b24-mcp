"""Полученный ответ не всегда подтверждает исход записи. Только фейковая сеть."""
import io
import json
import urllib.error

import pytest

from helpers import CFG

from b24_mcp.client import B24Error, Client


@pytest.fixture
def raw_portal(monkeypatch):
    def install(body, status=200, read_error=None):
        posts = []

        class ErrorBody(io.BytesIO):
            def read(self, *args, **kwargs):
                if read_error is not None:
                    raise read_error
                return super().read(*args, **kwargs)

        def request(req, timeout=None):
            posts.append(req.full_url.rsplit("/", 1)[-1])
            if status != 200:
                raise urllib.error.HTTPError(req.full_url, status, "bad response", {}, ErrorBody(body))
            return io.BytesIO(body)

        monkeypatch.setattr("urllib.request.urlopen", request)
        return posts

    return install


@pytest.mark.parametrize("method", ["im.message.add", "future.method.execute"])
@pytest.mark.parametrize("entry", ["call", "call_list", "call_batch", "raw_batch"])
@pytest.mark.parametrize("body", [
    b"<html>proxy error</html>", b"\xff", b"null", b"[]", b'"ok"', b"true", b"1",
    b"{}", b'{"time": {"duration": 1}}',
])
def test_invalid_write_response_is_unknown_once(raw_portal, method, entry, body):
    posts = raw_portal(body)
    client = Client(CFG, allow_write=True)
    with pytest.raises(B24Error) as error:
        if entry == "call_list":
            list(client.call_list(method))
        elif entry == "call_batch":
            client.call_batch({"safe": ("user.get", {}), "write": (method, {})})
        elif entry == "raw_batch":
            client.call("batch", {"cmd": {"safe": "user.get", "write": method}})
        else:
            client.call(method)
    assert error.value.code == "UNKNOWN_OUTCOME"
    assert posts == [("batch" if "batch" in entry else method) + ".json"]
    # Тело ответа может содержать секреты: их не должно быть в ошибке.
    assert body.decode("utf-8", errors="replace") not in str(error.value)


@pytest.mark.parametrize("method", ["im.message.add", "future.method.execute", "batch"])
@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}"])
def test_invalid_http_error_body_after_write_is_unknown(raw_portal, method, body):
    posts = raw_portal(body, status=400)
    with pytest.raises(B24Error) as error:
        params = {"cmd": {"write": "im.message.add"}} if method == "batch" else None
        Client(CFG, allow_write=True).call(method, params)
    assert error.value.code == "UNKNOWN_OUTCOME"
    assert posts == [method + ".json"]


def test_invalid_http_error_body_for_read_stays_http_error(raw_portal):
    posts = raw_portal(b"not json", status=400)
    with pytest.raises(B24Error) as error:
        Client(CFG).call("user.get")
    assert error.value.code == "HTTP_400"
    assert posts == ["user.get.json"]


@pytest.mark.parametrize("method", ["im.message.add", "future.method.execute", "batch"])
@pytest.mark.parametrize("status", [400, 503])
@pytest.mark.parametrize("read_error", [
    OSError("private response data"), RuntimeError("private response data"),
    B24Error("response.read", "NETWORK", "private response data"),
])
def test_http_error_body_read_failure_after_write_is_unknown_once(
    raw_portal, method, status, read_error,
):
    posts = raw_portal(b"", status=status, read_error=read_error)
    client = Client(CFG, allow_write=True)
    with pytest.raises(B24Error) as error:
        if method == "batch":
            client.call_batch({"safe": ("user.get", {}), "write": ("im.message.add", {})})
        else:
            client.call(method)
    assert error.value.code == "UNKNOWN_OUTCOME"
    assert posts == [method + ".json"]
    assert "private response data" not in str(error.value)


@pytest.mark.parametrize("status, attempts", [(400, 1), (503, 3)])
@pytest.mark.parametrize("read_error", [OSError("broken body"), RuntimeError("broken body")])
def test_http_error_body_read_failure_for_read_keeps_http_error(
    raw_portal, monkeypatch, status, attempts, read_error,
):
    monkeypatch.setattr("b24_mcp.client.time.sleep", lambda _: None)
    posts = raw_portal(b"", status=status, read_error=read_error)
    with pytest.raises(B24Error) as error:
        Client(CFG).call("user.get")
    assert error.value.code == f"HTTP_{status}"
    assert posts == ["user.get.json"] * attempts


@pytest.mark.parametrize("method", ["im.message.add", "future.method.execute", "batch"])
def test_write_http_4xx_with_readable_api_error_stays_known(raw_portal, method):
    posts = raw_portal(b'{"error": "ACCESS_DENIED"}', status=403)
    params = {"cmd": {"write": "im.message.add"}} if method == "batch" else None
    with pytest.raises(B24Error) as error:
        Client(CFG, allow_write=True).call(method, params)
    assert error.value.code == "ACCESS_DENIED"
    assert posts == [method + ".json"]


@pytest.mark.parametrize("body, expected", [(b'{"result": null}', None), (b'{"result": false}', False)])
def test_write_result_can_be_false_or_null(raw_portal, body, expected):
    posts = raw_portal(body)
    assert Client(CFG, allow_write=True).call("im.message.add") is expected
    assert posts == ["im.message.add.json"]


def test_write_api_error_remains_known(raw_portal):
    posts = raw_portal(b'{"error": "ACCESS_DENIED"}')
    with pytest.raises(B24Error) as error:
        Client(CFG, allow_write=True).call("im.message.add")
    assert error.value.code == "ACCESS_DENIED"
    assert posts == ["im.message.add.json"]


@pytest.mark.parametrize("body, exception", [
    (b"not json", json.JSONDecodeError), (b"\xff", UnicodeDecodeError),
    (b"[]", AttributeError), (b"null", TypeError),
])
def test_invalid_read_response_keeps_previous_exception(raw_portal, body, exception):
    posts = raw_portal(body)
    with pytest.raises(exception):
        Client(CFG).call("user.get")
    assert posts == ["user.get.json"]


def test_read_without_result_still_returns_none(raw_portal):
    posts = raw_portal(b"{}")
    assert Client(CFG).call("user.get") is None
    assert posts == ["user.get.json"]
