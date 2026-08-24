#!/usr/bin/env python3
"""
idmc_http_mcp_proxy.py — generic stdio→HTTP MCP proxy with IDMC auto-auth.

Wraps any Informatica IDMC HTTP MCP endpoint behind a stdio MCP server, so
Claude Code can use it without manually pasting expiring Bearer JWTs into
.claude.json. Mints a fresh JWT (Login → /jwt/Token) on startup and refreshes
when the cached token nears expiry.

Usage (registered in .claude.json mcpServers):

    {
      "command": ".../python.exe",
      "args": [
        "-u", ".../scripts/idmc_http_mcp_proxy.py",
        "--upstream", "https://qa-pod1-a2e-mcp.rel.infaqa.com/mcp-servers/public/cdgcsearchmetadata",
        "--name",     "cdgc-search-metadata"
      ],
      "env": { "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8" }
    }

Credentials come from claude_desktop_config.json → "claireIDMCAgent" block,
so this proxy reuses the same creds as claire_mcp.py — no second copy.
Token cache is per-upstream (.cache_<name>.json) so multiple proxy instances
don't clobber each other.
"""

import argparse
import asyncio
import json
import os
import ssl
import sys
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

# UTF-8 stdio (Windows cp1252 would otherwise corrupt JSON-RPC frames)
sys.stdout = open(sys.stdout.fileno(), "w", buffering=1, encoding="utf-8", closefd=False)
sys.stderr = open(sys.stderr.fileno(), "w", buffering=1, encoding="utf-8", closefd=False)

_SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
TOKEN_LIFETIME_MIN = 55
REFRESH_BUFFER_MIN = 5

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode    = ssl.CERT_NONE

_LOG_PREFIX = "idmc-proxy"


def log(msg: str) -> None:
    print(f"[{_LOG_PREFIX}] {msg}", file=sys.stderr, flush=True)


def _config_path() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude_desktop_config.json")
    return os.path.expanduser("~/.config/Claude/claude_desktop_config.json")


def load_config(block_name: str = "claireIDMCAgent") -> dict:
    path = _config_path()
    if not os.path.exists(path):
        log(f"FATAL: config not found at {path}")
        sys.exit(1)
    with open(path) as fh:
        cfg = json.load(fh)
    block = cfg.get(block_name)
    if not block:
        log(f"FATAL: '{block_name}' block missing from claude_desktop_config.json")
        sys.exit(1)
    return block


def _http(url: str, method: str = "POST", data: dict | None = None,
          headers: dict | None = None) -> tuple[str, int]:
    hdrs = dict(headers or {})
    hdrs.setdefault("Content-Type", "application/json")
    payload = json.dumps(data).encode() if data else b""
    req = urllib.request.Request(
        url, data=payload if method != "GET" else None, headers=hdrs, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL) as resp:
            return resp.read().decode(), resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        log(f"HTTP {exc.code} {exc.reason} → {url}\n  body: {body[:300]}")
        raise RuntimeError(f"HTTP {exc.code}: {body[:200]}")


def _fetch_tokens_sync(config: dict, auth_mode: str = "jwt") -> dict:
    base = config["identity_url"].rstrip("/")
    body, _ = _http(
        f"{base}/api/v1/Login",
        data={"username": config["username"], "password": config["password"]},
    )
    login = json.loads(body)
    sid = (login.get("userInfo", {}).get("sessionId")
           or login.get("sessionId") or login.get("session_id"))
    if not sid:
        raise RuntimeError(f"No session_id in login response: {login}")

    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=TOKEN_LIFETIME_MIN)).isoformat()

    if auth_mode == "session-only":
        log("Session refreshed (session-only mode, no JWT).")
        return {"session_id": sid, "jwt_token": "", "expires_at": expires_at}

    nonce = uuid.uuid4().hex
    body, _ = _http(
        f"{base}/api/v1/jwt/Token?client_id={config['client_id']}&nonce={nonce}",
        headers={"IDS-SESSION-ID": sid, "Content-Type": "application/json"},
    )
    jwt_token = json.loads(body).get("jwt_token")
    if not jwt_token:
        raise RuntimeError("No jwt_token in JWT response")

    log("Tokens refreshed.")
    return {"session_id": sid, "jwt_token": jwt_token, "expires_at": expires_at}


