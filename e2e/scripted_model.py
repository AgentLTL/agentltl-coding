#!/usr/bin/env python3
"""
A scripted chat model for testing a harness end to end, without a real model or account.

    python3 scripted_model.py SCRIPT.json [--port 8999] [--log DIR]

It serves the three wire formats coding agents use:

- OpenAI responses (``POST /v1/responses``): Codex CLI with a custom provider
  (``wire_api = "responses"``). A call ``{"name": "apply_patch", "input": "*** Begin Patch..."}``
  (``input`` instead of ``args``) is a freeform tool call (``custom_tool_call``); a call's
  ``namespace`` (Codex's ``multi_agent_v1``) is passed on.
- OpenAI chat completions (``POST /v1/chat/completions``, ``GET /v1/models``): Mistral Vibe,
  GitHub Copilot CLI with a custom provider (``COPILOT_PROVIDER_BASE_URL``)...
- Anthropic messages (``POST /v1/messages``, ``/v1/messages/count_tokens``): Claude Code with
  ``ANTHROPIC_BASE_URL``.

Both streaming and not. SCRIPT is a JSON list of steps; the n-th request of a conversation
(n = the number of assistant turns it already holds) gets step n, the last step repeating:

    [{"calls": [{"name": "bash", "args": {"command": "ls"}}]},   # tool calls
     {"text": "Done."}]                                         # a final answer

A conversation whose first user message contains ``CHILD`` (a subagent) gets the script's
``"child"`` steps instead, when SCRIPT is an object ``{"steps": [...], "child": [...]}``. A
request without tools (a title, a summary) gets a short text. Each request is saved to the
log directory as ``req-<time>.json``.

Only the standard library: it runs in any image with Python 3.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

USAGE_OPENAI = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
USAGE_ANTHROPIC = {"input_tokens": 10, "output_tokens": 5}


def load(path: str) -> Dict[str, List[Dict[str, Any]]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return {"steps": data, "child": [{"text": "Done."}]}
    return {"steps": data["steps"], "child": data.get("child") or [{"text": "Done."}]}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def step_for(script: Dict[str, List[Dict[str, Any]]], messages: List[Dict[str, Any]]
             ) -> Dict[str, Any]:
    n = sum(1 for m in messages if m.get("role") == "assistant")
    users = [m for m in messages if m.get("role") == "user"]
    steps = script["child"] if users and "CHILD" in _text_of(users[0].get("content")) \
        else script["steps"]
    return steps[min(n, len(steps) - 1)]


def responses_messages(items: List[Any]) -> List[Dict[str, Any]]:
    """A responses ``input`` as chat messages for :func:`step_for`: each model turn (an
    assistant message, or the tool calls of one step, whose ids carry the step number)
    becomes one assistant message."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        kind = it.get("type", "message")
        if kind == "message" and it.get("role") in ("user", "assistant"):
            out.append({"role": it["role"], "content": it.get("content")})
        elif kind in ("function_call", "custom_tool_call"):
            tag = str(it.get("call_id", "")).split("_")[:2]
            key = tag[1] if len(tag) == 2 and tag[0] == "call" else it.get("call_id")
            if key not in seen:
                seen.add(key)
                out.append({"role": "assistant", "content": ""})
    users = [m for m in out if m["role"] == "user" and "CHILD" in _text_of(m["content"])]
    if users:  # the subagent's task may follow context messages: put it first
        out = [users[0]] + [m for m in out if m is not users[0]]
    return out


