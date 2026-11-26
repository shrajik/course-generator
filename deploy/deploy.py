#!/usr/bin/env python3
"""Deploy the course generator to a Docker host over SSH. Standard library only.

    python deploy/deploy.py deploy   --host 45.140.201.44 --key path/to/deploy_key
    python deploy/deploy.py deploy   --dry-run          # checks everything, touches no server
    python deploy/deploy.py status
    python deploy/deploy.py logs backend
    python deploy/deploy.py rollback
    python deploy/deploy.py env      [--dry-run]        # push only the env file, restart what changed

What `deploy` does:
  1. checks the local env file (required secrets present, URL-safe database password, ...);
  2. connects with the SSH key, checks Docker, and creates the shared `web` network if missing;
  3. uploads the repository as a new release folder  <remote-dir>/releases/<timestamp>/  (tracked and
     not-ignored files only; never .env files, node_modules or backend/data);
  4. uploads the env file to <remote-dir>/shared/.env (mode 600) and links it into the release;
  5. runs `docker compose up -d --build` for the project `coursegen`;
  6. waits for the backend health check, then keeps the 3 newest releases and prunes dangling images.

It never touches any other app on the server: no other compose project, no host ports unless you pass
--with-caddy (and then only after checking that 80/443 are free).

Settings come from flags or environment variables: DEPLOY_HOST, DEPLOY_USER (default "deploy"),
DEPLOY_SSH_KEY, DEPLOY_ENV_FILE (default ".env.production"). The env file is a local, git-ignored copy of
.env.production.example. Secrets are never printed.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPOSE_FILE = "docker-compose.prod.yml"
PROJECT = "coursegen"
KEEP_RELEASES = 3
REQUIRED = ("APP_DOMAIN", "API_DOMAIN", "POSTGRES_PASSWORD", "JWT_SECRET", "JWT_REFRESH_SECRET")

# Never uploaded, wherever they appear in the path.
SKIP_DIRS = {
    ".git", ".github", ".claude", ".kilo", ".vscode", "node_modules", ".next", "__pycache__",
    ".venv", "venv", ".pytest_cache", "_to_delete", "_diagram_compare_out",
}
SKIP_PREFIXES = ("backend/data/",)  # runtime data lives in a Docker volume on the server
SKIP_NAMES = {"tsconfig.tsbuildinfo", ".DS_Store"}
KEEP_ENV_EXAMPLES = {".env.example", ".env.production.example"}


class DeployError(Exception):
    pass


def log(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    log(f"\n==> {msg}")


# --------------------------------------------------------------------------- local checks
def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for n, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise DeployError(f"{path.name}:{n}: expected KEY=VALUE")
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key.strip()] = val
    return values


def check_env(env: dict[str, str]) -> list[str]:
    """Return warnings; raise DeployError for anything that would break or endanger the deploy."""
    problems = [k for k in REQUIRED if not env.get(k)]
    if problems:
        raise DeployError("env file is missing required values: " + ", ".join(problems))
    for k in ("APP_DOMAIN", "API_DOMAIN"):
        if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?", env[k]) or "." not in env[k]:
            raise DeployError(f"{k} must be a bare host name like app.example.com (no https://, no path)")
    if env["APP_DOMAIN"] == env["API_DOMAIN"]:
        raise DeployError("APP_DOMAIN and API_DOMAIN must be different names")
    # The password is placed inside DATABASE_URL, so characters such as @ : / # ? % would break it.
    if not re.fullmatch(r"[A-Za-z0-9._~-]{12,}", env["POSTGRES_PASSWORD"]):
        raise DeployError("POSTGRES_PASSWORD must be 12+ characters of letters, digits, . _ ~ - only "
                          "(generate one with: openssl rand -hex 24)")
    for k in ("JWT_SECRET", "JWT_REFRESH_SECRET"):
        if len(env[k]) < 32:
            raise DeployError(f"{k} must be at least 32 characters (openssl rand -hex 48)")
    if env["JWT_SECRET"] == env["JWT_REFRESH_SECRET"]:
        raise DeployError("JWT_SECRET and JWT_REFRESH_SECRET must be different")
    warnings = []
    if env.get("MOCK_OPENAI", "").lower() == "true":
        warnings.append("MOCK_OPENAI=true: the site will serve offline placeholder content")
    if not any(env.get(k) for k in ("AZURE_GPT5_API_KEY", "AZURE_GPT55_API_KEY", "OPENAI_API_KEY")):
        warnings.append("no AI provider key found (AZURE_GPT5_API_KEY / AZURE_GPT55_API_KEY / OPENAI_API_KEY)")
    if not env.get("INITIAL_ADMIN_EMAIL"):
        warnings.append("INITIAL_ADMIN_EMAIL is empty: nobody will get admin rights")
    return warnings


def release_files() -> list[str]:
    """Paths (repo-relative, forward slashes) to upload: tracked + untracked-but-not-ignored."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=REPO, check=True, capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DeployError(f"cannot list files with git: {exc}") from exc
    files = []
    for rel in sorted(set(out.decode("utf-8").split("\0"))):
        if not rel:
            continue
        parts = rel.split("/")
        name = parts[-1]
        if any(p in SKIP_DIRS for p in parts[:-1]) or rel.startswith(SKIP_PREFIXES):
            continue
        if name in SKIP_NAMES or name.endswith(".pyc"):
            continue
        # Junk left by a botched shell command (private-use characters, "&&"): never ship it.
        if "&&" in rel or any("" <= ch <= "" for ch in rel):
            log(f"  skipping oddly named file: {rel.encode('ascii', 'backslashreplace').decode()[:90]}")
            continue
        if (name == ".env" or name.startswith(".env.")) and name not in KEEP_ENV_EXAMPLES:
            continue
        if not (REPO / rel).is_file():  # tracked but deleted in the working tree
            continue
        files.append(rel)
    for required in (COMPOSE_FILE, "backend/Dockerfile", "frontend/Dockerfile", "code-executor/Dockerfile"):
        if required not in files:
            raise DeployError(f"{required} is not in the release (is it committed or at least not ignored?)")
    return files


