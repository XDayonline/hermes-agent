from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from agent.anthropic_adapter import _is_oauth_token, resolve_anthropic_token
from hermes_cli.auth import _read_codex_tokens, resolve_codex_runtime_credentials
from hermes_cli.runtime_provider import resolve_runtime_provider


def _hermes_home() -> str:
    """Resolve HERMES_HOME from env or default."""
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")

def _load_antigravity_oauth_token() -> Optional[Dict[str, Any]]:
    """Read Antigravity OAuth token from disk."""
    path = os.path.join(_hermes_home(), "auth", "antigravity_oauth.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AccountUsageWindow:
    label: str
    used_percent: Optional[float] = None
    reset_at: Optional[datetime] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class AccountUsageSnapshot:
    provider: str
    source: str
    fetched_at: datetime
    title: str = "Account limits"
    plan: Optional[str] = None
    windows: tuple[AccountUsageWindow, ...] = ()
    details: tuple[str, ...] = ()
    unavailable_reason: Optional[str] = None

    @property
    def available(self) -> bool:
        return bool(self.windows or self.details) and not self.unavailable_reason


def _title_case_slug(value: Optional[str]) -> Optional[str]:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    return cleaned.replace("_", " ").replace("-", " ").title()


def _parse_dt(value: Any) -> Optional[datetime]:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _format_reset(dt: Optional[datetime]) -> str:
    if not dt:
        return "unknown"
    local_dt = dt.astimezone()
    delta = dt - _utc_now()
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return f"now ({local_dt.strftime('%Y-%m-%d %H:%M %Z')})"
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        rel = f"in {days}d {hours}h"
    elif hours > 0:
        rel = f"in {hours}h {minutes}m"
    else:
        rel = f"in {minutes}m"
    return f"{rel} ({local_dt.strftime('%Y-%m-%d %H:%M %Z')})"


def render_account_usage_lines(snapshot: Optional[AccountUsageSnapshot], *, markdown: bool = False) -> list[str]:
    if not snapshot:
        return []
    header = f"📈 {'**' if markdown else ''}{snapshot.title}{'**' if markdown else ''}"
    lines = [header]
    if snapshot.plan:
        lines.append(f"Provider: {snapshot.provider} ({snapshot.plan})")
    else:
        lines.append(f"Provider: {snapshot.provider}")
    for window in snapshot.windows:
        if window.used_percent is None:
            base = f"{window.label}: unavailable"
        else:
            remaining = max(0, round(100 - float(window.used_percent)))
            used = max(0, round(float(window.used_percent)))
            base = f"{window.label}: {remaining}% remaining ({used}% used)"
        if window.reset_at:
            base += f" • resets {_format_reset(window.reset_at)}"
        elif window.detail:
            base += f" • {window.detail}"
        lines.append(base)
    for detail in snapshot.details:
        lines.append(detail)
    if snapshot.unavailable_reason:
        lines.append(f"Unavailable: {snapshot.unavailable_reason}")
    return lines


def _resolve_codex_usage_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        normalized = "https://chatgpt.com/backend-api/codex"
    if normalized.endswith("/codex"):
        normalized = normalized[: -len("/codex")]
    if "/backend-api" in normalized:
        return normalized + "/wham/usage"
    return normalized + "/api/codex/usage"


def _resolve_codex_from_credential_pool() -> Optional[Dict[str, str]]:
    """Read Codex OAuth tokens from the new credential_pool format in auth.json."""
    try:
        from hermes_cli.auth import _load_auth_store
        auth_store = _load_auth_store()
        pool = auth_store.get("credential_pool") or {}
        codex_entries = pool.get("openai-codex") or []
        if not codex_entries:
            return None
        # Take the first (highest priority) entry
        entry = codex_entries[0] if isinstance(codex_entries, list) else codex_entries
        access_token = str(entry.get("access_token", "") or "").strip()
        if not access_token:
            return None
        base_url = str(entry.get("base_url", "") or "").strip()
        if not base_url:
            base_url = "https://chatgpt.com/backend-api/codex"
        return {
            "provider": "openai-codex",
            "base_url": base_url,
            "api_key": access_token,
            "source": "credential-pool",
        }
    except Exception:
        return None


def _fetch_codex_account_usage() -> Optional[AccountUsageSnapshot]:
    # Try old-format resolution first (providers.openai-codex in auth.json)
    try:
        creds = resolve_codex_runtime_credentials(refresh_if_expiring=True)
        token_data = _read_codex_tokens()
        tokens = token_data.get("tokens") or {}
        account_id = str(tokens.get("account_id", "") or "").strip() or None
    except Exception:
        # Fallback: credential_pool.openai-codex (new format)
        creds = _resolve_codex_from_credential_pool()
        if not creds:
            return None
        account_id = None  # not available in pool format
    headers = {
        "Authorization": f"Bearer {creds['api_key']}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    with httpx.Client(timeout=15.0) as client:
        response = client.get(_resolve_codex_usage_url(creds.get("base_url", "")), headers=headers)
        response.raise_for_status()
    payload = response.json() or {}
    rate_limit = payload.get("rate_limit") or {}
    windows: list[AccountUsageWindow] = []
    for key, label in (("primary_window", "Session"), ("secondary_window", "Weekly")):
        window = rate_limit.get(key) or {}
        used = window.get("used_percent")
        if used is None:
            continue
        windows.append(
            AccountUsageWindow(
                label=label,
                used_percent=float(used),
                reset_at=_parse_dt(window.get("reset_at")),
            )
        )
    details: list[str] = []
    credits = payload.get("credits") or {}
    if credits.get("has_credits"):
        balance = credits.get("balance")
        if isinstance(balance, (int, float)):
            details.append(f"Credits balance: ${float(balance):.2f}")
        elif credits.get("unlimited"):
            details.append("Credits balance: unlimited")
    return AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=_utc_now(),
        plan=_title_case_slug(payload.get("plan_type")),
        windows=tuple(windows),
        details=tuple(details),
    )


def _fetch_anthropic_account_usage() -> Optional[AccountUsageSnapshot]:
    token = (resolve_anthropic_token() or "").strip()
    if not token:
        return None
    if not _is_oauth_token(token):
        return AccountUsageSnapshot(
            provider="anthropic",
            source="oauth_usage_api",
            fetched_at=_utc_now(),
            unavailable_reason="Anthropic account limits are only available for OAuth-backed Claude accounts.",
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.0",
    }
    with httpx.Client(timeout=15.0) as client:
        response = client.get("https://api.anthropic.com/api/oauth/usage", headers=headers)
        response.raise_for_status()
    payload = response.json() or {}
    windows: list[AccountUsageWindow] = []
    mapping = (
        ("five_hour", "Current session"),
        ("seven_day", "Current week"),
        ("seven_day_opus", "Opus week"),
        ("seven_day_sonnet", "Sonnet week"),
    )
    for key, label in mapping:
        window = payload.get(key) or {}
        util = window.get("utilization")
        if util is None:
            continue
        used = float(util) * 100 if float(util) <= 1 else float(util)
        windows.append(
            AccountUsageWindow(
                label=label,
                used_percent=used,
                reset_at=_parse_dt(window.get("resets_at")),
            )
        )
    details: list[str] = []
    extra = payload.get("extra_usage") or {}
    if extra.get("is_enabled"):
        used_credits = extra.get("used_credits")
        monthly_limit = extra.get("monthly_limit")
        currency = extra.get("currency") or "USD"
        if isinstance(used_credits, (int, float)) and isinstance(monthly_limit, (int, float)):
            details.append(
                f"Extra usage: {used_credits:.2f} / {monthly_limit:.2f} {currency}"
            )
    return AccountUsageSnapshot(
        provider="anthropic",
        source="oauth_usage_api",
        fetched_at=_utc_now(),
        windows=tuple(windows),
        details=tuple(details),
    )


def _fetch_openrouter_account_usage(base_url: Optional[str], api_key: Optional[str]) -> Optional[AccountUsageSnapshot]:
    runtime = resolve_runtime_provider(
        requested="openrouter",
        explicit_base_url=base_url,
        explicit_api_key=api_key,
    )
    token = str(runtime.get("api_key", "") or "").strip()
    if not token:
        return None
    normalized = str(runtime.get("base_url", "") or "").rstrip("/")
    credits_url = f"{normalized}/credits"
    key_url = f"{normalized}/key"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=10.0) as client:
        credits_resp = client.get(credits_url, headers=headers)
        credits_resp.raise_for_status()
        credits = (credits_resp.json() or {}).get("data") or {}
        try:
            key_resp = client.get(key_url, headers=headers)
            key_resp.raise_for_status()
            key_data = (key_resp.json() or {}).get("data") or {}
        except Exception:
            key_data = {}
    total_credits = float(credits.get("total_credits") or 0.0)
    total_usage = float(credits.get("total_usage") or 0.0)
    details = [f"Credits balance: ${max(0.0, total_credits - total_usage):.2f}"]
    windows: list[AccountUsageWindow] = []
    limit = key_data.get("limit")
    limit_remaining = key_data.get("limit_remaining")
    limit_reset = str(key_data.get("limit_reset") or "").strip()
    usage = key_data.get("usage")
    if (
        isinstance(limit, (int, float))
        and float(limit) > 0
        and isinstance(limit_remaining, (int, float))
        and 0 <= float(limit_remaining) <= float(limit)
    ):
        limit_value = float(limit)
        remaining_value = float(limit_remaining)
        used_percent = ((limit_value - remaining_value) / limit_value) * 100
        detail_parts = [f"${remaining_value:.2f} of ${limit_value:.2f} remaining"]
        if limit_reset:
            detail_parts.append(f"resets {limit_reset}")
        windows.append(
            AccountUsageWindow(
                label="API key quota",
                used_percent=used_percent,
                detail=" • ".join(detail_parts),
            )
        )
    if isinstance(usage, (int, float)):
        usage_parts = [f"API key usage: ${float(usage):.2f} total"]
        for value, label in (
            (key_data.get("usage_daily"), "today"),
            (key_data.get("usage_weekly"), "this week"),
            (key_data.get("usage_monthly"), "this month"),
        ):
            if isinstance(value, (int, float)) and float(value) > 0:
                usage_parts.append(f"${float(value):.2f} {label}")
        details.append(" • ".join(usage_parts))
    return AccountUsageSnapshot(
        provider="openrouter",
        source="credits_api",
        fetched_at=_utc_now(),
        windows=tuple(windows),
        details=tuple(details),
    )