class Handler(BaseHTTPRequestHandler):
    script: Dict[str, List[Dict[str, Any]]] = {}
    log_dir = ""

    def log_message(self, *args: Any) -> None:
        pass

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _sse(self, data: Any, event: str = "") -> None:
        if event:
            self.wfile.write(f"event: {event}\n".encode())
        self.wfile.write(b"data: " + (data if isinstance(data, bytes) else json.dumps(data).encode())
                         + b"\n\n")
        self.wfile.flush()

    def do_GET(self) -> None:
        self._json({"object": "list", "data": [{"id": "scripted", "object": "model",
                                                "created": 0, "owned_by": "agentltl"}]})

    def do_POST(self) -> None:
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.log_dir:
            with open(os.path.join(self.log_dir, f"req-{time.time_ns()}.json"), "w") as fh:
                json.dump({"path": self.path, "body": req}, fh, indent=1)
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/messages/count_tokens"):
            return self._json({"input_tokens": 10})
        if path.endswith("/messages"):
            return self._anthropic(req)
        if path.endswith("/responses"):
            return self._responses(req)
        return self._openai(req)

    def _step(self, req: Dict[str, Any]) -> Dict[str, Any]:
        if not req.get("tools"):
            return {"text": "ok"}
        return step_for(self.script, req.get("messages") or [])

    # ── OpenAI chat completions ───────────────────────────────────────────────

    def _openai(self, req: Dict[str, Any]) -> None:
        step = self._step(req)
        calls = [{"index": i, "id": "call_" + uuid.uuid4().hex[:12], "type": "function",
                  "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                 for i, c in enumerate(step.get("calls") or [])]
        text = step.get("text") or ""
        finish = "tool_calls" if calls else "stop"
        base = {"id": "chatcmpl-" + uuid.uuid4().hex[:12], "created": int(time.time()),
                "model": req.get("model") or "scripted"}
        if not req.get("stream"):
            msg: Dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = [{k: v for k, v in c.items() if k != "index"} for c in calls]
            return self._json({**base, "object": "chat.completion", "usage": USAGE_OPENAI,
                               "choices": [{"index": 0, "message": msg,
                                            "finish_reason": finish}]})
        self._sse_start()
        chunk = {**base, "object": "chat.completion.chunk"}
        delta: Dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            delta["tool_calls"] = calls
        self._sse({**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        self._sse({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                   "usage": USAGE_OPENAI})
        self._sse(b"[DONE]")

    # ── OpenAI responses ──────────────────────────────────────────────────────

    def _responses(self, req: Dict[str, Any]) -> None:
        items = req.get("input") or []
        if isinstance(items, str):
            items = [{"type": "message", "role": "user", "content": items}]
        if not req.get("tools"):
            step: Dict[str, Any] = {"text": "ok"}
        else:
            step = step_for(self.script, responses_messages(items))
        n = sum(1 for m in responses_messages(items) if m["role"] == "assistant")
        out: List[Dict[str, Any]] = []
        for c in step.get("calls") or []:
            call_id = f"call_{n}_{uuid.uuid4().hex[:8]}"
            if "input" in c:
                item = {"type": "custom_tool_call", "id": "ctc_" + uuid.uuid4().hex[:12],
                        "status": "completed", "call_id": call_id, "name": c["name"],
                        "input": c["input"]}
            else:
                item = {"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:12],
                        "status": "completed", "call_id": call_id, "name": c["name"],
                        "arguments": json.dumps(c["args"])}
            if c.get("namespace"):
                item["namespace"] = c["namespace"]
            out.append(item)
        if not out:  # a step's text counts only when it makes no calls: one turn, one marker
            out.append({"type": "message", "id": "msg_" + uuid.uuid4().hex[:12],
                        "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": step.get("text") or "ok",
                                     "annotations": []}]})
        rid = "resp_" + uuid.uuid4().hex[:12]
        usage = {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 0},
                 "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 0},
                 "total_tokens": 15}
        response = {"id": rid, "object": "response", "created_at": int(time.time()),
                    "model": req.get("model") or "scripted", "status": "completed",
                    "output": out, "usage": usage}
        if not req.get("stream"):
            return self._json(response)
        self._sse_start()
        self._sse({"type": "response.created",
                   "response": {**response, "status": "in_progress", "output": []}},
                  "response.created")
        for i, item in enumerate(out):
            self._sse({"type": "response.output_item.added", "output_index": i, "item": item},
                      "response.output_item.added")
            if item["type"] == "message":
                self._sse({"type": "response.output_text.delta", "output_index": i,
                           "content_index": 0, "delta": item["content"][0]["text"]},
                          "response.output_text.delta")
            self._sse({"type": "response.output_item.done", "output_index": i, "item": item},
                      "response.output_item.done")
        self._sse({"type": "response.completed", "response": response}, "response.completed")

    # ── Anthropic messages ────────────────────────────────────────────────────

    def _anthropic(self, req: Dict[str, Any]) -> None:
        step = self._step(req)
        blocks: List[Dict[str, Any]] = []
        if step.get("text"):
            blocks.append({"type": "text", "text": step["text"]})
        for c in step.get("calls") or []:
            blocks.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20],
                           "name": c["name"], "input": c["args"]})
        if not blocks:
            blocks.append({"type": "text", "text": "ok"})
        stop = "tool_use" if step.get("calls") else "end_turn"
        message = {"id": "msg_" + uuid.uuid4().hex[:20], "type": "message", "role": "assistant",
                   "model": req.get("model") or "scripted", "stop_sequence": None}
        if not req.get("stream"):
            return self._json({**message, "content": blocks, "stop_reason": stop,
                               "usage": USAGE_ANTHROPIC})
        self._sse_start()
        self._sse({"type": "message_start", "message": {**message, "content": [],
                   "stop_reason": None, "usage": {**USAGE_ANTHROPIC, "output_tokens": 0}}},
                  "message_start")
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                self._sse({"type": "content_block_start", "index": i,
                           "content_block": {"type": "text", "text": ""}}, "content_block_start")
                self._sse({"type": "content_block_delta", "index": i,
                           "delta": {"type": "text_delta", "text": b["text"]}},
                          "content_block_delta")
            else:
                self._sse({"type": "content_block_start", "index": i,
                           "content_block": {**b, "input": {}}}, "content_block_start")
                self._sse({"type": "content_block_delta", "index": i,
                           "delta": {"type": "input_json_delta",
                                     "partial_json": json.dumps(b["input"])}},
                          "content_block_delta")
            self._sse({"type": "content_block_stop", "index": i}, "content_block_stop")
        self._sse({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                   "usage": {"output_tokens": 5}}, "message_delta")
        self._sse({"type": "message_stop"}, "message_stop")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    p.add_argument("script")
    p.add_argument("--port", type=int, default=8999)
    p.add_argument("--log", default=os.environ.get("LOG_DIR", ""))
    args = p.parse_args()
    Handler.script = load(args.script)
    Handler.log_dir = args.log
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
