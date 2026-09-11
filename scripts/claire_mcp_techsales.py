#!/usr/bin/env python3
"""
claire_mcp.py — MCP stdio proxy for CLAIRE's Streamable HTTP MCP server.

Architecture
────────────
Claude calls CLAIRE tools (OrchestratorSkill / MdmOrchestratorSkill) directly —
we do NOT wrap or hard-code their signatures. The actual tool list is fetched live
from CLAIRE's own MCP server via the SDK, so any new tools appear automatically.

The only extra tool we inject is `working`, a local polling helper:

    working()
        Returns the next item from the response queue using a cursor.
        If the queue is empty but the background task is still running, returns
        "⏳ Still working..." so Claude keeps polling.
        Once the task finishes and the queue is drained, returns "done".

Workflow Claude follows (described in SKILL.md):
  1. Call OrchestratorSkill (or MdmOrchestratorSkill) with { "prompt": "..." }
  2. Immediately call working() in a tight loop (~1 s interval) until it returns
     an ANSWER: … or "done".

Dependencies
────────────
  pip install mcp httpx   (handled by install.py / .venv)
"""

# ── stdlib ─────────────────────────────────────────────────────────────────────
import asyncio
import json
import os
import ssl
import sys
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── load .env from project root (scripts/../.env) — before any config reads ───
def _load_env() -> None:
    try:
        from dotenv import load_dotenv as _load_dotenv
    except ImportError:
        return  # dotenv optional; env vars must already be set
    # Try <project_root>/.env (i.e. one level above this script's directory)
    _here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(os.path.dirname(_here), ".env"),  # ../. env  (normal)
        os.path.join(_here, ".env"),                   # same dir fallback
    ):
        if os.path.isfile(candidate):
            _load_dotenv(candidate)
            print(f"[claire] Loaded .env from {candidate}", file=sys.stderr, flush=True)
            return
    print("[claire] WARNING: no .env file found; relying on environment variables", file=sys.stderr, flush=True)

_load_env()

# ── line-buffered I/O (stdout = MCP JSON-RPC, stderr = diagnostics) ────────────
# Force UTF-8 on Windows so unicode log chars (→ etc.) don't crash on cp1252 stderr.
sys.stdout = open(sys.stdout.fileno(), "w", buffering=1, encoding="utf-8", closefd=False)
sys.stderr = open(sys.stderr.fileno(), "w", buffering=1, encoding="utf-8", closefd=False)

# ── paths ──────────────────────────────────────────────────────────────────────
_SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH         = os.path.join(_SCRIPT_DIR, ".cache_techsales.json")
TOKEN_LIFETIME_MIN = 30
REFRESH_BUFFER_MIN = 5          # refresh when < 5 min remain

# ── SSL: accept self-signed corporate certs ────────────────────────────────────
_SSL                  = ssl.create_default_context()
_SSL.check_hostname   = False
_SSL.verify_mode      = ssl.CERT_NONE