def _fetch_deepseek_balance() -> Optional[AccountUsageSnapshot]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=10.0) as client:
        response = client.get("https://api.deepseek.com/user/balance", headers=headers)
        response.raise_for_status()
    payload = response.json() or {}
    details: list[str] = []
    for info in payload.get("balance_infos") or []:
        currency = str(info.get("currency") or "").upper()
        total = info.get("total_balance", "0.00")
        symbol = "¥" if currency == "CNY" else "$"
        details.append(f"Balance ({currency}): {symbol}{total}")
    if not details and payload.get("is_available") is not None:
        details.append(f"Status: {'Available' if payload.get('is_available') else 'Low balance'}")
    return AccountUsageSnapshot(
        provider="deepseek",
        source="balance_api",
        fetched_at=_utc_now(),
        details=tuple(details),
    )


def _fetch_antigravity_quota() -> Optional[AccountUsageSnapshot]:
    try:
        from agent.antigravity_code_assist import retrieve_user_quota_antigravity
    except ImportError as exc:
        return AccountUsageSnapshot(
            provider="google-antigravity", source="oauth_quota_api",
            fetched_at=_utc_now(),
            unavailable_reason=f"Antigravity modules unavailable: {exc}",
        )
    token_data = _load_antigravity_oauth_token()
    if not token_data or "access" not in token_data:
        return AccountUsageSnapshot(
            provider="google-antigravity", source="oauth_quota_api",
            fetched_at=_utc_now(),
            unavailable_reason="Not logged in — run `agy auth login`",
        )
    access_token = token_data["access"]
    project_id = token_data.get("project_id", "")

    try:
        buckets = retrieve_user_quota_antigravity(access_token, project_id=project_id)
    except Exception as exc:
        return AccountUsageSnapshot(
            provider="google-antigravity", source="oauth_quota_api",
            fetched_at=_utc_now(),
            unavailable_reason=f"Quota lookup failed: {exc}",
        )
    if not buckets:
        return AccountUsageSnapshot(
            provider="google-antigravity", source="oauth_quota_api",
            fetched_at=_utc_now(),
            unavailable_reason="No quota buckets reported (free-tier or unmetered).",
        )
    # Known user-facing Antigravity models (filter out internal chat/tab buckets)
    _USER_FACING_PREFIXES = ("gemini-", "claude-", "gpt-")
    details: list[str] = []
    for b in sorted(buckets, key=lambda x: (x.model_id, x.token_type)):
        if not b.model_id.startswith(_USER_FACING_PREFIXES):
            continue
        pct = int(round(b.remaining_fraction * 100))
        label = b.model_id + (f" [{b.token_type}]" if b.token_type else "")
        details.append(f"{label}: {pct}% remaining")
    return AccountUsageSnapshot(
        provider="google-antigravity", source="oauth_quota_api",
        fetched_at=_utc_now(),
        details=tuple(details),
    )


