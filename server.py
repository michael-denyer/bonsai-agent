#!/usr/bin/env python3
"""Serve the local Bonsai pack over an OpenAI-shaped HTTP endpoint on loopback."""

import argparse
import json
import os
import queue
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple

BONSAI_HOME = Path(os.environ.get("BONSAI_HOME") or Path.home() / ".local/share/bonsai")
DEFAULT_MODEL_DIR = BONSAI_HOME / "model"
DEFAULT_PORT = int(os.environ.get("BONSAI_PORT") or 8091)
DEFAULT_IDLE_TIMEOUT = 1800.0
DEFAULT_MAX_TOKENS = 4096

EFFORTS = ("xhigh", "medium", "low")
SAMPLING = {
    True: {"temperature": 1.0, "top_p": 0.95},
    False: {"temperature": 0.7, "top_p": 0.8},
}

JOBS = queue.SimpleQueue()
MODEL_ID = None

ACTIVITY_LOCK = threading.Lock()
LAST_ACTIVITY = time.monotonic()
IN_FLIGHT = 0


class Outcome(NamedTuple):
    """One generation's answer, thinking text, and resolved finish reason."""

    content: str
    reasoning_content: str
    finish_reason: str


def split_outcome(text, enable_thinking, finish_reason):
    """Split raw generated text into answer and thinking text.

    Args:
        text: Raw model output. It never holds an opening ``<think>`` tag, because
            the generation prompt already emitted one: bare when thinking is on,
            immediately closed and empty when thinking is off.
        enable_thinking: Whether the request asked for thinking.
        finish_reason: The finish reason the generation call reported.

    Returns:
        Outcome. With thinking on, a missing ``</think>`` means the token budget ran
        out mid-thought, so there is no answer to return and the stop is a length
        stop whatever the generation call called it.
    """
    if not enable_thinking:
        return Outcome(text.strip(), "", finish_reason)
    if "</think>" not in text:
        return Outcome("", text, "length")
    reasoning, content = text.split("</think>", 1)
    return Outcome(content.strip(), reasoning.strip(), finish_reason)


def request_error(payload):
    """Return the first reason this request body is unusable, or None when it is fine.

    Every guard for client input lives here so the generation path can trust its
    argument. A bad reasoning_effort in particular reaches the Jinja template and
    raises there, which would otherwise surface as a 500 for a client mistake.
    """
    if not isinstance(payload, dict):
        return "body must be a JSON object"
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return "body needs a non-empty messages list"
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            return "every message needs a string role"
        if not isinstance(message.get("content"), str):
            return "every message needs string content, and images are not supported"
    effort = payload.get("reasoning_effort", "xhigh")
    if effort not in EFFORTS:
        return f"reasoning_effort must be one of {', '.join(EFFORTS)}, got {effort!r}"
    return None


def begin_request():
    global IN_FLIGHT
    with ACTIVITY_LOCK:
        IN_FLIGHT += 1


def end_request():
    global IN_FLIGHT, LAST_ACTIVITY
    with ACTIVITY_LOCK:
        IN_FLIGHT -= 1
        LAST_ACTIVITY = time.monotonic()


def watch_idle(timeout):
    """Exit once the server has been idle past timeout with no request in flight."""
    while True:
        time.sleep(1.0)
        with ACTIVITY_LOCK:
            idle = time.monotonic() - LAST_ACTIVITY
            busy = IN_FLIGHT
        if not busy and idle >= timeout:
            print(f"exiting after {idle:.0f}s idle", file=sys.stderr, flush=True)
            os._exit(0)


def generation(generate, model, processor, payload):
    """Run one validated request and return its OpenAI-shaped response body."""
    enable_thinking = bool(payload.get("enable_thinking", True))
    defaults = SAMPLING[enable_thinking]
    prompt = processor.tokenizer.apply_chat_template(
        payload["messages"],
        add_generation_prompt=True,
        tokenize=False,
        enable_thinking=enable_thinking,
        reasoning_effort=payload.get("reasoning_effort", "xhigh"),
    )
    result = generate(
        model,
        processor,
        prompt,
        max_tokens=int(payload.get("max_tokens", DEFAULT_MAX_TOKENS)),
        temperature=float(payload.get("temperature", defaults["temperature"])),
        top_p=float(payload.get("top_p", defaults["top_p"])),
        verbose=False,
    )
    outcome = split_outcome(result.text, enable_thinking, result.finish_reason)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": outcome.content,
                    "reasoning_content": outcome.reasoning_content,
                },
                "finish_reason": outcome.finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.generation_tokens,
            "total_tokens": result.total_tokens,
            "tokens_per_second": result.generation_tps,
        },
    }


def worker(model_dir, ready):
    """Load the model and run every generation on this one thread.

    This MLX build ties streams to the thread that creates them. Generating from a
    fresh thread per request, which is what ThreadingHTTPServer hands out, segfaults
    the process once a third thread joins in. Serving every job from the thread that
    loaded the weights avoids that, and the queue makes one-at-a-time generation a
    property of the design rather than a lock to remember.

    Args:
        model_dir: Pack directory holding runtime/ and the weights.
        ready: Queue receiving the model id once loaded, or the load exception.
    """
    try:
        sys.path.insert(0, str(model_dir / "runtime"))
        from vision_artifact import load_vl_model

        model, processor, config = load_vl_model(str(model_dir))
        from mlx_vlm import generate
    except BaseException as error:
        ready.put(error)
        return
    ready.put(config["model_type"])
    while True:
        payload, reply = JOBS.get()
        try:
            reply.put(generation(generate, model, processor, payload))
        except Exception as error:
            traceback.print_exc()
            reply.put(error)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, code, body):
        encoded = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path != "/health":
            self.send_json(404, {"error": f"no route for GET {self.path}"})
            return
        self.send_json(200, {"status": "ok", "model": MODEL_ID})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_json(404, {"error": f"no route for POST {self.path}"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as error:
            self.send_json(400, {"error": f"body is not JSON: {error}"})
            return
        error = request_error(payload)
        if error:
            self.send_json(400, {"error": error})
            return
        begin_request()
        try:
            reply = queue.SimpleQueue()
            JOBS.put((payload, reply))
            result = reply.get()
            if isinstance(result, BaseException):
                self.send_json(500, {"error": f"generation failed: {result}"})
            else:
                self.send_json(200, result)
        finally:
            end_request()


def main():
    global MODEL_ID, LAST_ACTIVITY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT)
    args = parser.parse_args()

    if not (args.model_dir / "runtime").is_dir():
        parser.error(f"--model-dir has no runtime/ directory: {args.model_dir}")

    ready = queue.SimpleQueue()
    threading.Thread(target=worker, args=(args.model_dir, ready), daemon=True).start()
    loaded = ready.get()
    if isinstance(loaded, BaseException):
        raise loaded
    MODEL_ID = loaded

    LAST_ACTIVITY = time.monotonic()
    threading.Thread(target=watch_idle, args=(args.idle_timeout,), daemon=True).start()

    # Binding after the load is what makes a healthy /health mean a loaded model.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(
        f"serving {MODEL_ID} on 127.0.0.1:{args.port},"
        f" idle timeout {args.idle_timeout:.0f}s",
        file=sys.stderr,
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
