"""Authentication and repository resolution for GS-MAE CLI."""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse, urlunparse


@dataclass
class UserSession:
    username: str
    session_id: str
    authenticated_at: str
    repo_name: str
    repo_path: str
    is_remote: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)


class AuthenticationError(Exception):
    """Raised when authentication fails."""
    pass


class RepositoryError(Exception):
    """Raised when repository cannot be resolved or cloned."""
    pass


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def authenticate(username: Optional[str], password: Optional[str],
                 config: Optional[Dict] = None) -> str:
    """Validate user credentials.

    Checks configured users in config if specified, then environment variables,
    or enforces non-empty valid developer credentials.
    """
    u = (username or "").strip()
    p = password or ""

    if not u:
        raise AuthenticationError("Username is required and cannot be empty.")
    if not p:
        raise AuthenticationError("Password is required and cannot be empty.")

    # 1. Configured users in config
    cfg_auth = (config or {}).get("auth", {})
    configured_users = cfg_auth.get("users", {})
    if configured_users:
        expected = configured_users.get(u)
        if expected is None:
            raise AuthenticationError(f"User '{u}' is not authorized.")
        # Support both plain text match and SHA-256 hash match
        if expected != p and expected != _hash_password(p):
            raise AuthenticationError("Invalid password for user.")
        return u

    # 2. Environment variable credentials
    env_user = os.environ.get("GSMAE_AUTH_USER")
    env_pass = os.environ.get("GSMAE_AUTH_PASSWORD")
    if env_user:
        if u != env_user:
            raise AuthenticationError(f"User '{u}' does not match GSMAE_AUTH_USER.")
        if env_pass and p != env_pass:
            raise AuthenticationError("Invalid password for GSMAE_AUTH_USER.")
        return u

    # 3. Default policy: valid credentials required (username >= 2 chars, password >= 3 chars)
    if len(u) < 2:
        raise AuthenticationError("Username must be at least 2 characters.")
    if len(p) < 3:
        raise AuthenticationError("Password must be at least 3 characters.")

    return u


def is_remote_url(repo_target: str) -> bool:
    """Check if the repository target is a remote URL."""
    target = repo_target.strip()
    if target.startswith(("http://", "https://", "git://", "ssh://", "git@")):
        return True
    if target.endswith(".git") and (":" in target or "/" in target):
        return True
    return False


def _sanitize_url(url: str) -> str:
    """Remove password from URL for safe logging/display."""
    try:
        parsed = urlparse(url)
        if parsed.password:
            netloc = f"{parsed.username}:***@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urlunparse(parsed._replace(netloc=netloc))
        return url
    except Exception:
        return re.sub(r"://([^:@]+):([^@]+)@", r"://\1:***@", url)


def resolve_repository(repo_target: str, username: str, password: str,
                       checkout_root: Optional[Path] = None) -> Tuple[Path, bool]:
    """Resolve a local path or clone a remote repository using authenticated credentials.

    Returns:
        (resolved_path, is_remote)
    """
    target = repo_target.strip()
    if not target:
        raise RepositoryError("Repository target cannot be empty.")

    if not is_remote_url(target):
        # Local repository path
        local_path = Path(target).expanduser().resolve()
        if not local_path.exists():
            raise RepositoryError(f"Local repository path does not exist: {local_path}")
        if not local_path.is_dir():
            raise RepositoryError(f"Local repository path is not a directory: {local_path}")
        return local_path, False

    # Remote repository URL
    base_dir = Path(checkout_root or Path("outputs/checkouts")).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)

    # Derive clone directory name
    clean_name = re.sub(r"\.git$", "", target.rstrip("/").split("/")[-1])
    clean_name = re.sub(r"[^A-Za-z0-9._-]", "_", clean_name) or "repo"
    clone_dir = base_dir / f"{clean_name}_{int(time.time())}"

    # Build authenticated URL for HTTP/HTTPS
    parsed = urlparse(target)
    if parsed.scheme in ("http", "https"):
        from urllib.parse import quote
        auth_netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{parsed.hostname}"
        if parsed.port:
            auth_netloc += f":{parsed.port}"
        clone_url = urlunparse(parsed._replace(netloc=auth_netloc))
    else:
        clone_url = target

    safe_url = _sanitize_url(target)
    print(f"[gs-mae] Cloning remote repository {safe_url} into {clone_dir.name}...")

    try:
        # Run git clone with credentials
        cmd = ["git", "clone", "--depth", "1", clone_url, str(clone_dir)]
        # Use clean environment preventing prompt hangs
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=120,
        )
        if proc.returncode != 0:
            err = proc.stderr.replace(password, "***")
            raise RepositoryError(f"Git clone failed for {safe_url}: {err.strip()}")
    except subprocess.TimeoutExpired:
        if clone_dir.exists():
            shutil.rmtree(clone_dir, ignore_errors=True)
        raise RepositoryError(f"Git clone timed out for {safe_url}")
    except Exception as exc:
        if clone_dir.exists():
            shutil.rmtree(clone_dir, ignore_errors=True)
        err_msg = str(exc).replace(password, "***")
        raise RepositoryError(f"Failed to clone repository: {err_msg}")

    return clone_dir, True


def create_user_session(username: str, repo_path: Path, is_remote: bool) -> UserSession:
    """Create a unique session object tracking the authenticated user and repository."""
    sess_id = f"sess_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    return UserSession(
        username=username,
        session_id=sess_id,
        authenticated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        repo_name=repo_path.name,
        repo_path=str(repo_path.resolve()),
        is_remote=is_remote,
    )


def prompt_credentials(cli_username: Optional[str] = None,
                       cli_password: Optional[str] = None) -> Tuple[str, str]:
    """Obtain username and password from CLI arguments or interactive prompt."""
    username = cli_username
    while not username:
        username = input("Enter username: ").strip()

    password = cli_password
    while not password:
        password = getpass.getpass("Enter password: ").strip()

    return username, password


def prompt_repo(cli_repo: Optional[str] = None) -> str:
    """Obtain repository path/URL from CLI arguments or interactive prompt."""
    repo = cli_repo
    while not repo:
        repo = input("Enter repository path or git URL: ").strip()
    return repo