# ---------------------------------------------------------------------------
# OpenCode Go dashboard scraping (for /quota)
# ---------------------------------------------------------------------------

_OPENCODE_GO_DASHBOARD_URL_PREFIX = "https://opencode.ai/workspace/"
_OPENCODE_GO_DASHBOARD_URL_SUFFIX = "/go"
_OPENCODE_GO_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Gecko/20100101 Firefox/148.0"


def _parse_opencode_go_window(html: str, field: str) -> Optional[dict]:
    """Parse SolidJS SSR hydration output for a usage window.

    Looks for ``<field>:$R[N]={...usagePercent:...resetInSec:...}`` in the HTML.
    """
    import re
    pct_first = re.compile(
        rf'{field}:\$R\[\d+\]=\{{[^}}]*usagePercent:(-?\d+(?:\.\d+)?)[^}}]*resetInSec:(-?\d+(?:\.\d+)?)[^}}]*\}}'
    )
    reset_first = re.compile(
        rf'{field}:\$R\[\d+\]=\{{[^}}]*resetInSec:(-?\d+(?:\.\d+)?)[^}}]*usagePercent:(-?\d+(?:\.\d+)?)[^}}]*\}}'
    )
    for m in (pct_first.search(html), reset_first.search(html)):
        if m:
            return {"usagePercent": max(0.0, float(m.group(1))), "resetInSec": max(0.0, float(m.group(2)))}
    return None


