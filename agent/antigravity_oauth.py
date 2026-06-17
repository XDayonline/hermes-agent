"""OAuth token source for the Antigravity CLI inference provider.

Antigravity CLI stores Google OAuth credentials separately from Gemini CLI.
Hermes treats those credentials as a distinct source while reusing the same
Code Assist transport as ``google-gemini-cli``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from utils import atomic_replace

logger = logging.getLogger(__name__)


TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
TOKEN_REQUEST_TIMEOUT_SECONDS = 20.0
REFRESH_SKEW_SECONDS = 60
LOCK_TIMEOUT_SECONDS = 15.0

ENV_CLIENT_ID = "HERMES_ANTIGRAVITY_CLIENT_ID"
ENV_CLIENT_SECRET = "HERMES_ANTIGRAVITY_CLIENT_SECRET"
ENV_CLI_PATH = "HERMES_ANTIGRAVITY_CLI_PATH"
ENV_CLI_HOME = "HERMES_ANTIGRAVITY_CLI_HOME"

_CLIENT_ID_SHAPE = re.compile(r"([0-9]{8,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com)")
_CLIENT_SECRET_SHAPE = re.compile(r"(GOCSPX-[A-Za-z0-9_-]{20,80})")


class AntigravityOAuthError(RuntimeError):
    """Raised for failures reading or refreshing Antigravity OAuth tokens."""

    def __init__(self, message: str, *, code: str = "antigravity_oauth_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class AntigravityCredentials:
    access_token: str
    refresh_token: str
    expiry: datetime
    token_type: str = "Bearer"
    auth_method: str = "consumer"
    token_extras: Dict[str, Any] = field(default_factory=dict)
    top_level_extras: Dict[str, Any] = field(default_factory=dict)

    @property
    def expires_at_ms(self) -> int:
        return int(self.expiry.timestamp() * 1000)

    def access_token_expired(self, skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
        if not self.access_token:
            return True
        now = datetime.now(timezone.utc) + timedelta(seconds=max(0, skew_seconds))
        expiry = self.expiry
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return now >= expiry.astimezone(timezone.utc)

    def to_file_payload(self) -> Dict[str, Any]:
        expiry = self.expiry
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        payload = dict(self.top_level_extras or {})
        token_payload = dict(self.token_extras or {})
        token_payload.update({
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type or "Bearer",
            "expiry": expiry.isoformat(),
        })
        payload.update({
            "auth_method": self.auth_method or "consumer",
            "token": token_payload,
        })
        return payload


def _credentials_path() -> Path:
    cli_home = os.getenv(ENV_CLI_HOME, "").strip()
    if cli_home:
        return Path(cli_home).expanduser() / "antigravity-oauth-token"
    return Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


def _lock_path() -> Path:
    return _credentials_path().with_suffix(".lock")


_lock_state = threading.local()


@contextlib.contextmanager
def _credentials_lock(timeout_seconds: float = LOCK_TIMEOUT_SECONDS):
    """Cross-process lock around the Antigravity credentials file."""
    depth = getattr(_lock_state, "depth", 0)
    if depth > 0:
        _lock_state.depth = depth + 1
        try:
            yield
        finally:
            _lock_state.depth -= 1
        return

    lock_file_path = _lock_path()
    lock_file_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file_path), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            import fcntl
        except ImportError:
            fcntl = None

        if fcntl is not None:
            deadline = time.monotonic() + max(0.0, float(timeout_seconds))
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out acquiring Antigravity OAuth credentials lock at {lock_file_path}."
                        )
                    time.sleep(0.05)
        else:
            try:
                import msvcrt  # type: ignore[import-not-found]

                deadline = time.monotonic() + max(0.0, float(timeout_seconds))
                while True:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        acquired = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"Timed out acquiring Antigravity OAuth credentials lock at {lock_file_path}."
                            )
                        time.sleep(0.05)
            except ImportError:
                acquired = True

        _lock_state.depth = 1
        yield
    finally:
        try:
            if acquired:
                try:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
                except ImportError:
                    try:
                        import msvcrt  # type: ignore[import-not-found]

                        try:
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
                    except ImportError:
                        pass
        finally:
            os.close(fd)
            _lock_state.depth = 0


def _parse_expiry(value: Any) -> datetime:
    if isinstance(value, str) and value.strip():
        try:
            expiry = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise AntigravityOAuthError(
                "Antigravity CLI token file has an invalid expiry timestamp. Run `agy` and log in again.",
                code="antigravity_oauth_invalid_credentials",
            ) from exc
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry
    return datetime.fromtimestamp(0, tz=timezone.utc)


def load_credentials() -> Optional[AntigravityCredentials]:
    """Load Antigravity CLI credentials. Returns None if missing or invalid."""
    path = _credentials_path()
    try:
        with _credentials_lock():
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, IOError) as exc:
        logger.warning("Failed to read Antigravity OAuth credentials at %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    if not isinstance(token, dict):
        return None
    access_token = str(token.get("access_token") or "")
    if not access_token:
        return None
    try:
        return AntigravityCredentials(
            access_token=access_token,
            refresh_token=str(token.get("refresh_token") or ""),
            token_type=str(token.get("token_type") or "Bearer"),
            expiry=_parse_expiry(token.get("expiry")),
            auth_method=str(data.get("auth_method") or "consumer"),
            token_extras={
                k: v for k, v in token.items()
                if k not in {"access_token", "refresh_token", "token_type", "expiry"}
            },
            top_level_extras={
                k: v for k, v in data.items()
                if k not in {"auth_method", "token"}
            },
        )
    except AntigravityOAuthError as exc:
        logger.warning("Invalid Antigravity OAuth credentials at %s: %s", path, exc)
        return None


def save_credentials(creds: AntigravityCredentials) -> Path:
    """Write the Antigravity token file back in the CLI's native shape."""
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = json.dumps(creds.to_file_payload(), indent=2, sort_keys=True) + "\n"
    with _credentials_lock():
        tmp_path = path.with_suffix(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
        try:
            fd = os.open(
                str(tmp_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IRUSR | stat.S_IWUSR,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            atomic_replace(tmp_path, path)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
    return path


def clear_credentials() -> None:
    """Remove the Antigravity token file. Idempotent."""
    path = _credentials_path()
    with _credentials_lock():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Failed to remove Antigravity OAuth credentials at %s: %s", path, exc)


def _candidate_agy_paths() -> list[Path]:
    paths: list[Path] = []
    explicit = os.getenv(ENV_CLI_PATH, "").strip()
    if explicit:
        paths.append(Path(explicit).expanduser())
    resolved = shutil.which("agy")
    if resolved:
        paths.append(Path(resolved))
    paths.extend([
        Path.home() / ".local" / "bin" / "agy",
        Path("/opt/homebrew/bin/agy"),
        Path("/usr/local/bin/agy"),
    ])
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _secret_candidates(raw: str) -> list[str]:
    # Desktop OAuth secrets observed in Google CLIs are short GOCSPX strings.
    # Binary strings can be adjacent, so include plausible prefixes rather than
    # trusting one greedy regex match.
    candidates: list[str] = []
    for length in (35, 34, 36, 33, 37, 38, 39, 40, 41, 42):
        if len(raw) >= length:
            candidates.append(raw[:length])
    candidates.append(raw)
    return list(dict.fromkeys(candidates))


def _extract_oauth_client_credentials_from_agy(path: Path) -> list[Tuple[str, str]]:
    try:
        text = path.read_bytes().decode("latin-1", errors="ignore")
    except OSError:
        return []
    client_ids = list(dict.fromkeys(match.group(1) for match in _CLIENT_ID_SHAPE.finditer(text)))
    secret_matches: list[str] = []
    for match in _CLIENT_SECRET_SHAPE.finditer(text):
        secret_matches.extend(_secret_candidates(match.group(1)))
    secret_matches = list(dict.fromkeys(secret_matches))
    return [(client_id, secret) for client_id in client_ids for secret in secret_matches]


def _iter_oauth_client_credentials():
    env_id = os.getenv(ENV_CLIENT_ID, "").strip()
    env_secret = os.getenv(ENV_CLIENT_SECRET, "").strip()
    if env_id and env_secret:
        yield env_id, env_secret
        return
    for path in _candidate_agy_paths():
        for client_id, client_secret in _extract_oauth_client_credentials_from_agy(path):
            yield client_id, client_secret


def _resolve_oauth_client_credentials() -> Tuple[str, str]:
    for client_id, client_secret in _iter_oauth_client_credentials():
        return client_id, client_secret
    raise AntigravityOAuthError(
        "Could not find Antigravity OAuth client credentials. Install Antigravity CLI (`agy`) "
        "or set HERMES_ANTIGRAVITY_CLIENT_ID and HERMES_ANTIGRAVITY_CLIENT_SECRET.",
        code="antigravity_oauth_client_missing",
    )


def _post_form(url: str, data: Dict[str, str], timeout: float) -> Dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("ascii")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        code = "antigravity_oauth_token_http_error"
        if "invalid_grant" in detail.lower():
            code = "antigravity_oauth_invalid_grant"
        raise AntigravityOAuthError(
            f"Antigravity OAuth token endpoint returned HTTP {exc.code}: {detail or exc.reason}",
            code=code,
        ) from exc
    except urllib.error.URLError as exc:
        raise AntigravityOAuthError(
            f"Antigravity OAuth token request failed: {exc}",
            code="antigravity_oauth_token_network_error",
        ) from exc


def refresh_access_token(
    refresh_token: str,
    *,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    timeout: float = TOKEN_REQUEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    if not refresh_token:
        raise AntigravityOAuthError(
            "Cannot refresh Antigravity token: refresh_token is empty. Run `agy` and log in first.",
            code="antigravity_oauth_refresh_token_missing",
        )
    if client_id is not None or client_secret is not None:
        candidates = [(client_id or "", client_secret or "")]
    else:
        candidates = list(_iter_oauth_client_credentials())
        if not candidates:
            _resolve_oauth_client_credentials()

    last_error: Optional[AntigravityOAuthError] = None
    for cid, csecret in candidates:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": cid,
        }
        if csecret:
            data["client_secret"] = csecret
        try:
            return _post_form(TOKEN_ENDPOINT, data, timeout)
        except AntigravityOAuthError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise AntigravityOAuthError(
        "Could not find Antigravity OAuth client credentials. Install Antigravity CLI (`agy`) "
        "or set HERMES_ANTIGRAVITY_CLIENT_ID and HERMES_ANTIGRAVITY_CLIENT_SECRET.",
        code="antigravity_oauth_client_missing",
    )


_refresh_inflight: Dict[str, threading.Event] = {}
_refresh_inflight_lock = threading.Lock()


def get_valid_access_token(*, force_refresh: bool = False) -> str:
    creds = load_credentials()
    if creds is None:
        raise AntigravityOAuthError(
            "No Antigravity CLI OAuth credentials found. Run `agy` and complete Antigravity login first.",
            code="antigravity_oauth_not_logged_in",
        )

    if not force_refresh and not creds.access_token_expired():
        return creds.access_token

    refresh_token = creds.refresh_token
    with _refresh_inflight_lock:
        event = _refresh_inflight.get(refresh_token)
        if event is None:
            event = threading.Event()
            _refresh_inflight[refresh_token] = event
            owner = True
        else:
            owner = False

    if not owner:
        event.wait(timeout=LOCK_TIMEOUT_SECONDS)
        fresh = load_credentials()
        if fresh is not None and not fresh.access_token_expired():
            return fresh.access_token

    try:
        try:
            refreshed = refresh_access_token(refresh_token)
        except AntigravityOAuthError as exc:
            if exc.code == "antigravity_oauth_invalid_grant":
                logger.warning(
                    "Antigravity OAuth refresh token invalid (revoked/expired). "
                    "Clearing credentials at %s — user must re-login.",
                    _credentials_path(),
                )
                clear_credentials()
            raise

        access_token = str(refreshed.get("access_token") or "")
        if not access_token:
            raise AntigravityOAuthError(
                "Antigravity OAuth refresh response did not include an access token.",
                code="antigravity_oauth_refresh_invalid_response",
            )
        expires_in = int(refreshed.get("expires_in") or 3600)
        refreshed_creds = AntigravityCredentials(
            access_token=access_token,
            refresh_token=str(refreshed.get("refresh_token") or creds.refresh_token),
            token_type=str(refreshed.get("token_type") or creds.token_type or "Bearer"),
            expiry=datetime.now(timezone.utc) + timedelta(seconds=max(60, expires_in)),
            auth_method=creds.auth_method or "consumer",
            token_extras=dict(creds.token_extras or {}),
            top_level_extras=dict(creds.top_level_extras or {}),
        )
        save_credentials(refreshed_creds)
        return refreshed_creds.access_token
    finally:
        if owner:
            with _refresh_inflight_lock:
                _refresh_inflight.pop(refresh_token, None)
            event.set()
