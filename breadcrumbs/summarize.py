"""Summaries: heuristics choose what to summarize; a small local model writes the text.

Heuristics first:
  - only a "sess" or "span" crumb with more than MIN_BYTES of text gets a summary
    (a smaller crumb shows verbatim at any level for less than a summary costs);
  - a full span never changes (its id is its content), so its summary is made once;
  - summaries run in a background thread or in the CLI, never inside a model turn.
The model only writes the summary text.

Provenance: a summary is a crumb of kind "sum":
  ranges    the summary text (a line-spine doc: every line has an id)
  heritage  [the summarized crumb]: its Merkle id fixes every source byte
  meta      by, model, model_sha256, params, and input: the doc id of the exact prompt
            the model saw. Decoding is greedy, so a re-run on meta.input gives the same text.
The index maps each source to its summary (derived data: rebuild it from the "sum" crumbs).

The default model: Qwen3.5-0.8B (Q4_K_M GGUF, 580 MB, Apache-2.0) through llama.cpp's
llama-completion, one process per call (no server), greedy, thinking off. About 1.9 s and 0.9 GB per
call on an M-series Mac. It was the best of ten local models up to 1.5B on the summary bench.
BREADCRUMBS_SUMMARIZE=0 turns it off in the provider; BREADCRUMBS_SUM_MODEL and
BREADCRUMBS_LLAMA choose another model file or binary, and BREADCRUMBS_SUM_TEMPLATE its chat
family (TEMPLATES: qwen, chatml, nemotron, granite, gemma3, gemma4).
"""

import hashlib
import os
import re
import shutil
import subprocess
import threading

from .crumbs import Crumb, get_crumb, put_crumb
from .ingest import first_line, leaves
from .lines import get_lines, put_text, split_lines

MIN_BYTES = 2048
MAX_INPUT = 9000  # bytes of source text per call: about 2,300 tokens, inside a 4,096 context
KINDS = ("sess", "span")
CUT = "\u2026"
MODEL = "~/models/Qwen_Qwen3.5-0.8B-Q4_K_M.gguf"
SYSTEM = "You are a precise technical summarizer."
ASK = ("Summarize the log below in at most 60 words. Keep names, numbers, file names and decisions. "
       "Do not add facts.\n\nLOG:\n")
_MODEL_LOCK = threading.Lock()  # one model process at a time per Python process
_SHA = {}
_TOO_LONG = re.compile(rb"prompt is too long \((\d+) tokens, max (\d+)\)")
TRIES = 3


class PromptTooLong(ValueError):
    """The prompt does not fit the model context. Agent logs (code, paths, ids) can need one
    token per 2 bytes, so the byte cap alone cannot promise a fit."""

    def __init__(self, tokens, limit):
        super().__init__("prompt is too long (%d tokens, max %d)" % (tokens, limit))
        self.tokens, self.limit = tokens, limit


_LABEL = re.compile(r"\A\s*(?:LOG|TEXT|SUMMARY|Summary|Log)\s*:[ \t]*\n?")
# Chat turn markers per model family ({s} system, {u} the request plus the text). Thinking is off for
# the families that think. A prompt is built as text and sent as raw bytes (see Llama.argv).
TEMPLATES = {
    "qwen": "<|im_start|>system\n{s}<|im_end|>\n<|im_start|>user\n{u}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n",
    "chatml": "<|im_start|>system\n{s}<|im_end|>\n<|im_start|>user\n{u}<|im_end|>\n<|im_start|>assistant\n",
    "nemotron": "<|im_start|>system\n{s}<|im_end|>\n<|im_start|>user\n{u}<|im_end|>\n<|im_start|>assistant\n<think></think>",
    "granite": ("<|start_of_role|>system<|end_of_role|>{s}<|end_of_text|>\n<|start_of_role|>user<|end_of_role|>{u}"
                "<|end_of_text|>\n<|start_of_role|>assistant<|end_of_role|>"),
    "gemma3": "<start_of_turn>user\n{s}\n\n{u}<end_of_turn>\n<start_of_turn>model\n",
    "gemma4": "<|turn>system\n{s}<turn|>\n<|turn>user\n{u}<turn|>\n<|turn>model\n",
}


def clean(out):
    """The model answer without outer space and without a label copied from the prompt."""
    return _LABEL.sub("", (out or "").strip(), count=1).strip()


def shrink(max_input, err):
    """A smaller input cap after a prompt that was too long (10% margin for the template)."""
    return int(max_input * err.limit / err.tokens * 0.9)


