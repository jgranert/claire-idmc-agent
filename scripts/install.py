#!/usr/bin/env python3
"""
install.py — Installer for the CLAIRE MCP skill.

HOW TO RUN (3 steps, that's it):
  1. In Claude Desktop go to Settings → Skills and upload claire-idmc-agent.zip
  2. Open Terminal and run:

        python3 ~/Downloads/claire-idmc-agent/scripts/install.py

     Or pass credentials upfront:
        python3 ~/Downloads/claire-idmc-agent/scripts/install.py \
            --username me@company.com --password secret

  3. Restart Claude Desktop — CLAIRE tools will appear automatically.

Windows: use `python` instead of `python3`.

No system-wide pip install required. This script creates a self-contained
virtual environment inside the skill folder and installs only what is needed.
"""

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import venv
from datetime import datetime

# ── Hardcoded config (non-credential) ─────────────────────────────────────────
IDENTITY_URL   = "https://qa-ma.rel.infaqa.com/identity-service"
CLIENT_ID      = "cdlg_app"
CLAIRE_API_URL = "https://claire-gpt-api.rel.infaqa.com"

# ── Skill folder name as it lives inside Claude Desktop's skills directory ─────
SKILL_FOLDER_NAME = "claire-idmc-agent"

MCP_PACKAGES = ["mcp"]


# ── Platform paths ────────────────────────────────────────────────────────────
def get_config_path() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Claude/claude_desktop_config.json"
        )
    elif sys.platform == "win32":
        return os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")),
            "Claude", "claude_desktop_config.json",
        )
    else:
        return os.path.expanduser("~/.config/Claude/claude_desktop_config.json")


def get_skills_root() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin"
        )
    elif sys.platform == "win32":
        return os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")),
            "Claude", "local-agent-mode-sessions", "skills-plugin",
        )
    else:
        return os.path.expanduser(
            "~/.config/Claude/local-agent-mode-sessions/skills-plugin"
        )


# ── Find deployed claire_mcp.py ───────────────────────────────────────────────
def find_deployed_proxy() -> str | None:
    root = get_skills_root()
    if not os.path.isdir(root):
        return None

    # Primary: exact skill folder name match
    # (.../skills/claire-idmc-agent/scripts/claire_mcp.py)
    for dirpath, _, filenames in os.walk(root):
        if (
            os.path.basename(dirpath) == "scripts"
            and os.path.basename(os.path.dirname(dirpath)) == SKILL_FOLDER_NAME
            and "claire_mcp.py" in filenames
        ):
            return os.path.join(dirpath, "claire_mcp.py")

    # Fallback: find claire_mcp.py anywhere under skills-plugin
    # (handles cases where Claude Desktop uses a different folder name)
    for dirpath, _, filenames in os.walk(root):
        if "claire_mcp.py" in filenames:
            candidate = os.path.join(dirpath, "claire_mcp.py")
            print(f"  ⚠  Exact folder match failed — found at:\n       {candidate}")
            return candidate

    return None


# ── venv Python executable path (cross-platform) ─────────────────────────────
def venv_python(venv_dir: str) -> str:
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


# ── Helpers ────────────────────────────────────────────────────────────────────
def sep(title: str = ""):
    w = 60
    print(f"\n{'─' * w}")
    if title:
        print(f"  {title}")
        print(f"{'─' * w}")


def ok(msg):   print(f"  ✓  {msg}")
def warn(msg): print(f"  ⚠  {msg}")
def err(msg):  print(f"  ✗  {msg}")
def info(msg): print(f"  →  {msg}")


def backup(path: str):
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = f"{path}.backup_{ts}"
    shutil.copy2(path, dest)
    ok(f"Backed up → {os.path.basename(dest)}")


# ── Argument parsing ──────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("--username", default=None, help="Informatica username")
    p.add_argument("--password", default=None, help="Informatica password")
    return p.parse_args()


def collect_credentials(args) -> tuple[str, str]:
    username = args.username
    password = args.password
    if not username:
        print()
        username = input("  Enter your Informatica username: ").strip()
        if not username:
            err("Username cannot be empty.")
            sys.exit(1)
    if not password:
        password = getpass.getpass("  Enter your Informatica password: ")
        if not password:
            err("Password cannot be empty.")
            sys.exit(1)
    return username, password


