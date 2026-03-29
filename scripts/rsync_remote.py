#!/usr/bin/env python3
"""
Continuously rsync the local repo to a remote instance.

Uses the SSH host alias created by configure_remote_ssh.py, or discovers
the instance via RunPod API / manual SSH tokens.

Examples:

    # Default: use the SSH alias from configure_remote_ssh.py
    uv run scripts/rsync_remote.py

    # Auto-discover RunPod instance:
    uv run scripts/rsync_remote.py --runpod --identity ~/.ssh/key

    # Explicit SSH parameters:
    uv run scripts/rsync_remote.py -- ssh root@1.2.3.4 -p 22048 -i ~/.ssh/key
"""

from __future__ import annotations

import argparse
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

sys.path.insert(0, str(SCRIPT_DIR))
from configure_remote_ssh import (
    _expand,
    fetch_runpod_ssh,
    parse_ssh_provider_args,
    runpod_api_key,
)

DEFAULT_SSH_ALIAS = "build-nanogpt-remote"
DEFAULT_REMOTE_PATH = "~/build-nanogpt"
DEFAULT_INTERVAL = 1.0

SSH_CONTROL_PATH = "/tmp/rsync-build-nanogpt-%r@%h:%p"
SSH_CONTROL_PERSIST = "300"


def load_excludes(repo_root: Path) -> list[str]:
    """Read .gitignore and always exclude .git/."""
    excludes = [".git/"]
    gitignore = repo_root / ".gitignore"
    if gitignore.is_file():
        for line in gitignore.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                excludes.append(line)
    return excludes


def build_ssh_cmd_str(
    *,
    port: str | None = None,
    identity: str | None = None,
) -> str:
    parts = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={SSH_CONTROL_PATH}",
        "-o", f"ControlPersist={SSH_CONTROL_PERSIST}",
    ]
    if port:
        parts.extend(["-p", port])
    if identity:
        parts.extend(["-i", identity])
    return " ".join(parts)


def build_rsync_cmd(
    target: str,
    local_path: Path,
    excludes: list[str],
    *,
    ssh_cmd: str,
    verbose: bool = False,
) -> list[str]:
    cmd = ["rsync", "-az", "--delete"]
    if verbose:
        cmd.extend(["-v", "--progress"])
    else:
        cmd.extend(["--out-format", "%n"])
    for exc in excludes:
        cmd.extend(["--exclude", exc])
    cmd.extend(["-e", ssh_cmd])
    cmd.append(str(local_path).rstrip("/") + "/")
    cmd.append(target)
    return cmd


def resolve_target(args: argparse.Namespace) -> tuple[str, str, str]:
    """Return (rsync_target, ssh_cmd_string, ssh_host_for_commands)."""
    tokens = list(args.ssh_tokens)
    if tokens and tokens[0] == "--":
        tokens = tokens[1:]

    remote_path = args.remote_path.rstrip("/")

    if tokens:
        dest, port, identity = parse_ssh_provider_args(tokens)
        ssh_cmd = build_ssh_cmd_str(port=port, identity=identity)
        return f"{dest}:{remote_path}/", ssh_cmd, dest

    if args.runpod:
        api_key = runpod_api_key(args.runpod_api_key)
        if not api_key:
            print(
                "error: --runpod requires RUNPOD_API_KEY env or --runpod-api-key",
                file=sys.stderr,
            )
            sys.exit(1)
        dest, port, summary = fetch_runpod_ssh(
            api_key,
            pod_id=args.runpod_pod_id,
            pod_name=args.runpod_name,
            default_user=args.runpod_user,
        )
        print(summary, file=sys.stderr)
        identity = _expand(args.identity) if args.identity else None
        ssh_cmd = build_ssh_cmd_str(port=port, identity=identity)
        return f"{dest}:{remote_path}/", ssh_cmd, dest

    ssh_cmd = build_ssh_cmd_str()
    return f"{args.ssh_host_alias}:{remote_path}/", ssh_cmd, args.ssh_host_alias