# --------------------------------------------------------------------------- ssh helpers
class Remote:
    def __init__(self, host: str, user: str, key: str, base: str):
        self.target = f"{user}@{host}"
        self.base = base.rstrip("/")
        self.opts = [
            "-i", key, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30",
        ]

    def argv(self, command: str) -> list[str]:
        return ["ssh", *self.opts, self.target, command]

    def run(self, command: str, *, check: bool = True, input: bytes | None = None,
            capture: bool = False) -> subprocess.CompletedProcess:
        proc = subprocess.run(self.argv(command), input=input,
                              capture_output=capture, check=False)
        if check and proc.returncode != 0:
            raise DeployError(f"remote command failed (exit {proc.returncode}): {command[:120]}")
        return proc

    def text(self, command: str) -> str:
        out = self.run(command, capture=True, check=False).stdout
        return (out or b"").decode("utf-8", "replace").strip()


def tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o755 if info.isdir() or info.name.endswith(".sh") else 0o644
    return info


def upload_release(remote: Remote, files: list[str], release_dir: str) -> None:
    proc = subprocess.Popen(
        remote.argv(f"mkdir -p {shlex.quote(release_dir)} && tar -xzf - -C {shlex.quote(release_dir)}"),
        stdin=subprocess.PIPE,
    )
    assert proc.stdin is not None
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|gz") as tar:
            for rel in files:
                tar.add(REPO / rel, arcname=rel, recursive=False, filter=tar_filter)
    except BrokenPipeError as exc:
        raise DeployError("connection closed while uploading") from exc
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
    if proc.wait() != 0:
        raise DeployError("upload failed")


# --------------------------------------------------------------------------- commands
def compose_cmd(with_caddy: bool) -> str:
    profile = "--profile own-caddy " if with_caddy else ""
    return f"docker compose -p {PROJECT} -f {COMPOSE_FILE} {profile}"


def wait_healthy(remote: Remote, release_dir: str, with_caddy: bool, attempts: int = 30) -> None:
    probe = ("exec -T backend python -c "
             "\"import urllib.request;urllib.request.urlopen('http://localhost:8000/health').read()\"")
    cmd = f"cd {shlex.quote(release_dir)} && {compose_cmd(with_caddy)}{probe}"
    for i in range(1, attempts + 1):
        if remote.run(cmd, check=False, capture=True).returncode == 0:
            log("backend is healthy")
            return
        log(f"  waiting for the backend ({i}/{attempts})")
        time.sleep(10)
    log("\nbackend did not become healthy. Recent log lines:")
    remote.run(f"cd {shlex.quote(release_dir)} && {compose_cmd(with_caddy)}logs --tail=60 backend", check=False)
    raise DeployError("backend failed its health check")


