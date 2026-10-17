#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Maritime adapter for QwenPaw.

Implements the Maritime custom-container contract
(https://maritime.sh/docs/frameworks/custom):

  1. Long-lived server on 0.0.0.0:$PORT   (PORT is injected by Maritime)
  2. GET  /health -> 2xx, fast, no side effects
  3. POST /chat   -> {"message": "...", "source": "..."}
                  <- {"response": "..."} within 30 seconds

The adapter forwards chat to the QwenPaw app, which listens on loopback
only (default http://127.0.0.1:8088), via the background-task API:

  POST /api/console/chat/task        -> {"task_id": ...}
  GET  /api/console/chat/task/{id}   -> {"status": "running"|"finished", ...}

Configuration (environment variables):
  PORT                     injected by Maritime; listen port (default 8080)
  QWENPAW_BASE_URL         QwenPaw app base URL (default http://127.0.0.1:8088)
  ADAPTER_DEADLINE_SECONDS reply deadline for POST /chat (default 27; the
                           Maritime hard limit is 30s)
  QWENPAW_TASK_TIMEOUT     server-side task timeout passed to QwenPaw,
                           in seconds (default 900)
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

QWENPAW_BASE = os.environ.get("QWENPAW_BASE_URL", "http://127.0.0.1:8088")
PORT = int(os.environ.get("PORT", "8080"))
DEADLINE = float(os.environ.get("ADAPTER_DEADLINE_SECONDS", "27"))
TASK_TIMEOUT = int(os.environ.get("QWENPAW_TASK_TIMEOUT", "900"))
POLL_INTERVAL = 1.0

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# One in-flight chat per session: QwenPaw rejects a second run for the
# same chat with HTTP 409, so we fail fast instead of queueing.
_session_locks: dict[str, asyncio.Lock] = {}


@app.get("/health")
async def health() -> JSONResponse:
    """Proxy the QwenPaw readiness probe, fast and side-effect free."""
    try:
        async with httpx.AsyncClient(
            base_url=QWENPAW_BASE,
            timeout=2.0,
        ) as client:
            resp = await client.get("/api/healthz")
        if resp.status_code == 200:
            return JSONResponse({"status": "ok"})
    except Exception:  # noqa: BLE001 - any failure means "not ready"
        pass
    return JSONResponse({"status": "starting"}, status_code=503)


def _extract_text(result: dict[str, Any]) -> str:
    """Pull the assistant text out of a completed console-task result."""
    output = result.get("output") or []
    if not isinstance(output, list):
        return ""
    for message in reversed(output):
        if not isinstance(message, dict):
            continue
        content = message.get("content") or []
        if not isinstance(content, list):
            continue
        texts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        text = "\n".join(t for t in texts if t).strip()
        if text:
            return text
    return ""


async def _submit_task(client: httpx.AsyncClient, message: str, session_id: str) -> str:
    """Submit a chat task; returns task_id. Raises on HTTP errors."""
    payload = {
        "channel": "console",
        "user_id": "maritime",
        "session_id": session_id,
        "input": [
            {
                "role": "user",
                "type": "message",
                "content": [{"type": "text", "text": message}],
            },
        ],
        "timeout": TASK_TIMEOUT,
    }
    resp = await client.post("/api/console/chat/task", json=payload, timeout=15)
    resp.raise_for_status()
    return str(resp.json()["task_id"])


async def _run_chat(message: str, session_id: str) -> str:
    deadline = time.monotonic() + DEADLINE
    async with httpx.AsyncClient(base_url=QWENPAW_BASE) as client:
        try:
            task_id = await _submit_task(client, message, session_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                return "⏳ 上一条消息还在处理中，请稍等片刻再发。"
            if exc.response.status_code in (502, 503):
                return "⚠️ QwenPaw 还在启动中，请几秒钟后再试。"
            return f"⚠️ 消息提交失败 (HTTP {exc.response.status_code})。"
        except httpx.HTTPError:
            return "⚠️ 无法连接到 QwenPaw 服务，请稍后再试。"

        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                resp = await client.get(
                    f"/api/console/chat/task/{task_id}",
                    timeout=10,
                )
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            data = resp.json()
            if data.get("status") != "finished":
                continue
            result = data.get("result") or {}
            if result.get("status") == "completed":
                return _extract_text(result) or "(空回复)"
            error = (result.get("error") or {}).get("message", "未知错误")
            return f"⚠️ 处理失败:{error}"

    return (
        "⏳ 这条消息的处理时间较长,暂未完成。"
        "任务仍在后台运行,可以发条新消息继续对话。"
    )


@app.post("/chat")
async def chat(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    message = str(body.get("message") or "").strip()
    source = str(body.get("source") or "front_door")
    if not message:
        return JSONResponse({"response": "(空消息)"})

    # Per-source session: each Maritime channel keeps its own conversation.
    session_id = f"maritime-{source}"
    lock = _session_locks.setdefault(session_id, asyncio.Lock())
    if lock.locked():
        return JSONResponse(
            {"response": "⏳ 上一条消息还在处理中,请稍等片刻再发。"},
        )
    async with lock:
        return JSONResponse({"response": await _run_chat(message, session_id)})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
