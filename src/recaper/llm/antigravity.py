"""Antigravity models (your Google AI subscription) through the Antigravity ACP server.

T3 Code / Antigravity install `agy_acp_server`, an Agent Client Protocol server (JSON-RPC over stdio) that is
already logged in with your Google account. We drive it headlessly:

* one server process per run, a fresh session per request (sessions keep history; we want none);
* a private home (~/.recaper/antigravity) seeded with your existing login, so our sessions don't pile up in
  T3's folder and no MCP servers / skills from your IDE setup are loaded;
* a throwaway working dir, no fs/terminal client capabilities, and every permission request refused, so the
  agent can't edit files or run commands - it is used as a plain text/vision model.

The model list comes from the server (it depends on your plan), e.g. gemini-4-argon-high, gemini-3.8-flash-low.

Environment overrides: RECAPER_AGY_SERVER (path to agy_acp_server), RECAPER_AGY_LOGIN (folder with the
login: contains antigravity-acp/acp_token.json).
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Sequence

from .base import (
    ImagePart,
    Part,
    Provider,
    ProviderError,
    QuotaError,
    RefusalError,
    UnavailableError,
    is_quota_message,
    is_refusal_message,
)

EXE = "agy_acp_server.exe" if os.name == "nt" else "agy_acp_server"
PRIVATE_HOME = Path.home() / ".recaper" / "antigravity"
LOGIN_FILES = ("settings.json", "acp_token.json")
MAX_CONCURRENT_PROMPTS = 4
NO_TOOLS = (
    "You are being used as a non-interactive text/vision model by an automated program. "
    "Do NOT call any tools, do NOT read or write files, do NOT run commands, do NOT ask questions. "
    "Reply directly with ONLY the requested output (raw JSON when JSON is requested, with no markdown fences or commentary)."
)


# ------------------------------------------------------------------ discovery


def find_server() -> Path | None:
    if env := os.environ.get("RECAPER_AGY_SERVER"):
        return Path(env) if Path(env).is_file() else None
    if harness := os.environ.get("ANTIGRAVITY_HARNESS_PATH"):  # running inside an Antigravity session
        cand = Path(harness).with_name(EXE)
        if cand.is_file():
            return cand
    for root in (Path.home() / ".t3" / "tools" / "antigravity-acp").glob("*"):
        try:
            rid = json.loads((root / "active.json").read_text("utf-8"))["releaseId"]
            cand = root / "versions" / rid / EXE
            if cand.is_file():
                return cand
        except (OSError, ValueError, KeyError):
            pass
        found = sorted(root.glob(f"versions/*/{EXE}"), key=lambda p: p.stat().st_mtime, reverse=True)
        if found:
            return found[0]
    which = shutil.which("agy_acp_server")
    return Path(which) if which else None


def find_login() -> Path | None:
    """A folder whose antigravity-acp/ holds a logged-in acp_token.json (the server's GEMINI_HOME)."""
    cands: list[Path] = []
    for env in ("RECAPER_AGY_LOGIN", "GEMINI_HOME"):
        if os.environ.get(env):
            cands.append(Path(os.environ[env]))
    cands += (Path.home() / ".t3" / "userdata" / "providers" / "antigravity").glob("*")
    cands.append(Path.home() / ".gemini")
    ok = [c for c in cands if (c / "antigravity-acp" / "acp_token.json").is_file()]
    if not ok:
        return None
    return max(ok, key=lambda c: (c / "antigravity-acp" / "acp_token.json").stat().st_mtime)


def _prepare_home(login: Path) -> Path:
    """Copy the login into our private home (refreshing it when the source is newer) and clear old sessions."""
    src, dst = login / "antigravity-acp", PRIVATE_HOME / "antigravity-acp"
    dst.mkdir(parents=True, exist_ok=True)
    for name in LOGIN_FILES:
        s, d = src / name, dst / name
        if s.is_file() and (not d.exists() or s.stat().st_mtime > d.stat().st_mtime):
            shutil.copy2(s, d)
    _clear_sessions(dst)
    return PRIVATE_HOME


def _clear_sessions(acp_dir: Path) -> None:
    for sub in ("conversations", "brain"):
        shutil.rmtree(acp_dir / sub, ignore_errors=True)


# ------------------------------------------------------------------ connection


class _Server:
    """One agy_acp_server process; thread-safe, any number of concurrent sessions."""

    def __init__(self, exe: Path, home: Path):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANTIGRAVITY_")}
        env.update(GEMINI_HOME=str(home), AGY_ACP_FORCE_FILE_STORAGE="1")
        harness = exe.with_name("localharness_external.exe" if os.name == "nt" else "localharness_external")
        if harness.is_file():
            env["ANTIGRAVITY_HARNESS_PATH"] = str(harness)
        self.home = home
        self.workdir = Path(tempfile.mkdtemp(prefix="recaper-agy-"))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.workdir,
            env=env, text=True, encoding="utf-8", errors="replace", creationflags=flags,
        )  # fmt: skip
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(MAX_CONCURRENT_PROMPTS)
        self._next = 0
        self._pending: dict[int, queue.Queue] = {}
        self._sessions: dict[str, queue.Queue] = {}
        self._allow: dict[str, set[str]] = {}  # session -> tool names it may run (image generation only)
        self.stderr: deque[str] = deque(maxlen=60)
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        init = self.request("initialize", {
            "protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
        }, timeout=60)  # fmt: skip
        caps = init.get("agentCapabilities") or {}
        self.images = bool((caps.get("promptCapabilities") or {}).get("image", True))
        self.models: list[dict] = []
        self.default_model: str | None = None

    # -- io
    def alive(self) -> bool:
        return self.proc.poll() is None

    def _send(self, msg: dict) -> None:
        with self._lock:
            if not self.alive():
                raise ProviderError(f"antigravity server exited: {self.last_error()}")
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def _read_err(self) -> None:
        for line in self.proc.stderr:
            self.stderr.append(line.rstrip())

    def _read_out(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "method" not in msg:  # a response to one of our requests
                q = self._pending.pop(msg.get("id"), None)
                if q:
                    q.put(msg)
            elif msg["method"] == "session/update":
                q = self._sessions.get(msg["params"].get("sessionId"))
                if q:
                    q.put(msg["params"]["update"])
            elif "id" in msg:  # the agent asks us something: refuse everything not explicitly allowed
                if msg["method"] == "session/request_permission":
                    reply = {"result": {"outcome": {"outcome": "cancelled"}}}
                    params = msg.get("params") or {}
                    allowed = self._allow.get(params.get("sessionId"), set())
                    call = params.get("toolCall") or {}
                    name = str(call.get("title") or "").lower()
                    option = next((o for o in params.get("options") or [] if o.get("kind") == "allow_once"), None)
                    if option and any(t in name for t in allowed):  # e.g. "Run generate_image?"
                        reply = {"result": {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}}
                else:
                    reply = {"error": {"code": -32601, "message": "not supported by recaper"}}
                try:
                    self._send({"jsonrpc": "2.0", "id": msg["id"], **reply})
                except ProviderError:
                    pass
        for q in list(self._pending.values()):  # process died: wake every waiter
            q.put({"error": {"message": f"antigravity server exited: {self.last_error()}"}})

    def last_error(self) -> str:
        errs = [l for l in self.stderr if l[:1] in ("E", "W") or "rror" in l]
        return (errs or list(self.stderr) or ["no output"])[-1][-400:]

    def request(self, method: str, params: dict, timeout: float = 120) -> Any:
        with self._lock:
            self._next += 1
            rid = self._next
        q: queue.Queue = queue.Queue()
        self._pending[rid] = q
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty as e:
            self._pending.pop(rid, None)
            raise ProviderError(f"antigravity {method} timed out after {timeout:.0f}s") from e
        if "error" in msg:
            err = msg["error"]
            text = f"{err.get('message', err)}"
            if isinstance(err.get("data"), dict) and err["data"].get("availableModels"):
                text += f" (available: {', '.join(err['data']['availableModels'])})"
            if is_quota_message(text):
                raise QuotaError(f"antigravity: {text}")
            if is_refusal_message(text):
                raise RefusalError(f"antigravity policy refusal: {text}")
            raise ProviderError(f"antigravity {method}: {text}")
        return msg.get("result") or {}

    # -- sessions
    def new_session(self) -> tuple[str, queue.Queue]:
        res = self.request("session/new", {"cwd": str(self.workdir), "mcpServers": []}, timeout=120)
        sid = res["sessionId"]
        models = res.get("models") or {}
        if models.get("availableModels"):
            self.models = models["availableModels"]
            self.default_model = models.get("currentModelId")
        q: queue.Queue = queue.Queue()
        self._sessions[sid] = q
        return sid, q

    def end_session(self, sid: str) -> None:
        self._sessions.pop(sid, None)
        self._allow.pop(sid, None)
        acp = self.home / "antigravity-acp"
        for ext in (".db", ".db-shm", ".db-wal", ".meta"):
            try:
                (acp / "conversations" / f"{sid}{ext}").unlink(missing_ok=True)
            except OSError:
                pass  # still open on the server side; cleared at the next start
        shutil.rmtree(acp / "brain" / sid, ignore_errors=True)

    def close(self) -> None:
        if self.alive():
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    self.proc.kill()
        shutil.rmtree(self.workdir, ignore_errors=True)
        _clear_sessions(self.home / "antigravity-acp")


_server: _Server | None = None
_server_lock = threading.Lock()


def _get_server() -> _Server:
    global _server
    with _server_lock:
        if _server and _server.alive():
            return _server
        exe = find_server()
        if not exe:
            raise UnavailableError("Antigravity ACP server not found (install Antigravity in T3 Code, or set RECAPER_AGY_SERVER)")
        login = find_login()
        if not login:
            raise UnavailableError("no Antigravity login found (sign in to Antigravity in T3 Code, or set RECAPER_AGY_LOGIN)")
        try:
            _server = _Server(exe, _prepare_home(login))
        except ProviderError as e:
            raise UnavailableError(str(e)) from e
        atexit.register(_server.close)
        return _server


def list_models() -> list[dict]:
    """[{id, name}] the signed-in plan offers (starts the server if needed)."""
    srv = _get_server()
    if not srv.models:
        sid, _ = srv.new_session()
        srv.end_session(sid)
    return [{"id": m["modelId"], "name": m.get("name") or m["modelId"]} for m in srv.models]


def resolve_model(wanted: str | None, available: list[str]) -> str | None:
    """Exact id, else case-insensitive, else the -high / -medium / -low variant of a base/short name."""
    if not wanted or not available:
        return wanted
    low = {a.lower(): a for a in available}
    w = wanted.lower()
    if w in low:
        return low[w]
    for suffix in ("-high", "-medium", "-low"):
        if w + suffix in low:
            return low[w + suffix]
    for suffix in ("-high", "-medium", "-low", ""):
        for a in available:
            if w in a.lower() and a.lower().endswith(suffix):
                return a
    return wanted  # let the server reject it with the list of valid ids


# ------------------------------------------------------------------ provider


class AntigravityProvider(Provider):
    name = "antigravity"
    vision = True

    def __init__(self, model: str | None):
        super().__init__(model or None)

    def check(self) -> None:
        if not find_server():
            raise UnavailableError("Antigravity ACP server not found (install Antigravity in T3 Code, or set RECAPER_AGY_SERVER)")
        if not find_login():
            raise UnavailableError("no Antigravity login found (sign in to Antigravity in T3 Code)")

    def complete(self, system: str, parts: Sequence[Part]) -> str:
        srv = _get_server()
        with srv._sem:
            sid, updates = srv.new_session()
            try:
                if self.model:
                    model = resolve_model(self.model, [m["modelId"] for m in srv.models])
                    srv.request("session/set_model", {"sessionId": sid, "modelId": model}, timeout=60)
                prompt: list[dict] = [{"type": "text", "text": f"{NO_TOOLS}\n\n# Instructions\n\n{system}\n\n# Input\n"}]
                for p in parts:
                    if isinstance(p, ImagePart):
                        if not srv.images:
                            raise UnavailableError("this Antigravity server does not accept images")
                        prompt.append({"type": "image", "mimeType": "image/jpeg", "data": p.b64()})
                    else:
                        prompt.append({"type": "text", "text": p})
                try:
                    res = srv.request("session/prompt", {"sessionId": sid, "prompt": prompt}, timeout=900)
                except ProviderError:
                    try:
                        srv._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
                    except ProviderError:
                        pass
                    raise
                chunks: list[str] = []
                while True:
                    try:
                        u = updates.get_nowait()
                    except queue.Empty:
                        break
                    if u.get("sessionUpdate") == "agent_message_chunk" and (u.get("content") or {}).get("type") == "text":
                        chunks.append(u["content"]["text"])
                stop = res.get("stopReason", "end_turn")
                text = "".join(chunks)
                if is_refusal_message(text) or is_refusal_message(srv.last_error()):
                    detail = text.strip() or srv.last_error()
                    raise RefusalError(f"content filter refused prompt ({detail[:200]})")
                if stop not in ("end_turn", "max_tokens") or not text.strip():
                    msg = f"antigravity stopped ({stop}) with {'no text' if not text.strip() else 'partial text'}"
                    if is_quota_message(srv.last_error()):
                        raise QuotaError(f"{msg}: {srv.last_error()}")
                    raise ProviderError(msg)
                return text
            finally:
                srv.end_session(sid)


# ------------------------------------------------------------------ image generation

IMAGE_TOOL = "generate_image"
IMAGE_INSTRUCTIONS = (
    "You are used by an automated program as an image generator. Call the generate_image tool exactly once, "
    "with AspectRatio \"16:9\". Do not call any other tool and do not ask questions. Write the tool's Prompt yourself "
    "from the scene below: keep every detail of the scene, and when reference images are attached, describe their "
    "art style (line art, colouring, shading, palette, era) and the look of the characters precisely, because the "
    "tool cannot see the references. No text, captions, speech bubbles or watermarks in the image. "
    "After the tool finishes, reply only with the word DONE."
)


def _file_uri_path(uri: str) -> Path:
    """file://C:/x.jpg or file:///C:/x.jpg (Windows), file:///home/x.jpg (POSIX) -> Path."""
    from urllib.parse import unquote

    raw = unquote(uri[len("file:"):])
    return Path(raw.lstrip("/") if os.name == "nt" else "/" + raw.lstrip("/"))


def generate_image(prompt: str, refs: Sequence[Path], dest: Path, model: str | None = None, timeout: float = 420) -> Path:
    """Draw one 16:9 image with the agent's built-in generate_image tool (Google AI plan, no API key).

    Only that tool is allowed in the session. Reference images go to the model, which carries their style into the
    tool prompt in words (the tool itself takes no images). Returns `dest` (the file the tool produced, copied)."""
    srv = _get_server()
    with srv._sem:
        sid, updates = srv.new_session()
        srv._allow[sid] = {IMAGE_TOOL}
        try:
            if model:
                srv.request("session/set_model", {"sessionId": sid, "modelId": resolve_model(model, [m["modelId"] for m in srv.models])}, timeout=60)
            parts: list[dict] = [{"type": "text", "text": f"{IMAGE_INSTRUCTIONS}\n\nSCENE:\n{prompt}"}]
            for r in refs:
                parts.append({"type": "image", "mimeType": "image/jpeg", "data": ImagePart(Path(r)).b64()})
            srv.request("session/prompt", {"sessionId": sid, "prompt": parts}, timeout=timeout)
            made: list[Path] = []
            texts: list[str] = []
            while True:
                try:
                    u = updates.get_nowait()
                except queue.Empty:
                    break
                if u.get("sessionUpdate") == "agent_message_chunk" and (u.get("content") or {}).get("type") == "text":
                    texts.append(u["content"]["text"])
                content = u.get("content")
                for c in content if isinstance(content, list) else []:  # tool_call_update: [{type: content, content: {...}}]
                    inner = c.get("content") or {}
                    uri = str(inner.get("uri") or "")
                    if inner.get("type") == "image" and uri.startswith("file:"):
                        made.append(_file_uri_path(uri))
            made = [m for m in made if m.is_file()]
            if not made:
                brain = srv.home / "antigravity-acp" / "brain" / sid
                made = sorted((f for f in brain.glob("*") if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")), key=lambda f: f.stat().st_mtime)
            if not made:
                detail = "".join(texts).strip() or srv.last_error()
                if is_refusal_message(detail):
                    raise RefusalError(f"antigravity image refused: {detail[:200]}")
                if is_quota_message(detail):
                    raise QuotaError(f"antigravity: {detail[:200]}")
                raise ProviderError(f"antigravity produced no image ({detail[:200]})")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(made[-1], dest)
            return dest
        finally:
            srv.end_session(sid)


def _main() -> None:  # python -m recaper.llm.antigravity  -> list models
    print(f"server: {find_server()}\nlogin:  {find_login()}")
    for m in list_models():
        print(f"  antigravity:{m['id']:28s} {m['name']}")


if __name__ == "__main__":
    _main()
    sys.exit(0)