# ══════════════════════════════════════════════════════════════════════════════
# Logging
# ══════════════════════════════════════════════════════════════════════════════
def log(msg: str) -> None:
    print(f"[claire] {msg}", file=sys.stderr, flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# Dependency check
# ══════════════════════════════════════════════════════════════════════════════
def _check_deps() -> None:
    missing = []
    for pkg in ("mcp", "httpx"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        log(f"FATAL: missing packages: {', '.join(missing)}")
        log("  Fix: re-run install.py to recreate the .venv")
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# Config  (claude_desktop_config.json → "claireIDMCAgent" block)
# ══════════════════════════════════════════════════════════════════════════════
def _config_path() -> str:
    # Explicit override wins — set CLAIRE_CONFIG_PATH in the MCP launch entry's env
    override = os.environ.get("CLAIRE_CONFIG_PATH")
    if override:
        return override
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Claude/claude_desktop_config.json"
        )
    if sys.platform == "win32":
        # CCD (Claude-3p) takes precedence; fall back to standard Claude Desktop path
        local = os.environ.get("LOCALAPPDATA", "")
        ccd_path = os.path.join(local, "Claude-3p", "claude_desktop_config.json")
        if os.path.exists(ccd_path):
            return ccd_path
        return os.path.join(
            os.environ.get("APPDATA", ""), "Claude", "claude_desktop_config.json"
        )
    return os.path.expanduser("~/.config/Claude/claude_desktop_config.json")


def load_config() -> dict:
    # Fast path: credentials supplied directly via env (no file lookup needed)
    if os.environ.get("CLAIRE_USERNAME") and os.environ.get("CLAIRE_PASSWORD"):
        log("Loading CLAIRE credentials from environment variables")
        return {
            "username":       os.environ["CLAIRE_USERNAME"],
            "password":       os.environ["CLAIRE_PASSWORD"],
            "identity_url":   os.environ.get("CLAIRE_IDENTITY_URL", "https://dmp-us.informaticacloud.com/identity-service"),
            "client_id":      os.environ.get("CLAIRE_CLIENT_ID", "cdlg_app"),
            "claire_api_url": os.environ.get("CLAIRE_API_URL", "https://claire-gpt-api.dmp-us.informaticacloud.com"),
        }
    path = _config_path()
    log(f"Reading config from: {path}  (APPDATA={os.environ.get('APPDATA', '<unset>')})")
    if not os.path.exists(path):
        log(f"FATAL: config not found at {path}")
        sys.exit(1)
    with open(path, encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    log(f"Config top-level keys: {list(cfg.keys())}")
    claire = cfg.get("claireIDMCAgentTechSales")
    if not claire:
        log("FATAL: 'claireIDMCAgentTechSales' block missing from claude_desktop_config.json")
        sys.exit(1)
    return claire


# ══════════════════════════════════════════════════════════════════════════════
# Token cache  (.cache.json — persists JWT across restarts)
# ══════════════════════════════════════════════════════════════════════════════
def load_cache() -> dict:
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_PATH, "w") as fh:
        json.dump(cache, fh, indent=2)


def _token_valid(cache: dict) -> bool:
    if not cache.get("jwt_token") or not cache.get("session_id"):
        return False
    try:
        expiry = datetime.fromisoformat(
            cache["expires_at"].replace("Z", "+00:00")
        )
        return datetime.now(timezone.utc) < (
            expiry - timedelta(minutes=REFRESH_BUFFER_MIN)
        )
    except (ValueError, TypeError, KeyError):
        return False


# ══════════════════════════════════════════════════════════════════════════════
# HTTP helper  (sync — runs in executor so it never blocks the event loop)
# ══════════════════════════════════════════════════════════════════════════════
def _http(
    url: str,
    method: str = "POST",
    data: dict | None = None,
    headers: dict | None = None,
) -> tuple[str, dict, int]:
    hdrs = dict(headers or {})
    hdrs.setdefault("Content-Type", "application/json")
    payload = json.dumps(data).encode() if data else b""
    req = urllib.request.Request(
        url,
        data=payload if method != "GET" else None,
        headers=hdrs,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL) as resp:
            return resp.read().decode(), dict(resp.headers), resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        log(f"HTTP {exc.code} {exc.reason} → {url}\n  body: {body[:300]}")
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {body[:200]}")


# ══════════════════════════════════════════════════════════════════════════════
# Auth helpers  (sync)
# ══════════════════════════════════════════════════════════════════════════════
def _fetch_tokens_sync(config: dict) -> dict:
    base = config["identity_url"].rstrip("/")

    body, _, _ = _http(
        f"{base}/api/v1/Login",
        data={"username": config["username"], "password": config["password"]},
    )
    login = json.loads(body)
    sid = (
        login.get("userInfo", {}).get("sessionId")
        or login.get("sessionId")
        or login.get("session_id")
    )
    if not sid:
        raise RuntimeError(f"No session_id in login response: {login}")

    nonce = uuid.uuid4().hex
    body, _, _ = _http(
        f"{base}/api/v1/jwt/Token?client_id={config['client_id']}&nonce={nonce}",
        headers={"IDS-SESSION-ID": sid, "Content-Type": "application/json"},
    )
    jwt_token = json.loads(body).get("jwt_token")
    if not jwt_token:
        raise RuntimeError("No jwt_token in JWT response")

    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=TOKEN_LIFETIME_MIN)
    ).isoformat()
    log("Tokens refreshed.")
    return {"session_id": sid, "jwt_token": jwt_token, "expires_at": expires_at}


def _create_conversation_sync(config: dict, jwt_token: str) -> str:
    url = (
        config["claire_api_url"].rstrip("/")
        + "/claire-conversation-session/api/v2/conversations"
    )
    body, _, _ = _http(
        url,
        data={"name": "Conversation from Claude Desktop", "channel": "cgpt"},
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json",
        },
    )
    conv_id = json.loads(body).get("conversationId")
    if not conv_id:
        raise RuntimeError("No conversationId in response")
    log(f"New conversation: {conv_id}")
    return conv_id


