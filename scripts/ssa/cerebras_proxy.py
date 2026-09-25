#!/usr/bin/env python3
"""Local reverse proxy that keeps an API key out of an opencode worker.

Cerebras' chat completions API returns `reasoning` on gpt-oss / qwen and then
rejects the same field when a client echoes it back inside the assistant
history (400: `messages.N.assistant.reasoning_content ... is unsupported`).
Every AI-SDK based CLI (opencode among them) echoes it, so the second turn of
any agentic run dies. This proxy sits on 127.0.0.1, strips the echoed
reasoning fields from assistant messages, injects the API key so the worker
process never holds it, and streams the upstream response back unchanged.

    python3 cerebras_proxy.py --port-file /path/port [--upstream URL] [--key-file PATH]
                              [--key-name NAME] [--no-strip] [--label TAG]

The defaults are the Cerebras profile. DeepSeek runs through the same proxy
with --key-name DEEPSEEK_API_KEY --no-strip: DeepSeek wants the echoed
reasoning_content back inside a tool-call turn, so stripping it would break
the run, while the key injection is exactly what the worker needs.

It binds an ephemeral port, writes the port number to --port-file, and serves
until it is killed. No request or response body is ever logged.

Stdlib only. `rewrite_body()` is pure and unit-tested.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_UPSTREAM = "https://api.cerebras.ai"
DEFAULT_KEY_FILE = "~/.config/cerebras/env"
KEY_NAME = "CEREBRAS_API_KEY"
# Fields Cerebras refuses in assistant history. `reasoning` covers a client
# that echoes the response field verbatim; `reasoning_content` is what the
# AI SDK emits.
STRIP_FIELDS = ("reasoning_content", "reasoning")
# Hop-by-hop and framing headers this proxy owns itself.
_SKIP_REQUEST_HEADERS = {
    "host",
    "content-length",
    "connection",
    "accept-encoding",
    "authorization",
    "transfer-encoding",
}
_SKIP_RESPONSE_HEADERS = {"transfer-encoding", "connection", "content-length"}


def rewrite_body(raw: bytes, strip: tuple = STRIP_FIELDS) -> bytes:
    """Drop echoed reasoning fields from assistant messages. Pure.

    Anything that is not a JSON object with a `messages` list passes through
    untouched, byte for byte, so a malformed body reaches upstream and gets
    upstream's error instead of this proxy's.
    """
    if not strip:
        return raw
    try:
        doc = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(doc, dict):
        return raw
    messages = doc.get("messages")
    if not isinstance(messages, list):
        return raw
    changed = False
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for field in strip:
            if field in msg:
                del msg[field]
                changed = True
    if not changed:
        return raw
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def read_key(key_file: str, key_name: str = KEY_NAME) -> str:
    """The API key: environment first, then KEY=value lines in key_file."""
    env_value = os.environ.get(key_name, "").strip()
    if env_value:
        return env_value
    path = Path(os.path.expanduser(key_file))
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith(key_name + "="):
                return line[len(key_name) + 1 :].strip().strip("'\"")
    except OSError:
        pass
    return ""


def make_handler(upstream: str, key: str, strip: tuple = STRIP_FIELDS, label: str = "cerebras-proxy"):
    parts = urlsplit(upstream)
    host = parts.hostname or ""
    port = parts.port
    secure = parts.scheme == "https"
    prefix = parts.path.rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "cerebras-proxy/1"

        def log_message(self, fmt, *args):  # quiet: one line per request below
            pass

        def _forward(self):
            started = time.time()
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if self.command in ("POST", "PUT", "PATCH"):
                body = rewrite_body(body, strip)
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in _SKIP_REQUEST_HEADERS
            }
            headers["Host"] = host
            headers["Authorization"] = "Bearer " + key
            headers["Content-Length"] = str(len(body))
            conn_cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection
            conn = conn_cls(host, port, timeout=600)
            status = 502
            try:
                conn.request(self.command, prefix + self.path, body=body, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                self.send_response(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    if k.lower() in _SKIP_RESPONSE_HEADERS:
                        continue
                    self.send_header(k, v)
                # Close-delimited body: simplest framing that streams SSE
                # chunk by chunk without re-chunking it ourselves.
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                self.close_connection = True
            except Exception as exc:  # upstream unreachable, reset, timeout
                try:
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(
                        json.dumps({"error": {"message": "%s: %s" % (label, exc)}}).encode()
                    )
                except Exception:
                    pass
                self.close_connection = True
            finally:
                conn.close()
            sys.stderr.write(
                "%s %s %s -> %s %.2fs\n"
                % (label, self.command, self.path, status, time.time() - started)
            )

        def do_GET(self):
            if self.path == "/health":
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self._forward()

        do_POST = _forward
        do_PUT = _forward
        do_PATCH = _forward
        do_DELETE = _forward

    return Handler


def serve(
    port_file: str,
    upstream: str,
    key_file: str,
    key_name: str = KEY_NAME,
    strip: tuple = STRIP_FIELDS,
    label: str = "cerebras-proxy",
) -> int:
    key = read_key(key_file, key_name)
    if not key:
        sys.stderr.write("%s: no %s in the environment or %s\n" % (label, key_name, key_file))
        return 2
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(upstream, key, strip, label))
    server.daemon_threads = True
    bound = server.server_address[1]
    tmp = port_file + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("%d\n" % bound)
    os.replace(tmp, port_file)

    def _stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever(poll_interval=0.2)
    except SystemExit:
        pass
    finally:
        server.server_close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port-file", required=True, help="file that receives the bound port")
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    ap.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    ap.add_argument("--key-name", default=KEY_NAME, help="env var / file key holding the API key")
    ap.add_argument(
        "--no-strip", action="store_true", help="forward echoed reasoning fields untouched"
    )
    ap.add_argument("--label", default="cerebras-proxy", help="prefix of the per-request log line")
    args = ap.parse_args(argv)
    strip = () if args.no_strip else STRIP_FIELDS
    return serve(args.port_file, args.upstream, args.key_file, args.key_name, strip, args.label)


if __name__ == "__main__":
    sys.exit(main())