def cmd_deploy(args: argparse.Namespace) -> None:
    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        env_path = REPO / env_path
    if not env_path.is_file():
        raise DeployError(f"env file not found: {env_path}\n"
                          f"  copy .env.production.example to {env_path.name} and fill it in")
    step("Checking the env file")
    env = parse_env(env_path)
    for w in check_env(env):
        log(f"  warning: {w}")
    log(f"  ok: {len(env)} values; app https://{env['APP_DOMAIN']}  api https://{env['API_DOMAIN']}")

    step("Collecting files")
    files = release_files()
    log(f"  {len(files)} files")
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        log("  note: you have uncommitted changes; the working tree is what gets deployed")
    if args.dry_run:
        log("\n--dry-run: nothing was uploaded.")
        return

    remote = make_remote(args)
    base = remote.base
    release = time.strftime("%Y%m%d-%H%M%S")
    release_dir = f"{base}/releases/{release}"

    step(f"Connecting to {remote.target}")
    if not remote.text("docker compose version"):
        raise DeployError("docker compose is not available for this user on the server")
    remote.run("docker network inspect web >/dev/null 2>&1 || docker network create web >/dev/null")
    if args.with_caddy:
        busy = remote.text("ss -ltnH '( sport = :80 or sport = :443 )'")
        if busy:
            raise DeployError("--with-caddy: ports 80/443 are already in use on the server (another proxy?). "
                              "Run without --with-caddy and add this app's two hosts to the existing proxy.")

    step(f"Uploading release {release}")
    remote.run(f"mkdir -p {shlex.quote(base)}/shared {shlex.quote(base)}/releases")
    upload_release(remote, files, release_dir)
    remote.run(f"umask 077 && cat > {shlex.quote(base)}/shared/.env",
               input=env_path.read_bytes().rstrip(b"\r\n") + b"\n")
    remote.run(f"ln -sfn ../../shared/.env {shlex.quote(release_dir)}/.env")

    step("Building and starting (the first build takes several minutes)")
    remote.run(f"cd {shlex.quote(release_dir)} && {compose_cmd(args.with_caddy)}up -d --build --remove-orphans")

    step("Waiting for the backend")
    wait_healthy(remote, release_dir, args.with_caddy)

    remote.run(f"ln -sfn releases/{release} {shlex.quote(base)}/current")
    remote.run(f"cd {shlex.quote(base)}/releases && ls -1dt */ | tail -n +{KEEP_RELEASES + 1} | xargs -r rm -rf", check=False)
    remote.run("docker image prune -f >/dev/null", check=False)

    step("Done")
    log(f"  release {release} is running as compose project '{PROJECT}'")
    if args.with_caddy:
        log(f"  open https://{env['APP_DOMAIN']}")
    else:
        log("  No ports are published. Your reverse proxy must route to the shared `web` network:")
        log(f"    https://{env['APP_DOMAIN']}  -> coursegen-frontend:3000")
        log(f"    https://{env['API_DOMAIN']}  -> coursegen-backend:8000   (flush_interval -1)")
    log("  Then register INITIAL_ADMIN_EMAIL straight away: that account becomes the admin.")


# Changing these after the first deploy would break the running stack or need a rebuild.
ENV_NEEDS_FULL_DEPLOY = ("APP_DOMAIN", "API_DOMAIN")   # baked into the frontend bundle at build time
ENV_DB_LOCKED = ("POSTGRES_PASSWORD", "POSTGRES_USER", "POSTGRES_DB")  # the database keeps its own copy


def cmd_env(args: argparse.Namespace) -> None:
    """Upload only the env file and recreate the services whose environment changed (no rebuild)."""
    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        env_path = REPO / env_path
    if not env_path.is_file():
        raise DeployError(f"env file not found: {env_path}")
    new = parse_env(env_path)
    for w in check_env(new):
        log(f"  warning: {w}")

    remote = make_remote(args)
    base = remote.base
    current = remote.text(f"readlink -f {shlex.quote(base)}/current")
    if not current:
        raise DeployError("nothing is deployed yet: run `deploy` first")
    old_text = remote.text(f"cat {shlex.quote(base)}/shared/.env")
    tmp = REPO / ".env.remote.tmp"
    try:
        tmp.write_text(old_text + "\n", encoding="utf-8")
        old = parse_env(tmp)
    finally:
        tmp.unlink(missing_ok=True)

    added = sorted(k for k in new if k not in old)
    removed = sorted(k for k in old if k not in new)
    changed = sorted(k for k in new if k in old and new[k] != old[k])
    step("Differences between your env file and the server's (names only, never values)")
    for label, keys in (("added", added), ("changed", changed), ("removed", removed)):
        log(f"  {label:8}: {', '.join(keys) if keys else '-'}")
    if not (added or removed or changed):
        log("\nThe server already has this env file. Nothing to do.")
        return

    touched = set(added) | set(changed) | set(removed)
    if touched & set(ENV_NEEDS_FULL_DEPLOY):
        raise DeployError("APP_DOMAIN / API_DOMAIN changed: the frontend must be rebuilt. Run `deploy` instead.")
    locked = sorted(touched & set(ENV_DB_LOCKED))
    if locked and not args.force:
        raise DeployError(f"{', '.join(locked)} changed, but the existing database keeps its original value, so the "
                          "backend would fail to connect. Change the database password inside Postgres first, or "
                          "pass --force if you know what you are doing.")
    if {"JWT_SECRET", "JWT_REFRESH_SECRET"} & touched:
        log("\n  note: changing the JWT secrets signs every user out")
    if args.dry_run:
        log("\n--dry-run: nothing was uploaded.")
        return

    step("Uploading the env file")
    remote.run(f"umask 077 && cat > {shlex.quote(base)}/shared/.env",
               input=env_path.read_bytes().rstrip(b"\r\n") + b"\n")
    step("Recreating the services whose environment changed (no rebuild)")
    remote.run(f"cd {shlex.quote(current)} && {compose_cmd(args.with_caddy)}up -d --no-build --remove-orphans")
    wait_healthy(remote, current, args.with_caddy)
    log("\n  env updated. Services whose settings did not change were left running.")