class Llama:
    """llama.cpp one-shot: a chat prompt on stdin (raw bytes), greedy decode, thinking off."""
    name = "llama-completion"

    def __init__(self, model=None, exe=None, n=120, ctx=4096, timeout=180, template=None):
        self.model = os.path.expanduser(model or os.environ.get("BREADCRUMBS_SUM_MODEL") or MODEL)
        self.exe = (exe or os.environ.get("BREADCRUMBS_LLAMA") or shutil.which("llama-completion")
                    or "/opt/homebrew/bin/llama-completion")
        template = template or os.environ.get("BREADCRUMBS_SUM_TEMPLATE") or "qwen"
        if template not in TEMPLATES:
            raise ValueError("unknown template %r (known: %s)" % (template, ", ".join(TEMPLATES)))
        self.params = {"n": n, "ctx": ctx, "temp": 0, "template": template}
        self.timeout = timeout

    def available(self):
        return os.path.isfile(self.model) and os.access(self.exe, os.X_OK)

    def ident(self):
        st = os.stat(self.model)
        key = (self.model, st.st_size, st.st_mtime_ns)
        if key not in _SHA:
            with open(self.model, "rb") as f:
                _SHA[key] = hashlib.file_digest(f, "sha256").hexdigest()
        return {"model": os.path.basename(self.model), "model_sha256": _SHA[key]}

    def prompt(self, text):
        return TEMPLATES[self.params["template"]].format(s=SYSTEM, u=(ASK + text).strip())

    def argv(self):
        # -bf reads the prompt bytes as they are; -f drops a trailing newline, and most chat
        # templates end in one ("assistant\n", "</think>\n\n"). --no-escape keeps a literal
        # backslash-n as two characters (llama.cpp unescapes prompts by default).
        return [self.exe, "-m", self.model, "-bf", "/dev/stdin", "--no-escape", "-n", str(self.params["n"]),
                "-c", str(self.params["ctx"]), "-ngl", "99", "--temp", "0",
                "-no-cnv", "--no-display-prompt", "--simple-io"]  # never --log-disable: it empties stdout

    def __call__(self, prompt):
        argv = self.argv()
        with _MODEL_LOCK:
            p = subprocess.run(argv, input=prompt.encode("utf-8"), capture_output=True, timeout=self.timeout)
        if p.returncode:
            m = _TOO_LONG.search(p.stderr)
            if m:
                raise PromptTooLong(int(m.group(1)), int(m.group(2)))
            raise RuntimeError("llama-completion exit %d: %s" % (p.returncode, p.stderr[-300:].decode("utf-8", "replace")))
        return p.stdout.decode("utf-8", "replace").replace("[end of text]", "").strip()


def default_runner():
    """The default model, or None when BREADCRUMBS_SUMMARIZE=0 or the model or binary is missing."""
    if os.environ.get("BREADCRUMBS_SUMMARIZE", "auto") == "0":
        return None
    runner = Llama()
    return runner if runner.available() else None


def _turn_text(store, crumb, label=None):
    role = label or crumb.meta.get("role") or crumb.kind
    body = b"".join(b"".join(get_lines(store, doc, a, b)) for doc, a, b in crumb.ranges)
    return "%s: %s" % (role, body.decode("utf-8", "replace").strip())


def _blocks(store, crumb_id, summaries, top=True):
    crumb = get_crumb(store, crumb_id)
    if not top and summaries:
        summary = summaries(crumb_id)
        if summary:  # a part that has a summary gives its summary, not its turns
            return [_turn_text(store, get_crumb(store, summary), "summary")]
    if not crumb.kids:
        return [_turn_text(store, crumb)]
    return [b for kid in crumb.kids for b in _blocks(store, kid, summaries, False)]


def _cap(sizes, total):
    """The largest c with sum(min(s, c)) <= total: short blocks stay whole, long ones share the rest."""
    left, rest = total, sorted(sizes)
    for i, size in enumerate(rest):
        share = left // (len(rest) - i)
        if size > share:
            return share
        left -= size
    return rest[-1] if rest else 0


def _cut(text, limit):
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:max(0, limit - 3)].decode("utf-8", "ignore") + CUT


def source_text(store, crumb_id, summaries=None, max_input=MAX_INPUT):
    """The text a summary of crumb_id is made from: one block per turn (or per summarized part),
    in order, each block cut to a fair share so that the whole fits max_input bytes."""
    blocks = _blocks(store, crumb_id, summaries)
    cap = _cap([len(b.encode("utf-8")) for b in blocks], max_input - len(blocks))
    return "".join(_cut(b, cap) + "\n" for b in blocks)


def plan(store, index, crumb_id, runner, max_input=MAX_INPUT):
    """The exact prompt for crumb_id's summary, or None (it has one, or it gets none)."""
    if index.summary_of(crumb_id):
        return None
    src = get_crumb(store, crumb_id)
    if src.kind not in KINDS:
        return None
    text = source_text(store, crumb_id, index.summary_of, max_input)
    if len(text.encode("utf-8")) <= MIN_BYTES:
        return None
    return runner.prompt(text)


def store_summary(store, index, crumb_id, runner, prompt, out):
    """Keep the model's answer as a "sum" crumb with full provenance. Returns its id, or None."""
    out = clean(out)
    if not out:
        return None
    src = get_crumb(store, crumb_id)
    data = (out + "\n").encode("utf-8")
    doc, n = put_text(store, data), len(split_lines(data))
    meta = dict(runner.ident(), by=runner.name, params=runner.params, input=put_text(store, prompt.encode("utf-8")))
    if "sess" in src.meta:
        meta["sess"] = src.meta["sess"]
    sid = put_crumb(store, Crumb("sum", ("summary: " + first_line(out))[:60], ranges=[[doc, 0, n]],
                                 heritage=[crumb_id], meta=meta))
    index.add(sid, "%s:0-%d" % (doc, n), out, kind="sum")
    index.set_summary(crumb_id, sid)
    return sid


def summarize(store, index, crumb_id, runner, max_input=MAX_INPUT):
    """The id of crumb_id's summary crumb (made now if needed), or None if it gets none."""
    have = index.summary_of(crumb_id)
    if have:
        return have
    for _ in range(TRIES):
        prompt = plan(store, index, crumb_id, runner, max_input)
        if prompt is None:
            return None
        try:
            out = runner(prompt)
        except PromptTooLong as err:
            max_input = shrink(max_input, err)
            continue
        return store_summary(store, index, crumb_id, runner, prompt, out)
    return None


def closed_spans(store, crumb_id, full=False):
    """Spans under crumb_id that can never change, innermost first. Only the last span at each
    level can still grow; every other span holds a fixed list of kids, so its id is final."""
    spans = [k for k in get_crumb(store, crumb_id).kids if get_crumb(store, k).kind == "span"]
    out = []
    for i, span in enumerate(spans):
        done = full or i < len(spans) - 1
        out.extend(closed_spans(store, span, done))
        if done:
            out.append(span)
    return out