def _token_valid(cache: dict, auth_mode: str = "jwt") -> bool:
    if not cache.get("session_id"):
        return False
    if auth_mode == "jwt" and not cache.get("jwt_token"):
        return False
    try:
        expiry = datetime.fromisoformat(cache["expires_at"].replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < (expiry - timedelta(minutes=REFRESH_BUFFER_MIN))
    except (ValueError, TypeError, KeyError):
        return False


def _sanitize_schema(schema: dict) -> tuple[dict, list[str]]:
    """Drop `required` entries that aren't defined in `properties`.

    Why: cdgcsearchmetadata advertises `required: [knowledgeQuery, segments]`
    but only defines `knowledgeQuery` in properties — the SDK refuses to let
    the caller supply `segments`, yet the upstream errors without it. We strip
    the orphan from the schema so Claude doesn't see it, and return the list of
    orphan keys so the proxy can inject empty defaults when calling upstream.

    Returns (cleaned_schema, orphan_keys).
    """
    if not isinstance(schema, dict):
        return schema, []
    props    = schema.get("properties") or {}
    required = schema.get("required")
    if not isinstance(required, list):
        return schema, []
    cleaned = [k for k in required if k in props]
    orphans = [k for k in required if k not in props]
    if orphans:
        log(f"sanitize_schema: dropping orphan required keys {orphans}")
        schema = dict(schema)
        schema["required"] = cleaned
    return schema, orphans


class Proxy:
    def __init__(self, upstream: str, cache_path: str, config: dict, auth_mode: str = "jwt") -> None:
        self.upstream    = upstream
        self.cache_path  = cache_path
        self.config      = config
        self.auth_mode   = auth_mode
        self.jwt_token:   str | None = None
        self.ids_session: str | None = None
        self._lock = asyncio.Lock()
        self._orphan_defaults: dict[str, dict] = {}  # tool_name -> {orphan_key: default}

    def _load_cache(self) -> dict:
        if not os.path.exists(self.cache_path):
            return {}
        try:
            with open(self.cache_path) as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_cache(self, cache: dict) -> None:
        with open(self.cache_path, "w") as fh:
            json.dump(cache, fh, indent=2)

    async def ensure_token(self, force: bool = False) -> None:
        async with self._lock:
            cache = self._load_cache()
            if force or not _token_valid(cache, self.auth_mode):
                loop = asyncio.get_running_loop()
                tokens = await loop.run_in_executor(None, _fetch_tokens_sync, self.config, self.auth_mode)
                cache.update(tokens)
                self._save_cache(cache)
            self.jwt_token   = cache.get("jwt_token", "")
            self.ids_session = cache["session_id"]

    def _auth_headers(self) -> dict:
        h = {"IDS-SESSION-ID": self.ids_session,
             "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.auth_mode == "jwt" and self.jwt_token:
            h["Authorization"] = f"Bearer {self.jwt_token}"
        return h

    async def _jsonrpc(self, method: str, params: dict) -> dict:
        """Send a single JSON-RPC 2.0 request directly via httpx (no MCP SDK transport)."""
        import httpx
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with httpx.AsyncClient(verify=False,
                                     timeout=httpx.Timeout(connect=30.0, read=300.0,
                                                           write=30.0, pool=10.0)) as client:
            resp = await client.post(self.upstream, headers=self._auth_headers(),
                                     json=payload)
            if resp.status_code in (401, 403):
                raise RuntimeError(f"HTTP {resp.status_code}: auth failure")
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"JSON-RPC error: {data['error']}")
            return data.get("result", {})

    async def _call_with_retry(self, method: str, params: dict) -> dict:
        try:
            return await self._jsonrpc(method, params)
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "401" in msg or "403" in msg or "auth" in msg:
                log(f"Auth failure ({exc}); refreshing token and retrying.")
                await self.ensure_token(force=True)
                return await self._jsonrpc(method, params)
            raise

    async def list_tools(self) -> list[dict]:
        result = await self._call_with_retry("tools/list", {})
        tools = result.get("tools", [])
        out = []
        for t in tools:
            schema, orphans = _sanitize_schema(t.get("inputSchema") or {})
            if orphans:
                # segments is declared required by upstream but not in properties.
                # Pass [] to satisfy the required check; upstream may still reject
                # it with -32602 if the server is buggy — see call_tool retry logic.
                self._orphan_defaults[t["name"]] = {k: [] for k in orphans}
            out.append({"name": t["name"], "description": t.get("description", ""),
                        "inputSchema": schema})
        return out

    async def call_tool(self, name: str, args: dict) -> list[dict]:
        # Inject orphan defaults (e.g. segments=[]) required by upstream schema.
        # If upstream rejects them with -32602, retry without them and clear the
        # orphan record so future calls don't re-inject.
        if name in self._orphan_defaults:
            merged = dict(self._orphan_defaults[name])
            merged.update(args)
            args_with_orphans = merged
        else:
            args_with_orphans = args

        try:
            result = await self._call_with_retry("tools/call", {"name": name, "arguments": args_with_orphans})
        except RuntimeError as exc:
            if "-32602" in str(exc) and name in self._orphan_defaults:
                log(f"call_tool: upstream rejected orphan args for '{name}', retrying without them.")
                del self._orphan_defaults[name]
                result = await self._call_with_retry("tools/call", {"name": name, "arguments": args})
            else:
                raise
        blocks = []
        for b in result.get("content", []):
            if isinstance(b, dict) and b.get("type") == "text":
                blocks.append({"type": "text", "text": b.get("text", "")})
            elif isinstance(b, str):
                blocks.append({"type": "text", "text": b})
        return blocks


# ── stdio MCP server loop ────────────────────────────────────────────────────
async def serve(proxy: Proxy, server_name: str) -> None:
    await proxy.ensure_token()
    log(f"Proxy ready → {proxy.upstream}")

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stdin_buf = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin

    def _reader_thread() -> None:
        try:
            for raw in iter(stdin_buf.readline, b""):
                loop.call_soon_threadsafe(queue.put_nowait, raw)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=_reader_thread, daemon=True).start()

    while True:
        item = await queue.get()
        if item is None:
            log("stdin closed — exiting.")
            return
        line = item.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            log(f"JSON parse error: {exc}")
            continue
        asyncio.ensure_future(_dispatch(proxy, server_name, message))