def cmd_status(args: argparse.Namespace) -> None:
    remote = make_remote(args)
    step(f"Releases on {remote.target}")
    remote.run(f"ls -1dt {shlex.quote(remote.base)}/releases/*/ 2>/dev/null | head -5; "
               f"readlink {shlex.quote(remote.base)}/current || echo '(nothing deployed yet)'", check=False)
    step(f"Containers (project {PROJECT})")
    remote.run(f"docker compose -p {PROJECT} ps -a", check=False)


def cmd_logs(args: argparse.Namespace) -> None:
    remote = make_remote(args)
    remote.run(f"docker compose -p {PROJECT} logs --tail={int(args.lines)} {shlex.quote(args.service)}", check=False)


def cmd_rollback(args: argparse.Namespace) -> None:
    remote = make_remote(args)
    base = remote.base
    current = remote.text(f"readlink {shlex.quote(base)}/current").rstrip("/").split("/")[-1]
    releases = [r for r in remote.text(f"ls -1t {shlex.quote(base)}/releases").splitlines() if r]
    older = [r for r in releases if current and r < current]
    if not current or not older:
        raise DeployError("there is no older release to roll back to")
    target = older[0]
    release_dir = f"{base}/releases/{target}"
    step(f"Rolling back {current} -> {target}")
    remote.run(f"cd {shlex.quote(release_dir)} && {compose_cmd(args.with_caddy)}up -d --build --remove-orphans")
    wait_healthy(remote, release_dir, args.with_caddy)
    remote.run(f"ln -sfn releases/{target} {shlex.quote(base)}/current")
    log(f"  now running release {target}. The database was NOT rolled back.")


def make_remote(args: argparse.Namespace) -> Remote:
    if not args.host:
        raise DeployError("no server given: use --host or set DEPLOY_HOST")
    if not args.key:
        raise DeployError("no SSH key given: use --key or set DEPLOY_SSH_KEY")
    key = Path(args.key).expanduser()
    if not key.is_file():
        raise DeployError(f"SSH key not found: {key}")
    return Remote(args.host, args.user, str(key), args.remote_dir)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Deploy the course generator over SSH.")
    p.add_argument("command", nargs="?", default="deploy",
                   choices=["deploy", "status", "logs", "rollback", "env"])
    p.add_argument("service", nargs="?", default="backend", help="service for `logs` (default: backend)")
    p.add_argument("--host", default=os.environ.get("DEPLOY_HOST"))
    p.add_argument("--user", default=os.environ.get("DEPLOY_USER", "deploy"))
    p.add_argument("--key", default=os.environ.get("DEPLOY_SSH_KEY"))
    p.add_argument("--env-file", default=os.environ.get("DEPLOY_ENV_FILE", ".env.production"))
    p.add_argument("--remote-dir", default="/opt/course-generator")
    p.add_argument("--with-caddy", action="store_true",
                   help="also start the bundled Caddy on ports 80/443 (only if the server has no proxy)")
    p.add_argument("--dry-run", action="store_true", help="check the env file and file list; connect to nothing")
    p.add_argument("--lines", type=int, default=100, help="log lines for `logs`")
    p.add_argument("--force", action="store_true", help="env: allow changing POSTGRES_* (see the error text first)")
    return p


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    args = build_parser().parse_args()
    handlers = {"deploy": cmd_deploy, "status": cmd_status, "logs": cmd_logs, "rollback": cmd_rollback,
                "env": cmd_env}
    try:
        handlers[args.command](args)
    except DeployError as exc:
        log(f"\nERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        log("\ninterrupted")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