def _fetch_opencode_go_quota() -> Optional[AccountUsageSnapshot]:
    workspace_id = os.getenv("OPENCODE_GO_WORKSPACE_ID", "").strip()
    auth_cookie = os.getenv("OPENCODE_GO_AUTH_COOKIE", "").strip()
    if not workspace_id or not auth_cookie:
        return None

    from urllib.parse import quote
    url = f"{_OPENCODE_GO_DASHBOARD_URL_PREFIX}{quote(workspace_id)}{_OPENCODE_GO_DASHBOARD_URL_SUFFIX}"
    headers = {
        "User-Agent": _OPENCODE_GO_USER_AGENT,
        "Accept": "text/html",
        "Cookie": f"auth={auth_cookie}",
    }
    try:
        response = httpx.get(url, headers=headers, timeout=10.0, follow_redirects=True)
        response.raise_for_status()
    except Exception as exc:
        return AccountUsageSnapshot(
            provider="opencode-go", source="dashboard_scrape",
            fetched_at=_utc_now(),
            unavailable_reason=f"Dashboard scrape failed: {exc}",
        )

    html = response.text
    windows_info = {}
    now = datetime.now(timezone.utc)
    for field, label in (("rollingUsage", "5h"), ("weeklyUsage", "Weekly"), ("monthlyUsage", "Monthly")):
        parsed = _parse_opencode_go_window(html, field)
        if parsed:
            pct = int(round(parsed["usagePercent"]))
            remaining = max(0, 100 - pct)
            reset_dt = now.timestamp() + parsed["resetInSec"]
            windows_info[label] = f"{remaining}% remaining ({pct}% used)"

    if not windows_info:
        return AccountUsageSnapshot(
            provider="opencode-go", source="dashboard_scrape",
            fetched_at=_utc_now(),
            unavailable_reason="Could not parse OpenCode Go dashboard — verify WORKSPACE_ID and AUTH_COOKIE.",
        )

    details = [f"{label}: {info}" for label, info in windows_info.items()]
    return AccountUsageSnapshot(
        provider="opencode-go", source="dashboard_scrape",
        fetched_at=_utc_now(),
        details=tuple(details),
    )