async def _dispatch(proxy: Proxy, server_name: str, message: dict) -> None:
    method = message.get("method", "")
    msg_id = message.get("id")
    is_notification = msg_id is None

    try:
        response = await _handle(proxy, server_name, method, msg_id, message)
        if is_notification or response is None:
            return
        sys.stdout.write(json.dumps(response) + "\n")
    except Exception as exc:
        log(f"ERROR handling {method}: {exc}")
        if is_notification:
            return
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32603, "message": str(exc)},
        }) + "\n")


async def _handle(proxy: Proxy, server_name: str, method: str, msg_id, message: dict):
    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities":    {"tools": {}, "prompts": {}, "resources": {}},
                "serverInfo":      {"name": server_name, "version": "1.0.0"},
            },
        }
    if method == "tools/list":
        tools = await proxy.list_tools()
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}
    if method == "tools/call":
        params = message.get("params", {})
        blocks = await proxy.call_tool(params.get("name", ""), dict(params.get("arguments", {})))
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": blocks}}
    if method in ("prompts/list", "resources/list"):
        key = method.split("/")[0]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {key: []}}
    if method.startswith("notifications/"):
        return None
    log(f"Unhandled method: {method}")
    return {"jsonrpc": "2.0", "id": msg_id, "result": {}}


def main() -> None:
    global _LOG_PREFIX
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True, help="Upstream HTTP MCP URL")
    parser.add_argument("--name",     required=True, help="Server name (used for cache + logs)")
    parser.add_argument("--auth-mode", choices=["jwt", "session-only"], default="jwt",
                        help="jwt: send Bearer JWT + IDS-SESSION-ID (QA); session-only: IDS-SESSION-ID only (prod)")
    parser.add_argument("--config-block", default="claireIDMCAgent",
                        help="Top-level key in claude_desktop_config.json holding identity_url/username/password/client_id")
    parser.add_argument("--identity-url", help="Override identity service base URL")
    parser.add_argument("--username",     help="Override username")
    parser.add_argument("--password",     help="Override password")
    parser.add_argument("--client-id",    default="cdlg_app", help="Client ID for JWT mint")
    args = parser.parse_args()

    _LOG_PREFIX = f"idmc-proxy/{args.name}"
    cache_path = os.path.join(_SCRIPT_DIR, f".cache_{args.name}.json")

    if args.identity_url and args.username and args.password:
        config = {
            "identity_url": args.identity_url,
            "username":     args.username,
            "password":     args.password,
            "client_id":    args.client_id,
        }
    else:
        config = load_config(args.config_block)

    proxy = Proxy(args.upstream, cache_path, config, auth_mode=args.auth_mode)
    asyncio.run(serve(proxy, args.name))


if __name__ == "__main__":
    main()
