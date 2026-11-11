"""End to end on the OpenAI chat-completions wire, through Hermes's own AIAgent and the OpenAI SDK.

A local HTTP server stands in for the model. It records each request body and answers "ok". The agent runs one
turn without the plugin, then one turn after dials.install(). The check: Hermes alone sends no temperature, and
with the plugin the HTTP body holds each option of the model (top_k through extra_body). It uses a scratch
HERMES_HOME and never touches a real profile. tests/hermes_integration.py runs it (wire_check); alone:

  HERMES_PY=<the python of the Hermes venv>
  $HERMES_PY tests/openai_wire_e2e.py
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = tempfile.mkdtemp(prefix="wire-e2e-")
os.environ["HERMES_HOME"] = HOME  # before Hermes reads it
os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")
sys.path.insert(0, REPO)

MODEL = "e2e-open-model"
OPTIONS = {"temperature": 0.42, "top_p": 0.9, "seed": 7, "top_k": 20}
BODIES = []


class Model(http.server.BaseHTTPRequestHandler):
    def _send(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # a model list, for Hermes's probes
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, "application/json", json.dumps(
                {"object": "list", "data": [{"id": MODEL, "object": "model", "context_length": 131072}]}).encode())
        else:
            self._send(404, "application/json", b"{}")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        BODIES.append(body)
        usage = {"prompt_tokens": 50, "completion_tokens": 1, "total_tokens": 51}
        head = {"id": "c1", "created": 0, "model": MODEL}
        if body.get("stream"):
            chunks = [dict(head, object="chat.completion.chunk", choices=[
                          {"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]),
                      dict(head, object="chat.completion.chunk", choices=[
                          {"index": 0, "delta": {}, "finish_reason": "stop"}], usage=usage)]
            data = "".join("data: %s\n\n" % json.dumps(c) for c in chunks) + "data: [DONE]\n\n"
            self._send(200, "text/event-stream", data.encode())
        else:
            self._send(200, "application/json", json.dumps(dict(head, object="chat.completion", usage=usage, choices=[
                {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}])).encode())

    def log_message(self, format, *args):  # noqa: A002 - the base class names it so
        pass


def turn(port):
    from run_agent import AIAgent
    agent = AIAgent(base_url="http://127.0.0.1:%d/v1" % port, api_key="e2e", provider="custom", model=MODEL,
                    quiet_mode=True, skip_memory=True, skip_context_files=True, max_iterations=2,
                    enabled_toolsets=[], save_trajectories=False)
    n = len(BODIES)
    reply = agent.chat("Say ok.")
    sent = [b for b in BODIES[n:] if "messages" in b]
    if not sent:
        raise SystemExit("FAIL: the agent sent no chat-completions request")
    return reply, sent[-1]


def main():
    from breadcrumbs import dials
    root = os.path.join(HOME, "breadcrumbs")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"models": {"e2e-*": OPTIONS}}, f)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        reply, before = turn(port)
        print("Hermes alone   reply=%r  sampling keys in the HTTP body: %s" % (
            reply, {k: before[k] for k in OPTIONS if k in before} or "none"))
        if any(k in before for k in OPTIONS):
            raise SystemExit("FAIL: Hermes alone sent an option; this check assumes it sends none")
        if not dials.install(lambda: HOME):
            raise SystemExit("FAIL: dials.install found no ChatCompletionsTransport")
        reply, after = turn(port)
        got = {k: after.get(k) for k in OPTIONS}
        print("with the plugin reply=%r  sampling keys in the HTTP body: %s" % (reply, got))
        if got != OPTIONS:
            raise SystemExit("FAIL: the body has %s, the options are %s" % (got, OPTIONS))
        print("OK: each option of %s reached the wire (stream=%s)" % (MODEL, after.get("stream")))
    finally:
        server.shutdown()
        shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