# ══════════════════════════════════════════════════════════════════════════════
# ResponseQueue — cursor-based, dedup-safe
# ══════════════════════════════════════════════════════════════════════════════
class ResponseQueue:
    """
    A simple ordered queue consumed via an integer cursor.

    Producer (background task) calls push() for each notification or final
    answer item.  Consumer (working tool) calls drain() to atomically collect
    all items since its last call — returning them in order without duplicates.

    Because cursor only moves forward and items are never removed, there is no
    race between concurrent drain() calls; each call is idempotent at a given
    cursor position.
    """

    def __init__(self) -> None:
        self._items:  list[dict] = []
        self._cursor: int        = 0
        self._done:   bool       = False
        self._error:  str | None = None
        self._event               = asyncio.Event()

    # ── producer API ───────────────────────────────────────────────────────────
    def push(self, item: dict) -> None:
        """Append one item and wake any waiting consumer."""
        self._items.append(item)
        self._event.set()

    def finish(self, error: str | None = None) -> None:
        """Signal that the producer is done (optionally with an error)."""
        self._error = error
        self._done  = True
        self._event.set()

    # ── consumer API ──────────────────────────────────────────────────────────
    def drain(self) -> list[dict]:
        """Return all items added since the last drain(), advancing the cursor."""
        new_items     = self._items[self._cursor:]
        self._cursor += len(new_items)
        return new_items

    @property
    def is_done(self) -> bool:
        return self._done

    @property
    def error(self) -> str | None:
        return self._error

    async def wait(self, timeout: float = 20.0, settle_ms: float = 10.0) -> bool:
        """
        Block until new items are available beyond the current cursor position,
        or until the queue is finished.

        Returns True if something is ready; False on timeout (still running).

        settle_ms:
            After the first item wakes us, sleep this many milliseconds so that
            any items arriving in the same burst (e.g. back-to-back
            REASONING_TRACE notifications) land in the queue before we drain.
            10 ms is imperceptible to the user but eliminates the one-item-per-
            poll jitter caused by draining too early after the first push().
        """
        # Fast path — items already waiting beyond cursor
        if self._cursor < len(self._items) or self._done:
            return True

        # Clear event then double-check to avoid TOCTOU race
        self._event.clear()
        if self._cursor < len(self._items) or self._done:
            return True

        # Block until producer pushes something or timeout expires
        try:
            await asyncio.wait_for(asyncio.shield(self._event.wait()), timeout=timeout)
        except asyncio.TimeoutError:
            return False

        # Brief settle window: let co-arriving notifications land before drain
        if settle_ms > 0:
            await asyncio.sleep(settle_ms / 1000.0)

        return True


# ══════════════════════════════════════════════════════════════════════════════
# Working tool  (the only locally-defined MCP tool)
# ══════════════════════════════════════════════════════════════════════════════
# This module-level tool definition is intentionally self-contained so it can
# be replaced by a native CLAIRE MCP tool in the future with minimal changes.
#
# Contract:
#   • Call with no arguments after invoking any CLAIRE skill.
#   • Returns one or more queue items on each call (all accumulated since the
#     last call).
#   • Returns "⏳ Still working..." when the queue is empty but the task runs.
#   • Returns "done" when the task has finished and the queue is empty.
#   • Returns "Error: <message>" if the background task failed.

