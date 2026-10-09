"""e2e/scripted_model.py: the steps come back in both wire formats, streamed or not."""

import importlib.util
import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "e2e", "scripted_model.py")
_spec = importlib.util.spec_from_file_location("scripted_model", _PATH)
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)

TOOLS = [{"name": "bash"}]


@pytest.fixture
def url(tmp_path):
    script = tmp_path / "s.json"
    script.write_text(json.dumps({"steps": [{"calls": [{"name": "bash", "args": {"command": "ls"}}]},
                                            {"text": "Done."}],
                                  "child": [{"text": "child done"}]}))
    sm.Handler.script = sm.load(str(script))
    sm.Handler.log_dir = str(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), sm.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def post(url, path, body):
    req = urllib.request.Request(url + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.read().decode()


def events(raw):
    return [json.loads(line[6:]) for line in raw.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


def test_openai(url):
    first = json.loads(post(url, "/v1/chat/completions", {"tools": TOOLS, "messages": [
        {"role": "user", "content": "go"}]}))
    call = first["choices"][0]["message"]["tool_calls"][0]["function"]
    assert (call["name"], json.loads(call["arguments"])) == ("bash", {"command": "ls"})
    assert first["choices"][0]["finish_reason"] == "tool_calls"
    later = events(post(url, "/v1/chat/completions", {"stream": True, "tools": TOOLS, "messages": [
        {"role": "user", "content": "go"}, {"role": "assistant", "content": None},
        {"role": "tool", "content": "x"}]}))
    assert later[0]["choices"][0]["delta"]["content"] == "Done."
    assert later[-1]["choices"][0]["finish_reason"] == "stop"


def test_anthropic(url):
    first = events(post(url, "/v1/messages", {"stream": True, "tools": TOOLS, "messages": [
        {"role": "user", "content": [{"type": "text", "text": "go"}]}]}))
    kinds = [e["type"] for e in first]
    assert kinds[0] == "message_start" and kinds[-1] == "message_stop"
    start = next(e for e in first if e["type"] == "content_block_start")
    delta = next(e for e in first if e["type"] == "content_block_delta")
    assert start["content_block"]["name"] == "bash"
    assert json.loads(delta["delta"]["partial_json"]) == {"command": "ls"}
    assert next(e for e in first if e["type"] == "message_delta")["delta"]["stop_reason"] == "tool_use"
    child = json.loads(post(url, "/v1/messages", {"tools": TOOLS, "messages": [
        {"role": "user", "content": "CHILD: look"}]}))
    assert child["content"] == [{"type": "text", "text": "child done"}]
    assert json.loads(post(url, "/v1/messages/count_tokens", {}))["input_tokens"] == 10


def test_a_request_without_tools_gets_text(url):
    out = json.loads(post(url, "/v1/messages", {"messages": [{"role": "user", "content": "title?"}]}))
    assert out["stop_reason"] == "end_turn"