def ensure_remote_rsync(ssh_cmd_str: str, ssh_host: str) -> None:
    """Install rsync on the remote if missing."""
    ssh_parts = ssh_cmd_str.split()
    check_cmd = [*ssh_parts, ssh_host, "command -v rsync"]
    result = subprocess.run(check_cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return
    print("Installing rsync on remote...")
    install_cmd = [
        *ssh_parts, ssh_host,
        "apt-get update -qq && apt-get install -y -qq rsync",
    ]
    ret = subprocess.run(install_cmd).returncode
    if ret != 0:
        print("error: failed to install rsync on remote", file=sys.stderr)
        sys.exit(1)
    print("rsync installed on remote.\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Continuously rsync local repo to a remote instance.",
    )
    parser.add_argument(
        "ssh_tokens",
        nargs=argparse.REMAINDER,
        help='SSH line, e.g. "ssh root@1.2.3.4 -p 22048 -i ~/.ssh/key"',
    )
    parser.add_argument(
        "--ssh-host-alias",
        default=DEFAULT_SSH_ALIAS,
        metavar="NAME",
        help=f"SSH config alias (default: {DEFAULT_SSH_ALIAS})",
    )
    parser.add_argument(
        "--remote-path",
        default=DEFAULT_REMOTE_PATH,
        metavar="PATH",
        help=f"Remote directory (default: {DEFAULT_REMOTE_PATH})",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        metavar="SECS",
        help=f"Seconds between syncs (default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--runpod",
        action="store_true",
        help="Auto-discover pod via RunPod API",
    )
    parser.add_argument("--runpod-api-key", default=None, metavar="KEY")
    parser.add_argument("--runpod-pod-id", default=None, metavar="ID")
    parser.add_argument("--runpod-name", default=None, metavar="NAME")
    parser.add_argument("--runpod-user", default="root", metavar="USER")
    parser.add_argument(
        "--identity",
        default=None,
        metavar="PATH",
        help="SSH private key (for --runpod mode)",
    )
    args = parser.parse_args()

    if not shutil.which("rsync"):
        print("error: rsync not found in PATH", file=sys.stderr)
        return 1

    try:
        target, ssh_cmd, ssh_host = resolve_target(args)
    except (RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    excludes = load_excludes(REPO_ROOT)

    print(f"  source:   {REPO_ROOT}")
    print(f"  target:   {target}")
    print(f"  interval: {args.interval}s")
    print()

    ensure_remote_rsync(ssh_cmd, ssh_host)

    cmd_verbose = build_rsync_cmd(
        target, REPO_ROOT, excludes, ssh_cmd=ssh_cmd, verbose=True,
    )
    print("Initial sync...")
    ret = subprocess.run(cmd_verbose).returncode
    if ret != 0:
        print(f"\nerror: initial rsync failed (exit {ret})", file=sys.stderr)
        return ret
    print("\nWatching for changes (Ctrl+C to stop)...\n")

    cmd_quiet = build_rsync_cmd(
        target, REPO_ROOT, excludes, ssh_cmd=ssh_cmd,
    )

    stop = False

    def handle_signal(sig, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while not stop:
        time.sleep(args.interval)
        if stop:
            break
        result = subprocess.run(cmd_quiet, capture_output=True, text=True)
        if result.returncode != 0:
            ts = time.strftime("%H:%M:%S")
            stderr = result.stderr.strip()
            if "connection unexpectedly closed" in stderr or "Connection closed" in stderr:
                print(f"[{ts}] Connection lost, retrying...", file=sys.stderr)
            else:
                print(f"[{ts}] rsync error ({result.returncode}): {stderr}", file=sys.stderr)
            continue
        changed = [l for l in result.stdout.strip().splitlines() if l.strip()]
        if changed:
            ts = time.strftime("%H:%M:%S")
            if len(changed) <= 5:
                print(f"[{ts}] Synced: {', '.join(changed)}")
            else:
                print(f"[{ts}] Synced {len(changed)} files")

    print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