WORKING_TOOL_DEFINITION = {
    "name": "working",
    "description": (
        "Poll for progress updates and the final answer after calling a CLAIRE skill "
        "(OrchestratorSkill or MdmOrchestratorSkill). "
        "Call this tool repeatedly at ~1-second intervals until you receive a line "
        "starting with 'PROBE:' or 'ANSWER:' or the literal string 'done'. "
        "Each call returns all items queued since the previous call (cursor-based, "
        "no duplicates). Possible response types:\n"
        "  Plain text lines — CLAIRE's execution progress and reasoning. Display each line "
        "verbatim to the user as-is, without rephrasing or summarizing, then keep polling.\n"
        "  PROBE: <question> — clarification question, ask user and stop polling\n"
        "  ANSWER: <JSON> — final bundle with responseType=FINAL_BUNDLE containing "
        "artifact, plan, human_probe fields; render per SKILL.md and stop polling\n"
        "  Still working… — nothing ready yet, poll again silently\n"
        "  done — task finished with no further output, stop polling\n"
        "  Error: <msg> — task failed, report to user and stop\n"
        "NOTE: This is a local polling shim. When CLAIRE's MCP server provides "
        "native progress streaming, this tool will be removed and replaced."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# new_conversation tool  (local — signals the start of a new chat session)
# ══════════════════════════════════════════════════════════════════════════════
# Claude Desktop does NOT restart the MCP server process when the user clicks
# "New Chat" — the stdio process lives for the entire Claude Desktop session.
# The MCP `initialize` handshake fires only once at process start, so there is
# no protocol-level signal for "new chat".
#
# Solution: expose `new_conversation` as an explicit local tool and instruct
# Claude (via SKILL.md) to call it as the very first action in every session.
# Claude reliably knows it is in a fresh context (no prior messages), so this
# instruction is dependable in practice.
#
# When called, this tool:
#   1. Creates a new Informatica conversation ID via the CLAIRE API.
#   2. Updates self.conv_id so all subsequent CLAIRE tool calls use it.
#   3. Returns the new conversation ID so it's visible in Claude's tool log.

NEW_CONVERSATION_TOOL_DEFINITION = {
    "name": "new_conversation",
    "description": (
        "MUST be called once as the very first action at the start of every new "
        "chat session, BEFORE calling any CLAIRE skill. "
        "Creates a fresh Informatica conversation ID so CLAIRE has clean context "
        "for this chat. Do not call it again mid-conversation unless explicitly "
        "asked to reset the context. "
        "Returns the new conversation ID on success."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# MCPProxy
# ══════════════════════════════════════════════════════════════════════════════
class MCPProxy:
    """
    Bridges Claude Desktop (stdio MCP) to CLAIRE's Streamable HTTP MCP server.

    • CLAIRE tools are fetched live — no hard-coded tool list.
    • One extra tool (`working`) is injected for queue-based progress polling.
    • Each invocation of a CLAIRE tool starts a background asyncio task that
      streams notifications and the final answer into a ResponseQueue.
    • `working` drains the queue cursor-style, returning all new items at once.
    """

    def __init__(self, config: dict) -> None:
        self.config    = config
        self.mcp_url   = (
            config["claire_api_url"].rstrip("/")
            + "/claire-orchestrator-ai-agent/api/v1/mcp"
        )
        self.jwt_token:   str | None = None
        self.ids_session: str | None = None
        self.conv_id:     str | None = None
        self._lock                   = asyncio.Lock()

        # Active background task + queue
        self._queue: ResponseQueue | None     = None
        self._task:  asyncio.Task | None      = None

    # ── startup ────────────────────────────────────────────────────────────────
    async def startup(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            tokens = await loop.run_in_executor(None, _fetch_tokens_sync, self.config)
            self._apply_tokens(tokens)
            cache = load_cache()
            cache.update(tokens)
            existing = cache.get("conversation_id")
            if existing:
                self.conv_id = existing
                log(f"Reusing conversation: {self.conv_id}")
            else:
                self.conv_id = await loop.run_in_executor(
                    None, _create_conversation_sync, self.config, self.jwt_token
                )
                cache["conversation_id"] = self.conv_id
            save_cache(cache)
            log(f"Proxy ready → {self.mcp_url}")
        except Exception as exc:
            log(f"Startup warning (will retry on first call): {exc}")

    def _apply_tokens(self, tokens: dict) -> None:
        self.jwt_token   = tokens["jwt_token"]
        self.ids_session = tokens["session_id"]

    # ── token / conversation guard ─────────────────────────────────────────────
    async def _ensure_ready(self) -> None:
        loop  = asyncio.get_running_loop()
        cache = load_cache()
        if not _token_valid(cache):
            log("Token expired — refreshing…")
            tokens = await loop.run_in_executor(None, _fetch_tokens_sync, self.config)
            self._apply_tokens(tokens)
            cache.update(tokens)
            save_cache(cache)
        else:
            self._apply_tokens(cache)

        if not self.conv_id:
            self.conv_id = await loop.run_in_executor(
                None, _create_conversation_sync, self.config, self.jwt_token
            )
            cache = load_cache()
            cache["conversation_id"] = self.conv_id
            save_cache(cache)

        log(f"Auth OK | conv={self.conv_id[:8] if self.conv_id else 'None'}…")

    # ── SDK session factory ────────────────────────────────────────────────────
    def _sdk_session(self):
        """Returns an async context manager yielding (read_stream, write_stream)."""
        import contextlib
        import httpx
        from mcp.client.streamable_http import streamable_http_client

        client = httpx.AsyncClient(
            verify=False,
            headers={
                "Authorization":  f"Bearer {self.jwt_token}",
                "IDS-SESSION-ID": self.ids_session,
            },
            timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=10.0),
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=300.0,
            ),
        )

        @contextlib.asynccontextmanager
        async def _ctx():
            async with client:
                async with streamable_http_client(
                    self.mcp_url, http_client=client, terminate_on_close=False
                ) as streams:
                    # mcp <0.9 yields (read, write, get_session_id); newer yields (read, write).
                    # Take the first two regardless of tuple arity.
                    read, write = streams[0], streams[1]
                    yield read, write

        return _ctx()

    # ── Background task: call a CLAIRE tool and fill the queue ────────────────
    async def _run_claire_tool(
        self, queue: ResponseQueue, tool_name: str, args: dict
    ) -> None:
        """
        Opens an SDK session, calls the specified CLAIRE tool, and fills the
        ResponseQueue with items in two distinct phases:

        Phase 1 — notifications (arrive during execution, pushed individually):
          • EXECUTION_INSIGHT  — progress text, pushed one per notification
          • REASONING_TRACE    — internal thought, pushed one per notification

        Phase 2 — tool result (the final items[] array from CLAIRE's response):
          CLAIRE bundles some combination of ARTIFACT, PLAN, and HUMAN_PROBE
          in one atomic items[] array.  We merge multiple ARTIFACTs into one
          and push a single FINAL_BUNDLE envelope so working() can hand the
          complete answer to Claude atomically.

          FINAL_BUNDLE shape (internal only — never returned by CLAIRE):
            {
              "responseType": "FINAL_BUNDLE",
              "artifact":    <merged ARTIFACT item>  | None,
              "plan":        <PLAN item>              | None,
              "human_probe": <HUMAN_PROBE item>       | None,
            }

          TOKEN and STREAM_STATUS items are informational metadata — skipped.
        """
        from mcp import ClientSession

        def _on_notification(params) -> None:
            """Called by the SDK for each server-sent notification during execution."""
            try:
                raw = (
                    params.data
                    if hasattr(params, "data")
                    else params.get("params", {}).get("data", "")
                )
                chunk = json.loads(raw) if isinstance(raw, str) else raw
                for item in chunk.get("items", []):
                    rt = item.get("responseType", "")
                    # Queue both EXECUTION_INSIGHT and REASONING_TRACE
                    if rt in ("EXECUTION_INSIGHT", "REASONING_TRACE"):
                        queue.push(item)
                        log(f"  queued notification: {rt}")
                    # TOKEN, STREAM_STATUS → skip silently
            except Exception as exc:
                log(f"  notification parse error: {exc}")

        try:
            async with self._sdk_session() as (read, write):
                async with ClientSession(
                    read, write, logging_callback=_on_notification
                ) as session:
                    await session.initialize()
                    log(f"  SDK session open — calling {tool_name}…")
                    result = await session.call_tool(tool_name, arguments=args)
                    log(f"  tool returned {len(result.content)} content block(s)")

                    for block in result.content:
                        text = getattr(block, "text", None) or ""
                        if not text:
                            continue

                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError:
                            # Plain text response — treat as a text-only ARTIFACT
                            queue.push({
                                "responseType": "FINAL_BUNDLE",
                                "artifact": {
                                    "responseType": "ARTIFACT",
                                    "payload": {"summary": text, "data": []},
                                },
                                "plan":        None,
                                "human_probe": None,
                            })
                            continue

                        final_items = data.get("items", [])
                        if not final_items:
                            # Bare JSON with no items[] wrapper — treat as ARTIFACT payload
                            queue.push({
                                "responseType": "FINAL_BUNDLE",
                                "artifact": {
                                    "responseType": "ARTIFACT",
                                    "payload": data,
                                },
                                "plan":        None,
                                "human_probe": None,
                            })
                            continue

                        # ── Parse the final items[] array ─────────────────────
                        # Any combination of ARTIFACT / PLAN / HUMAN_PROBE may be
                        # present.  Multiple ARTIFACTs are merged into one so their
                        # data[] arrays are preserved in order.
                        merged_data:    list           = []
                        merged_summary: str | None     = None
                        plan_item:      dict | None    = None
                        human_probe:    dict | None    = None

                        for fi in final_items:
                            rt = fi.get("responseType", "")
                            log(f"  final item: {rt}")
                            if rt == "ARTIFACT":
                                fp = fi.get("payload", {})
                                if fp.get("summary") and not merged_summary:
                                    merged_summary = fp["summary"]
                                merged_data.extend(fp.get("data", []))
                            elif rt == "PLAN":
                                plan_item = fi          # keep the full item (payload.plan)
                            elif rt == "HUMAN_PROBE":
                                human_probe = fi        # keep the full item (payload.probe_message)
                            # TOKEN / STREAM_STATUS → informational only, skip

                        merged_artifact = None
                        if merged_data or merged_summary:
                            merged_artifact = {
                                "responseType": "ARTIFACT",
                                "payload": {
                                    "summary": merged_summary,
                                    "data":    merged_data,
                                },
                            }

                        bundle = {
                            "responseType": "FINAL_BUNDLE",
                            "artifact":    merged_artifact,
                            "plan":        plan_item,
                            "human_probe": human_probe,
                        }
                        log(
                            f"  queuing FINAL_BUNDLE "
                            f"(artifact={'yes' if merged_artifact else 'no'}, "
                            f"artifact_items={len(merged_data)}, "
                            f"plan={'yes' if plan_item else 'no'}, "
                            f"probe={'yes' if human_probe else 'no'})"
                        )
                        queue.push(bundle)

            queue.finish()
            log("  background task finished cleanly")

        except asyncio.CancelledError:
            log("  background task cancelled")
            # Do NOT call queue.finish() — the new invocation already reset it
        except Exception as exc:
            log(f"  background task ERROR: {exc}")
            queue.finish(error=str(exc))

    # ── tools/list — live from CLAIRE + injected `working` ────────────────────
    async def _list_tools(self, msg_id) -> dict:
        from mcp import ClientSession
        try:
            await self._ensure_ready()
            async with self._sdk_session() as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    tools = [
                        {
                            "name":        t.name,
                            "description": t.description or "",
                            # mcp renamed inputSchema -> input_schema in newer versions
                            "inputSchema": getattr(t, "inputSchema", None)
                                           or getattr(t, "input_schema", None)
                                           or {},
                        }
                        for t in result.tools
                    ]
            log(f"tools/list: fetched {len(tools)} tool(s) from CLAIRE")
        except Exception as exc:
            log(f"tools/list ERROR: {exc!r} — returning empty tool list")
            # TaskGroup swallows the real cause in .exceptions — unwrap it
            for attr in ("exceptions", "__cause__", "__context__"):
                sub = getattr(exc, attr, None)
                if sub:
                    subs = sub if isinstance(sub, (list, tuple)) else [sub]
                    for s in subs:
                        log(f"tools/list SUB-EXCEPTION [{attr}]: {type(s).__name__}: {s!r}")
            import traceback as _tb
            log("tools/list TRACEBACK:\n" + _tb.format_exc())
            tools = []

        # Inject local tools: working + new_conversation
        tools.append(WORKING_TOOL_DEFINITION)
        tools.append(NEW_CONVERSATION_TOOL_DEFINITION)

        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}

    # ── tools/call dispatch ────────────────────────────────────────────────────
    async def _call_tool(self, message: dict, msg_id) -> dict:
        tool_name = message.get("params", {}).get("name", "")
        args      = dict(message.get("params", {}).get("arguments", {}))

        if tool_name == "new_conversation":
            return await self._handle_new_conversation(msg_id)

        if tool_name == "working":
            return await self._handle_working(msg_id)

        # Any other tool → forward to CLAIRE
        return await self._invoke_claire_tool(msg_id, tool_name, args)

    async def _handle_new_conversation(self, msg_id) -> dict:
        """
        Create a fresh Informatica conversation ID for this chat session.

        Called by Claude at the start of every new chat (see SKILL.md).
        Updates self.conv_id in place so all subsequent CLAIRE tool calls
        automatically use the new ID — no further action needed from Claude.
        """
        async with self._lock:
            await self._ensure_ready()
            loop         = asyncio.get_running_loop()
            new_conv_id  = await loop.run_in_executor(
                None, _create_conversation_sync, self.config, self.jwt_token
            )
            self.conv_id = new_conv_id
            cache        = load_cache()
            cache["conversation_id"] = new_conv_id
            save_cache(cache)
            log(f"new_conversation: created {new_conv_id}")

        return _text_response(
            msg_id, f"New conversation created: {self.conv_id}"
        )

    async def _invoke_claire_tool(
        self, msg_id, tool_name: str, args: dict
    ) -> dict:
        """
        Start a background task to call the CLAIRE tool.
        Cancel any previous task first so we don't leak connections.
        """
        async with self._lock:
            await self._ensure_ready()

        # Cancel previous task if still running
        if self._task and not self._task.done():
            self._task.cancel()
            log("Cancelled previous background task")
        if self._queue and not self._queue.is_done:
            self._queue.finish(error="cancelled by new request")

        # Inject required CLAIRE args
        args.pop("context", None)
        args.pop("conversationId", None)
        args.pop("conversation_id", None)
        args["conversation_id"] = self.conv_id
        args.setdefault("events", True)
        args.setdefault("reasoning", True)

        log(f"Invoking CLAIRE tool: {tool_name} | conv={self.conv_id[:8]}…")

        queue       = ResponseQueue()
        self._queue = queue
        self._task  = asyncio.ensure_future(
            self._run_claire_tool(queue, tool_name, args)
        )

        return {
            "jsonrpc": "2.0",
            "id":      msg_id,
            "result": {
                "content": [{
                    "type": "text",
                    "text": (
                        "CLAIRE is working on your request. "
                        "Call `working` to get progress and the answer."
                    ),
                }]
            },
        }

    async def _handle_working(self, msg_id) -> dict:
        """
        Drain the response queue cursor-style and format for Claude.

        Design goals
        ────────────
        1. Zero unnecessary latency — if items are already in the queue when
           `working` is called, return them immediately without waiting.
        2. No jitter on notification bursts — the settle window in
           ResponseQueue.wait() lets co-arriving notifications accumulate before
           we drain, so they are returned together in one call rather than
           trickling out one-per-poll.
        3. Progress/reasoning items are always returned BEFORE the FINAL_BUNDLE
           in the same drain batch, so Claude shows them to the user in order.

        Response types that may appear in the queue:
          EXECUTION_INSIGHT  → plain text (execution progress, display verbatim + keep polling)
          REASONING_TRACE    → plain text (reasoning thought, display verbatim + keep polling)
          FINAL_BUNDLE       → "ANSWER: <json>" (complete final answer, stop polling)
                               optionally prefixed with "PROBE: <question>" when a
                               HUMAN_PROBE is embedded in the bundle.

        Behaviour:
          • Fast-drains immediately if items are already queued (no wait needed).
          • Otherwise blocks up to 20 s for the next notification.
          • Returns "Still working…" on timeout (task still running).
          • Returns "done" when the task finished and the queue is empty.
          • Returns "Error: …" if the background task errored.
        """
        queue = self._queue

        if queue is None:
            return _text_response(msg_id, "No active CLAIRE request.")

        # ── Fast path: items already waiting — return immediately, no wait ────
        # This is the common case after the first notification arrives.
        # Avoids any sleep or event overhead on subsequent polls.
        if queue._cursor < len(queue._items) or queue.is_done:
            pass  # fall through to drain below
        else:
            # Nothing ready yet — block until something arrives or timeout
            ready = await queue.wait(timeout=20.0, settle_ms=10.0)
            if not ready:
                # Timeout — task still running, nothing arrived
                return _text_response(msg_id, "⏳ Still working…")

        # ── Drain all items accumulated since last call ────────────────────────
        items = queue.drain()

        if not items:
            # Queue drained to empty — check terminal states
            if queue.is_done:
                if queue.error:
                    return _text_response(msg_id, f"Error: {queue.error}")
                return _text_response(msg_id, "done")
            return _text_response(msg_id, "⏳ Still working…")

        # ── Format items in order ─────────────────────────────────────────────
        # CRITICAL RULE: ANSWER: must always be returned alone — never joined
        # with progress/reasoning lines in the same response. Reason: SKILL.md detects
        # the response type by its starting prefix. A response that starts with
        # progress text and has ANSWER: on line 2 will be treated entirely as progress
        # and the answer will be discarded.
        #
        # Strategy: split items into two buckets — progress/reasoning and final.
        # If both are present in the same drain batch, return progress now and
        # re-queue the final bundle so the next working() call returns it alone.
        progress_lines: list[str] = []
        final_part:     str | None = None

        for item in items:
            rt      = item.get("responseType", "")
            payload = item.get("payload", {})

            if rt == "EXECUTION_INSIGHT":
                text = payload.get("text", "")
                if text:
                    # Render as-is, one line per insight
                    progress_lines.append(text)

            elif rt == "REASONING_TRACE":
                thought = payload.get("thought", "")
                if thought:
                    # Render as-is, one line per thought
                    progress_lines.append(thought)

            elif rt == "FINAL_BUNDLE":
                # Build the ANSWER: string
                answer_lines: list[str] = []
                probe = item.get("human_probe")
                if probe:
                    question = probe.get("payload", {}).get("probe_message", "")
                    if question:
                        answer_lines.append(f"PROBE: {question}")
                answer_lines.append(f"ANSWER: {json.dumps(item)}")
                final_part = "\n".join(answer_lines)

            else:
                log(f"  working: skipping unknown responseType={rt}")

        if progress_lines and final_part is not None:
            # Both arrived in the same drain batch.
            # Return progress now; re-queue the FINAL_BUNDLE so the next
            # working() call returns it cleanly on its own.
            # We push directly back onto the queue items list at cursor position
            # by re-inserting a synthetic FINAL_BUNDLE marker that _handle_working
            # will pick up next call. Simplest: just stash it as a one-item list.
            self._queue._items.append(items[-1])   # re-queue the FINAL_BUNDLE item
            # cursor does NOT advance for the re-queued item (it was not in the
            # original drain slice, so cursor is already correct)
            log("  working: flushing progress before answer — FINAL_BUNDLE re-queued")
            return _text_response(msg_id, "\n".join(progress_lines))

        if progress_lines:
            return _text_response(msg_id, "\n".join(progress_lines))

        if final_part is not None:
            return _text_response(msg_id, final_part)

        # All drained items were skipped (unknown types) — heartbeat
        return _text_response(msg_id, "Still working…")

    # ── main dispatcher ────────────────────────────────────────────────────────
    async def forward(self, message: dict) -> dict:
        method = message.get("method", "")
        msg_id = message.get("id")

        if method == "initialize":
            return await self._handle_initialize(msg_id)

        if method == "tools/list":
            async with self._lock:
                await self._ensure_ready()
            return await self._list_tools(msg_id)

        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": []}}

        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}

        if method == "tools/call":
            return await self._call_tool(message, msg_id)

        if method.startswith("notifications/"):
            return {}   # no reply for notifications

        log(f"Unhandled method: {method}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    async def _handle_initialize(self, msg_id) -> dict:
        """
        On initialize, start a fresh conversation (user opened a new chat).
        """
        async with self._lock:
            loop  = asyncio.get_running_loop()
            cache = load_cache()

            # Ensure tokens are valid first
            if not _token_valid(cache):
                tokens = await loop.run_in_executor(None, _fetch_tokens_sync, self.config)
                self._apply_tokens(tokens)
                cache.update(tokens)
            else:
                self._apply_tokens(cache)

            # Always create a fresh conversation on initialize
            self.conv_id = await loop.run_in_executor(
                None, _create_conversation_sync, self.config, self.jwt_token
            )
            cache["conversation_id"] = self.conv_id
            save_cache(cache)
            log(f"New chat → conversation: {self.conv_id}")

        return {
            "jsonrpc": "2.0",
            "id":      msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities":    {"tools": {}, "prompts": {}, "resources": {}},
                "serverInfo":      {"name": "claire-idmc-agent-techsales", "version": "2.0.0"},
            },
        }


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════
def _text_response(msg_id, text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id":      msg_id,
        "result": {"content": [{"type": "text", "text": text}]},
    }


# ══════════════════════════════════════════════════════════════════════════════
# stdio MCP server loop
# ══════════════════════════════════════════════════════════════════════════════
async def run_server(proxy: MCPProxy) -> None:
    log("CLAIRE MCP proxy listening on stdin…")
    loop  = asyncio.get_running_loop()

    # Windows ProactorEventLoop can't connect_read_pipe() to real stdio;
    # use a background thread + asyncio.Queue to bridge stdin into the loop.
    queue: asyncio.Queue = asyncio.Queue()
    stdin_buf = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin

    def _reader_thread() -> None:
        try:
            for raw in iter(stdin_buf.readline, b""):
                loop.call_soon_threadsafe(queue.put_nowait, raw)
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, ("ERR", exc))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    import threading
    threading.Thread(target=_reader_thread, daemon=True).start()

    while True:
        item = await queue.get()
        if item is None:
            log("stdin closed — exiting.")
            break
        if isinstance(item, tuple) and item and item[0] == "ERR":
            log(f"stdin read error: {item[1]}")
            break
        line_bytes = item

        line = line_bytes.decode("utf-8", errors="replace").strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            log(f"JSON parse error: {exc}")
            continue

        method          = message.get("method", "")
        msg_id          = message.get("id")
        is_notification = msg_id is None

        log(f"→ {method} (id={msg_id})")
        asyncio.ensure_future(
            _handle_message(proxy, message, method, msg_id, is_notification)
        )


async def _handle_message(
    proxy: MCPProxy,
    message: dict,
    method: str,
    msg_id,
    is_notification: bool,
) -> None:
    try:
        response = await proxy.forward(message)

        if is_notification or not response:
            return

        if isinstance(response, dict):
            response.setdefault("id",      msg_id)
            response.setdefault("jsonrpc", "2.0")

        output = json.dumps(response)

    except Exception as exc:
        log(f"ERROR handling {method}: {exc}")
        if is_notification:
            return
        output = json.dumps({
            "jsonrpc": "2.0",
            "id":      msg_id,
            "error":   {"code": -32603, "message": str(exc)},
        })

    sys.stdout.write(output + "\n")
    log(f"← replied to {method}")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════
async def main() -> None:
    _check_deps()
    config = load_config()
    proxy  = MCPProxy(config)
    await proxy.startup()
    await run_server(proxy)


if __name__ == "__main__":
    asyncio.run(main())