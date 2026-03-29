#!/usr/bin/env python3
"""
Configure local ~/.ssh/config and prepare a remote host for this repo.

**RunPod (default when no SSH line is given):** set RUNPOD_API_KEY and run with no
positional arguments. The script calls https://rest.runpod.io/v1 to find your running
pod and uses its public IP and SSH port mapping.

  export RUNPOD_API_KEY=...
  uv run scripts/configure_remote_ssh.py --identity ~/.ssh/your_key

**Manual provider string:** pass the SSH line from any provider, for example:

  uv run scripts/configure_remote_ssh.py ssh root@69.30.85.59 -p 22048 -i ~/.ssh/your_key

Options can appear before or after the destination, matching common provider strings.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests

RUNPOD_REST_BASE = "https://rest.runpod.io/v1"


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def parse_ssh_provider_args(tokens: list[str]) -> tuple[str, str | None, str | None]:
    """
    Parse tokens like: ssh user@host -p PORT -i KEY
    or: ssh -p PORT -i KEY user@host
    Returns (destination, port or None, identity path or None).
    Destination may be user@host or bare hostname/IP.
    """
    if not tokens:
        raise ValueError("missing SSH arguments (need at least user@host or host)")

    if tokens[0] == "ssh":
        tokens = tokens[1:]

    port: int | None = None
    identity: str | None = None
    dest: str | None = None
    i = 0
    n = len(tokens)

    def take_port() -> None:
        nonlocal i, port
        if i + 1 >= n:
            raise ValueError("-p requires a port number")
        port = int(tokens[i + 1])
        i += 2

    def take_identity() -> None:
        nonlocal i, identity
        if i + 1 >= n:
            raise ValueError("-i requires a path to an identity file")
        identity = _expand(tokens[i + 1])
        i += 2

    while i < n:
        t = tokens[i]
        if t == "-p":
            take_port()
        elif t == "-i":
            take_identity()
        elif t == "-o" and i + 1 < n:
            i += 2
        elif t.startswith("-"):
            raise ValueError(f"unsupported SSH option {t!r} (only -p, -i, -o are handled)")
        else:
            dest = t
            i += 1
            break

    while i < n:
        t = tokens[i]
        if t == "-p":
            take_port()
        elif t == "-i":
            take_identity()
        elif t == "-o" and i + 1 < n:
            i += 2
        elif t.startswith("-"):
            raise ValueError(f"unsupported SSH option {t!r}")
        else:
            raise ValueError(f"unexpected argument after destination: {t!r}")

    if dest is None:
        raise ValueError("missing destination (user@host or host)")

    return dest, (str(port) if port is not None else None), identity


def runpod_api_key(explicit: str | None) -> str | None:
    if explicit:
        return explicit.strip()
    k = os.environ.get("RUNPOD_API_KEY", "").strip()
    return k if k else None


def fetch_runpod_ssh(
    api_key: str,
    *,
    pod_id: str | None = None,
    pod_name: str | None = None,
    default_user: str = "root",
) -> tuple[str, str | None, str]:
    """
    Call RunPod REST API (GET /pods) and return (destination, port, summary line).

    destination is user@host for ~/.ssh/config; port is the public SSH port when mapped.
    """
    url = f"{RUNPOD_REST_BASE}/pods"
    headers = {"Authorization": f"Bearer {api_key}"}
    params: dict[str, str | int] = {"desiredStatus": "RUNNING"}
    if pod_id:
        params["id"] = pod_id
    try:
        r = requests.get(url, headers=headers, params=params, timeout=60)
    except requests.RequestException as e:
        raise RuntimeError(f"RunPod API request failed: {e}") from e
    if r.status_code == 401:
        raise RuntimeError("RunPod API rejected the key (401). Check RUNPOD_API_KEY.")
    if r.status_code == 403:
        raise RuntimeError("RunPod API forbidden (403). Check API key permissions.")
    if not r.ok:
        raise RuntimeError(
            f"RunPod API error {r.status_code}: {r.text[:500] if r.text else '(no body)'}"
        )
    try:
        pods = r.json()
    except ValueError as e:
        raise RuntimeError("RunPod API returned non-JSON response.") from e
    if not isinstance(pods, list):
        raise RuntimeError("RunPod API returned unexpected payload (expected a list of pods).")

    if pod_name:
        pods = [p for p in pods if isinstance(p, dict) and p.get("name") == pod_name]
        if not pods:
            raise RuntimeError(f"No RUNNING pod found with name {pod_name!r}.")
        if len(pods) > 1:
            raise RuntimeError(
                f"Multiple RUNNING pods named {pod_name!r}; use --runpod-pod-id to pick one."
            )

    if not pods:
        hint = " Create or start a pod, or pass --runpod-pod-id / --runpod-name."
        if pod_id:
            raise RuntimeError(f"No RUNNING pod matched id {pod_id!r}.{hint}")
        raise RuntimeError(f"No RUNNING pods found.{hint}")

    if len(pods) > 1 and not pod_id and not pod_name:
        lines = []
        for p in pods:
            if not isinstance(p, dict):
                continue
            pid = p.get("id", "?")
            pname = p.get("name") or ""
            lines.append(f"  - id={pid!r} name={pname!r}")
        msg = "Multiple RUNNING pods; pick one with --runpod-pod-id or --runpod-name:\n"
        msg += "\n".join(lines)
        raise RuntimeError(msg)

    pod = pods[0]
    if not isinstance(pod, dict):
        raise RuntimeError("RunPod API returned an invalid pod entry.")

    pid = pod.get("id", "?")
    public_ip = pod.get("publicIp")
    port_mappings = pod.get("portMappings") or {}
    if not public_ip:
        raise RuntimeError(
            f"Pod {pid!r} has no publicIp yet (still provisioning?). Retry in a moment."
        )

    ssh_port: str | None = None
    if isinstance(port_mappings, dict):
        for key in ("22", 22):
            if key in port_mappings:
                ssh_port = str(port_mappings[key])
                break
    if ssh_port is None:
        raise RuntimeError(
            f"Pod {pid!r} has no port 22 mapping in portMappings yet "
            f"(got {port_mappings!r}). SSH may still be starting."
        )

    dest = f"{default_user}@{public_ip}"
    summary = f"RunPod pod {pid} -> {dest} -p {ssh_port}"
    return dest, ssh_port, summary


def split_user_host(destination: str) -> tuple[str, str | None]:
    if "@" in destination:
        u, h = destination.split("@", 1)
        return h.strip(), u.strip() or None
    return destination.strip(), None


def default_ssh_config_path() -> Path:
    return Path.home() / ".ssh" / "config"


def render_host_block(
    host_alias: str,
    hostname: str,
    user: str | None,
    port: str | None,
    identity_file: str | None,
) -> str:
    lines = [f"Host {host_alias}", f"  HostName {hostname}"]
    if user:
        lines.append(f"  User {user}")
    if port:
        lines.append(f"  Port {port}")
    if identity_file:
        # Prefer absolute path for IdentityFile when we expand ~
        id_path = _expand(identity_file)
        lines.append(f"  IdentityFile {id_path}")
    lines.append("")
    return "\n".join(lines)


def upsert_ssh_config(config_path: Path, host_alias: str, new_block: str) -> None:
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker_begin = f"# --- build-nanogpt: {host_alias} (managed by configure_remote_ssh.py) ---"
    marker_end = f"# --- end build-nanogpt: {host_alias} ---"

    if config_path.exists():
        text = config_path.read_text(encoding="utf-8")
    else:
        text = ""

    # Remove previous managed block for this alias
    pattern = re.compile(
        re.escape(marker_begin) + r".*?" + re.escape(marker_end) + r"\s*",
        re.DOTALL,
    )
    text, _ = pattern.subn("", text)

    insert = f"{marker_begin}\n{new_block.rstrip()}\n{marker_end}\n\n"
    if text and not text.endswith("\n"):
        text += "\n"
    text = text + insert
    config_path.write_text(text, encoding="utf-8")
    try:
        config_path.chmod(0o600)
    except OSError:
        pass


def git_cwd_origin() -> str | None:
    try:
        cp = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        if cp.returncode != 0:
            return None
        return cp.stdout.strip()
    except OSError:
        return None


def build_ssh_base_cmd(host_alias: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        host_alias,
    ]


def remote_path_for_remote_bash(remote_path: str) -> str:
    """Turn provider-style paths into shell the remote bash expands correctly."""
    p = remote_path.rstrip("/")
    if p.startswith("~/"):
        return f"$HOME/{p[2:]}"
    return p


def remote_path_shell_assignment(remote_path: str) -> str:
    """REMOTE_PATH=... line for bash (tilde and $HOME must expand on the server)."""
    rp = remote_path_for_remote_bash(remote_path)
    if rp.startswith("$"):
        return f'REMOTE_PATH="{rp}"'
    return f"REMOTE_PATH={sh_quote(rp)}"


def github_https_clone_url(git_url: str) -> str | None:
    """If URL is GitHub over SSH, return equivalent HTTPS URL (no SSH key needed on the remote)."""
    m = re.match(r"^git@github\.com:(.+)$", git_url)
    if m:
        return f"https://github.com/{m.group(1)}"
    m = re.match(r"^ssh://git@github\.com/(.+)$", git_url)
    if m:
        return f"https://github.com/{m.group(1)}"
    return None


def effective_remote_clone_url(
    clone_url: str | None,
    *,
    ssh_clone: bool,
) -> str | None:
    """Prefer HTTPS for GitHub SSH URLs on the server unless --ssh-clone."""
    if clone_url is None or ssh_clone:
        return clone_url
    https = github_https_clone_url(clone_url)
    return https if https is not None else clone_url


def git_ssh_host_for_keyscan(clone_url: str) -> str | None:
    """Hostname to ssh-keyscan when cloning over SSH (avoids host key verification failures on fresh VMs)."""
    if clone_url.startswith("git@"):
        rest = clone_url[4:]
        if ":" in rest:
            return rest.split(":", 1)[0]
        return None
    m = re.match(r"^ssh://git@([^/]+)/", clone_url)
    if m:
        return m.group(1)
    return None


def remote_bootstrap_script(remote_path: str, clone_url: str | None) -> str:
    """Shell script run on the remote via ssh."""
    cu = clone_url or ""
    git_host = git_ssh_host_for_keyscan(cu) if cu else None
    lines = [
        "set -euo pipefail",
        remote_path_shell_assignment(remote_path),
        f"CLONE_URL={sh_quote(cu)}",
        'export PATH="$HOME/.local/bin:$PATH"',
        "if ! command -v uv >/dev/null 2>&1; then",
        '  curl -LsSf https://astral.sh/uv/install.sh | sh',
        '  export PATH="$HOME/.local/bin:$PATH"',
        "fi",
        'grep -q \'/.local/bin\' "$HOME/.bashrc" 2>/dev/null || echo \'export PATH="$HOME/.local/bin:$PATH"\' >> "$HOME/.bashrc"',
        'if [ ! -d "$REMOTE_PATH/.git" ]; then',
        '  mkdir -p "$(dirname "$REMOTE_PATH")"',
        '  if [ -n "$CLONE_URL" ]; then',
        '    mkdir -p "$HOME/.ssh"',
        '    chmod 700 "$HOME/.ssh"',
        '    touch "$HOME/.ssh/known_hosts"',
        '    chmod 600 "$HOME/.ssh/known_hosts"',
    ]
    if git_host:
        gh = sh_quote(git_host)
        lines.append(
            f"    ssh-keyscan -t rsa,ecdsa,ed25519 {gh} >> \"$HOME/.ssh/known_hosts\" 2>/dev/null || true"
        )
    lines += [
        '    export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new -o BatchMode=no"',
        '    git clone "$CLONE_URL" "$REMOTE_PATH"',
        "  else",
        '    echo "Remote path $REMOTE_PATH is missing or not a git repo; pass --clone-url or clone manually." >&2',
        "    exit 1",
        "  fi",
        "else",
        '  cd "$REMOTE_PATH"',
        '  echo "==> git pull..."',
        "  git pull --ff-only",
        "fi",
        'cd "$REMOTE_PATH"',
        'if [ ! -e /dev/nvidia0 ] && ls /dev/nvidia[0-9]* >/dev/null 2>&1; then',
        '  GPU_DEV=$(ls /dev/nvidia[0-9]* | head -1)',
        '  echo "==> /dev/nvidia0 missing but $GPU_DEV found; creating symlink..."',
        '  ln -s "$GPU_DEV" /dev/nvidia0',
        "fi",
        'echo "==> uv sync (large CUDA/PyTorch download; may take several minutes)..."',
        "uv sync",
        'echo "==> torch import check..."',
        'uv run python -c "import torch; print(\'torch\', torch.__version__, \'cuda\', torch.cuda.is_available())"',
    ]
    return "\n".join(lines) + "\n"


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add SSH config + prepare remote for build-nanogpt (uv sync, torch check).",
    )
    parser.add_argument(
        "ssh_tokens",
        nargs=argparse.REMAINDER,
        help='Provider SSH invocation, e.g. "ssh root@1.2.3.4 -p 22135 -i ~/.ssh/key"',
    )
    parser.add_argument(
        "--ssh-host-alias",
        default="build-nanogpt-remote",
        metavar="NAME",
        help="Host alias written to ~/.ssh/config (default: %(default)s)",
    )
    parser.add_argument(
        "--remote-path",
        default="~/build-nanogpt",
        metavar="PATH",
        help="Directory on the server for this repo (default: %(default)s)",
    )
    parser.add_argument(
        "--clone-url",
        default=None,
        metavar="URL",
        help="Git URL to clone if the remote path is missing or not a repo "
        "(default: origin URL of this repo on your machine)",
    )
    parser.add_argument(
        "--ssh-clone",
        action="store_true",
        help="Clone using SSH on the server (needs GitHub host keys + a key on the VM). "
        "Default: GitHub git@ origins use HTTPS on the remote so cloning works without keys.",
    )
    parser.add_argument(
        "--skip-remote",
        action="store_true",
        help="Only update local ~/.ssh/config; do not SSH for setup",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without writing config or running remote commands",
    )
    parser.add_argument(
        "--runpod-api-key",
        default=None,
        metavar="KEY",
        help="RunPod API key (default: RUNPOD_API_KEY env). Used when no SSH tokens are given.",
    )
    parser.add_argument(
        "--runpod-pod-id",
        default=None,
        metavar="ID",
        help="Select a specific RUNNING pod when multiple match (RunPod API mode).",
    )
    parser.add_argument(
        "--runpod-name",
        default=None,
        metavar="NAME",
        help="Select a RUNNING pod by its display name (RunPod API mode).",
    )
    parser.add_argument(
        "--runpod-user",
        default="root",
        metavar="USER",
        help="SSH user for RunPod API mode (default: %(default)s)",
    )
    parser.add_argument(
        "--identity",
        default=None,
        metavar="PATH",
        help="SSH private key path (RunPod API mode; same role as -i in a manual ssh line).",
    )
    args = parser.parse_args()

    tokens = list(args.ssh_tokens)
    if tokens and tokens[0] == "--":
        tokens = tokens[1:]

    use_runpod = not bool(tokens)
    api_key = runpod_api_key(args.runpod_api_key)

    if use_runpod:
        if not api_key:
            parser.error(
                "With no SSH tokens, set RUNPOD_API_KEY (or pass --runpod-api-key) to use "
                "the RunPod REST API, or pass a provider SSH line, e.g.\n"
                "  python scripts/configure_remote_ssh.py ssh root@63.141.33.45 -p 22135 -i ~/.ssh/key"
            )
        try:
            dest, port, runpod_summary = fetch_runpod_ssh(
                api_key,
                pod_id=args.runpod_pod_id,
                pod_name=args.runpod_name,
                default_user=args.runpod_user,
            )
        except RuntimeError as e:
            parser.error(str(e))
        identity = _expand(args.identity) if args.identity else None
        if not args.dry_run:
            print(runpod_summary, file=sys.stderr)
    else:
        try:
            dest, port, identity = parse_ssh_provider_args(tokens)
        except ValueError as e:
            parser.error(str(e))

    hostname, user_from_dest = split_user_host(dest)
    user = user_from_dest

    if identity and not Path(identity).expanduser().is_file():
        print(f"warning: identity file not found locally: {identity}", file=sys.stderr)

    block = render_host_block(
        args.ssh_host_alias,
        hostname,
        user,
        port,
        identity,
    )
    cfg = default_ssh_config_path()

    if args.dry_run:
        print(f"Would write managed block to {cfg}:\n{block}")
    else:
        upsert_ssh_config(cfg, args.ssh_host_alias, block)
        print(f"Updated {cfg} (Host {args.ssh_host_alias})")

    if args.skip_remote:
        print(f"Connect with: ssh {args.ssh_host_alias}")
        return 0

    raw_clone = args.clone_url or git_cwd_origin()
    clone_url = effective_remote_clone_url(raw_clone, ssh_clone=args.ssh_clone)
    if raw_clone and clone_url != raw_clone and not args.dry_run:
        print(f"Using HTTPS on the remote for clone (was {raw_clone!r}).", file=sys.stderr)
    remote_path = args.remote_path.rstrip("/")
    remote_script = remote_bootstrap_script(remote_path, clone_url)

    ssh_cmd = build_ssh_base_cmd(args.ssh_host_alias) + ["bash", "-s"]
    if args.dry_run:
        print("Would run on remote:\n" + remote_script)
        print("SSH:", " ".join(ssh_cmd))
        return 0

    if not shutil.which("ssh"):
        print("error: ssh not found in PATH", file=sys.stderr)
        return 1

    print(f"Connecting as Host {args.ssh_host_alias} and preparing {remote_path} ...")
    try:
        cp = subprocess.run(
            ssh_cmd,
            input=remote_script,
            text=True,
        )
    except FileNotFoundError:
        print("error: ssh failed to execute", file=sys.stderr)
        return 1

    if cp.returncode != 0:
        print("Remote setup failed.", file=sys.stderr)
        return cp.returncode

    print(
        f"Done. Use: ssh {args.ssh_host_alias}  and open "
        f"{remote_path_for_remote_bash(args.remote_path)} on the server in Remote-SSH."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