# ---------------------------------------------------------------------------
# GitHub Copilot OAuth quota (GET /copilot_internal/user)
# ---------------------------------------------------------------------------

_COPILOT_INTERNAL_USER_URL = "https://api.github.com/copilot_internal/user"


def _fetch_copilot_quota() -> Optional[AccountUsageSnapshot]:
    try:
        from hermes_cli.auth import _load_auth_store
        auth_store = _load_auth_store()
        cp = auth_store.get("credential_pool") or {}
        entries = cp.get("copilot") or []
        if not entries:
            return None
        entry = entries[0] if isinstance(entries, list) else entries
        token = (entry.get("access_token") or "").strip()
        if not token:
            return None
    except Exception:
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "hermes-agent/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = httpx.get(_COPILOT_INTERNAL_USER_URL, headers=headers, timeout=10.0)
        response.raise_for_status()
        data = response.json() or {}
    except Exception as exc:
        return AccountUsageSnapshot(
            provider="github-copilot", source="internal_user_api",
            fetched_at=_utc_now(),
            unavailable_reason=f"Quota fetch failed: {exc}",
        )

    # Parse quota from the copilot_internal/user response
    chat = data.get("chat") or {}
    remaining = chat.get("remaining")
    limit = chat.get("limit")
    windows: list[AccountUsageWindow] = []
    details: list[str] = []

    if isinstance(remaining, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
        used_pct = max(0.0, (1.0 - float(remaining) / float(limit)) * 100)
        windows.append(AccountUsageWindow(
            label="Copilot Chat",
            used_percent=used_pct,
            detail=f"{int(remaining)}/{int(limit)} remaining",
        ))
    else:
        # Check for unlimited/copilot pro subscription
        chat_premium = data.get("chat_premium_enabled")
        individual = data.get("individual") or {}
        if chat_premium or individual.get("plan") in ("pro", "pro+"):
            details.append("Copilot Chat: active (unlimited or pro plan)")
        else:
            plan = individual.get("plan", "free")
            details.append(f"Copilot plan: {plan}")

    return AccountUsageSnapshot(
        provider="github-copilot",
        source="internal_user_api",
        fetched_at=_utc_now(),
        windows=tuple(windows),
        details=tuple(details),
    )


def fetch_account_usage(
    provider: Optional[str],
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[AccountUsageSnapshot]:
    normalized = str(provider or "").strip().lower()
    if normalized in {"", "auto", "custom"}:
        return None
    try:
        if normalized == "openai-codex":
            return _fetch_codex_account_usage()
        if normalized == "anthropic":
            return _fetch_anthropic_account_usage()
        if normalized == "openrouter":
            return _fetch_openrouter_account_usage(base_url, api_key)
        if normalized == "deepseek":
            return _fetch_deepseek_balance()
        if normalized == "google-antigravity":
            return _fetch_antigravity_quota()
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# fetch_all_providers_quota — for /quota command
# ---------------------------------------------------------------------------

# Providers with a real quota/balance API (detected via env var)
_QUOTA_FETCHERS: list[tuple[str, str, "callable"]] = [
    ("DEEPSEEK_API_KEY",    "deepseek",    lambda: _fetch_deepseek_balance()),
    ("OPENROUTER_API_KEY",  "openrouter",  lambda: _fetch_openrouter_account_usage(None, None)),
    ("ANTHROPIC_API_KEY",   "anthropic",   lambda: _fetch_anthropic_account_usage()),
]

# Dashboard-scraping providers (detected via separate env vars)
_SCRAPE_FETCHERS: list[tuple[str, str, list[str], "callable"]] = [
    ("opencode-go", "opencode-go", ["OPENCODE_GO_WORKSPACE_ID", "OPENCODE_GO_AUTH_COOKIE"],
     lambda: _fetch_opencode_go_quota()),
]

# OAuth-based providers — always attempted, graceful failure if not logged in
_OAUTH_QUOTA_FETCHERS: list[tuple[str, "callable"]] = [
    ("openai-codex",       lambda: _fetch_codex_account_usage()),
    ("google-antigravity", lambda: _fetch_antigravity_quota()),
]

# Providers with a key but no public quota API
_KEY_ONLY_PROVIDERS: list[tuple[str, str, str]] = [
    ("DASHSCOPE_API_KEY",     "alibaba",                "Alibaba DashScope — quota N/A via API"),
    ("MINIMAX_API_KEY",       "minimax",                "MiniMax — quota N/A via API"),
]


def fetch_all_providers_quota() -> list[AccountUsageSnapshot]:
    """Fetch quota/balance for every configured provider.

    - API-key providers: detected via env vars
    - OAuth providers (openai-codex, google-antigravity): always attempted,
      return unavailable_reason if not logged in (non-fatal)
    - Key-only providers: listed with unavailable_reason if key is set
    """
    results: list[AccountUsageSnapshot] = []

    # API-key providers
    for env_key, provider_id, fetcher in _QUOTA_FETCHERS:
        if not os.getenv(env_key, "").strip():
            continue
        try:
            snapshot = fetcher()
        except Exception:
            snapshot = None
        if snapshot is not None:
            results.append(snapshot)

    # OAuth providers — always try, gracefully fail
    for provider_id, fetcher in _OAUTH_QUOTA_FETCHERS:
        try:
            snapshot = fetcher()
        except Exception:
            snapshot = None
        if snapshot is not None:
            results.append(snapshot)

    # Dashboard-scraping providers
    for provider_id, label, env_vars, fetcher in _SCRAPE_FETCHERS:
        if not all(os.getenv(v, "").strip() for v in env_vars):
            continue
        try:
            snapshot = fetcher()
        except Exception:
            snapshot = None
        if snapshot is not None:
            results.append(snapshot)

    # Key-only providers
    for env_key, provider_id, reason in _KEY_ONLY_PROVIDERS:
        env_present = bool(os.getenv(env_key, "").strip()) if env_key else False
        if not env_present:
            continue
        results.append(AccountUsageSnapshot(
            provider=provider_id,
            source="env_detected",
            fetched_at=_utc_now(),
            unavailable_reason=reason,
        ))

    # Credential-pool detected providers (no env var, but OAuth configured)
    _CREDENTIAL_POOL_KEY_ONLY = ["cursor", "zai", "kimi-for-coding", "alibaba-coding-plan"]
    _CREDENTIAL_POOL_FETCHERS = [
        ("copilot", lambda: _fetch_copilot_quota()),
    ]
    try:
        from hermes_cli.auth import _load_auth_store
        auth_store = _load_auth_store()
        cp = auth_store.get("credential_pool") or {}

        # Providers with real fetchers
        for provider_id, fetcher in _CREDENTIAL_POOL_FETCHERS:
            entries = cp.get(provider_id)
            if not entries or not (isinstance(entries, list) and len(entries) > 0):
                continue
            e = entries[0]
            if e.get("access_token") and not e.get("last_error_reason"):
                try:
                    snapshot = fetcher()
                except Exception:
                    snapshot = None
                if snapshot is not None:
                    results.append(snapshot)

        # Key-only credential pool providers
        for provider_id in _CREDENTIAL_POOL_KEY_ONLY:
            entries = cp.get(provider_id)
            if not entries or not (isinstance(entries, list) and len(entries) > 0):
                continue
            e = entries[0]
            if e.get("access_token") and not e.get("last_error_reason"):
                results.append(AccountUsageSnapshot(
                    provider=provider_id,
                    source="credential_pool",
                    fetched_at=_utc_now(),
                    unavailable_reason="Quota via OAuth only — not yet implemented",
                ))
    except Exception:
        pass

    return results