# ── venv creation + pip install ───────────────────────────────────────────────
def create_venv(venv_dir: str) -> str:
    """
    Creates a fresh venv at venv_dir and installs MCP_PACKAGES into it.
    Returns the path to the venv's Python executable.
    """
    if os.path.isdir(venv_dir):
        warn("Existing .venv found — removing and recreating...")
        shutil.rmtree(venv_dir)

    info(f"Creating venv at: {venv_dir}")
    venv.create(venv_dir, with_pip=True, clear=True)
    python_exe = venv_python(venv_dir)

    if not os.path.exists(python_exe):
        err(f"venv creation failed — Python not found at {python_exe}")
        sys.exit(1)
    ok(f"venv created → {venv_dir}")

    pip_env = {**os.environ, "PIP_CONFIG_FILE": "/dev/null"}
    if sys.platform == "win32":
        pip_env["PIP_CONFIG_FILE"] = "nul"

    pip_base = [
        python_exe, "-m", "pip", "install",
        "--index-url", "https://pypi.org/simple",
        "--trusted-host", "pypi.org",
    ]

    subprocess.run(
        pip_base + ["--quiet", "--upgrade", "pip"],
        env=pip_env, check=False
    )

    info("Installing mcp and dependencies into venv...")
    result = subprocess.run(
        pip_base + MCP_PACKAGES,
        env=pip_env,
        capture_output=True,
        text=True,
    )
    for line in result.stderr.splitlines():
        if line.startswith("ERROR"):
            err(f"  {line}")
    if result.returncode != 0:
        print()
        err("pip install failed.")
        print()
        print("  You can install manually inside the venv:")
        print(f"    {python_exe} -m pip install mcp")
        sys.exit(1)
    ok("mcp and all dependencies installed")

    info("Verifying imports...")
    check = subprocess.run(
        [python_exe, "-c",
         "from mcp import ClientSession; "
         "from mcp.client.streamable_http import streamable_http_client; "
         "print('ok')"],
        capture_output=True, text=True,
    )
    if check.returncode != 0 or check.stdout.strip() != "ok":
        err("Import verification failed:")
        print(check.stderr)
        print()
        print("  The venv is missing a required package.")
        print(f"  Re-run: python3 claire-idmc-agent/scripts/install.py")
        sys.exit(1)
    ok("Import verification passed — mcp client ready")

    return python_exe


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║              CLAIRE MCP Skill — Installer                   ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    args = parse_args()

    # ── Step 1: credentials ───────────────────────────────────────────────────
    sep("Step 1 — Credentials")
    username, password = collect_credentials(args)
    ok(f"Username : {username}")
    ok("Password : ••••••••")

    # ── Step 2: locate deployed skill ─────────────────────────────────────────
    sep("Step 2 — Locating deployed skill")
    proxy_path = find_deployed_proxy()

    if not proxy_path:
        err("Could not find claire_mcp.py inside Claude Desktop's skills folder.")
        print()
        print("  Please:")
        print("    1. Open Claude Desktop")
        print("    2. Go to Settings → Skills")
        print("    3. Upload claire-idmc-agent.zip")
        print("    4. Run this installer again")
        sys.exit(1)

    skill_root = os.path.dirname(os.path.dirname(proxy_path))  # claire-idmc-agent/
    venv_dir   = os.path.join(skill_root, ".venv")
    ok(f"Found skill at : {proxy_path}")

    # ── Step 3: create venv + install deps ────────────────────────────────────
    sep("Step 3 — Setting up Python environment")
    python_exe = create_venv(venv_dir)
    ok(f"venv Python    : {python_exe}")

    # ── Step 4: locate / create config ────────────────────────────────────────
    sep("Step 4 — Locating Claude Desktop config")
    config_path = get_config_path()

    if not os.path.exists(config_path):
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as f:
            json.dump({}, f, indent=2)
        ok(f"Created new config at: {config_path}")
    else:
        ok(f"Config found at: {config_path}")

    # ── Step 5: load & validate JSON ──────────────────────────────────────────
    with open(config_path, encoding="utf-8") as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError as e:
            err(f"Invalid JSON in config: {e}")
            print("  Please fix the JSON manually and re-run.")
            sys.exit(1)

    # ── Step 6: backup ────────────────────────────────────────────────────────
    sep("Step 5 — Backing up config")
    backup(config_path)

    # ── Step 7: register MCP server using the VENV Python ─────────────────────
    sep("Step 6 — Registering MCP server")
    was_existing = "claire-idmc-agent" in config.get("mcpServers", {})

    config.setdefault("mcpServers", {})
    config["mcpServers"]["claire-idmc-agent"] = {
        "command": python_exe,         # <-- venv Python, not system Python
        "args":    ["-u", proxy_path], # -u = force unbuffered stdout/stderr
        "env":     {"PYTHONUNBUFFERED": "1"},
    }

    ok(f"{'Updated' if was_existing else 'Added'} claire-idmc-agent entry")
    ok(f"command : {python_exe}")
    ok(f"script  : {proxy_path}")

    # ── Step 8: write credentials ─────────────────────────────────────────────
    sep("Step 7 — Writing credentials to config")
    config["claireIDMCAgent"] = {
        "username":       username,
        "password":       password,
        "identity_url":   IDENTITY_URL,
        "client_id":      CLIENT_ID,
        "claire_api_url": CLAIRE_API_URL,
    }
    ok("claireIDMCAgent block written")

    config.setdefault("preferences", {})

    # ── Step 9: save ──────────────────────────────────────────────────────────
    sep("Step 8 — Saving config")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    ok(f"Saved → {config_path}")

    # ── Done ──────────────────────────────────────────────────────────────────
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║                 Installation complete ✓                     ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()
    print("  Next step : Restart Claude Desktop")
    print("  Then      : CLAIRE tools will appear automatically ✨")
    print()


if __name__ == "__main__":
    main()