from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

import httpx

from agent.anthropic_adapter import _is_oauth_token, resolve_anthropic_token
from hermes_cli.auth import _read_codex_tokens, resolve_codex_runtime_credentials
from hermes_cli.runtime_provider import resolve_runtime_provider

if TYPE_CHECKING:
    from typing import TypeGuard

logger = logging.getLogger(__name__)


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


def _fmt_usd(d: float) -> str:
    return f"${d:,.2f}"


def _is_finite_num(v: Any) -> TypeGuard[float]:
    """True iff v is a real numeric value (int or float, not bool, not NaN/Inf).

    Typed as a ``TypeGuard[float]`` so the type checker narrows ``v`` to a real
    number in the positive branch — callers can then do arithmetic / pass it to
    ``_fmt_usd`` without a None-operand warning.
    """
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def build_nous_credits_snapshot(account_info) -> Optional[AccountUsageSnapshot]:
    """Map a NousPortalAccountInfo into an AccountUsageSnapshot for /usage.

    Shows dollar magnitudes (subscription / top-up / total) + renewal date + a
    portal CTA. When the portal supplies a subscription denominator
    (``monthly_credits``), also emits a subscription-usage window so the renderer
    shows a real ``% used`` gauge; when it's absent (older portals) the view
    gracefully degrades to magnitudes-only. Returns None when there's no usable
    account info to show (fail-open: caller just shows nothing).
    """
    try:
        from hermes_cli.nous_account import nous_portal_topup_url

        if account_info is None or not getattr(account_info, "logged_in", False):
            return None

        access = getattr(account_info, "paid_service_access_info", None)
        sub = getattr(account_info, "subscription", None)

        windows: list[AccountUsageWindow] = []
        details: list[str] = []

        if sub is not None:
            monthly_credits = getattr(sub, "monthly_credits", None)
            sub_remaining = getattr(sub, "credits_remaining", None)
            if (
                _is_finite_num(monthly_credits)
                and monthly_credits > 0
                and _is_finite_num(sub_remaining)
                and sub_remaining <= monthly_credits
            ):
                used = monthly_credits - sub_remaining
                used_pct = max(0.0, min(100.0, used / monthly_credits * 100.0))
                windows.append(
                    AccountUsageWindow(
                        label="Subscription",
                        used_percent=used_pct,
                        detail=f"{_fmt_usd(sub_remaining)} of {_fmt_usd(monthly_credits)} left",
                    )
                )

        if access is not None:
            sub_credits = getattr(access, "subscription_credits_remaining", None)
            if _is_finite_num(sub_credits):
                details.append(f"Subscription credits: {_fmt_usd(sub_credits)}")
            purchased = getattr(access, "purchased_credits_remaining", None)
            if _is_finite_num(purchased):
                details.append(f"Top-up credits: {_fmt_usd(purchased)}")
            total_usable = getattr(access, "total_usable_credits", None)
            if _is_finite_num(total_usable):
                details.append(f"Total usable: {_fmt_usd(total_usable)}")

        if sub is not None:
            rollover = getattr(sub, "rollover_credits", None)
            if _is_finite_num(rollover) and rollover > 0:
                details.append(f"Rollover: {_fmt_usd(rollover)}")
            period_end = getattr(sub, "current_period_end", None)
            if period_end:
                details.append(f"Renews: {period_end}")

        paid = getattr(account_info, "paid_service_access", None)
        if paid is False:
            details.append("Status: access depleted — top up to restore")

        if not windows and not details:
            return None

        details.append(f"Top up: {nous_portal_topup_url(account_info)}")
        details.append("(or run /credits)")

        plan = getattr(sub, "plan", None) if sub is not None else None
        return AccountUsageSnapshot(
            provider="nous",
            source="portal-account",
            fetched_at=_utc_now(),
            title="Nous credits",
            plan=plan,
            windows=tuple(windows),
            details=tuple(details),
        )
    except (AttributeError, TypeError):
        return None


def nous_credits_lines(*, markdown: bool = False, timeout: float = 10.0) -> list[str]:
    """Return rendered Nous-credits /usage lines, or [] when there's nothing to show.

    Account-independent of any live agent: gated on "a Nous account is logged in"
    (a cheap local auth-state check), then a wall-clock-bounded portal fetch. Shared
    by the CLI ``_show_usage`` and the TUI ``session.usage`` RPC so both surfaces show
    the same block regardless of session API-call count or resume state. Fail-open:
    any auth/portal hiccup or timeout returns [] (the caller shows nothing).

    Dev override: when HERMES_DEV_CREDITS_FIXTURE selects a fixture state, /usage
    renders from that fixture instead of the real portal (so the block + gauge are
    testable without a live account). Throwaway scaffolding.
    """
    try:
        from agent.credits_tracker import dev_fixture_credits_state

        fixture = dev_fixture_credits_state()
    except Exception:
        fixture = None
    if fixture is not None:
        snapshot = _snapshot_from_credits_state(fixture)
        return render_account_usage_lines(snapshot, markdown=markdown)

    try:
        from hermes_cli.auth import get_provider_auth_state

        tok = (get_provider_auth_state("nous") or {}).get("access_token")
        if not (isinstance(tok, str) and tok.strip()):
            return []
    except Exception:
        return []
    try:
        import concurrent.futures

        from hermes_cli.nous_account import get_nous_portal_account_info

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            account = pool.submit(
                get_nous_portal_account_info, force_fresh=True
            ).result(timeout=timeout)
        snapshot = build_nous_credits_snapshot(account)
        return render_account_usage_lines(snapshot, markdown=markdown)
    except Exception:
        logger.debug("credits ▸ /usage portal fetch/render failed (fail-open)", exc_info=True)
        return []


def _snapshot_from_credits_state(state) -> Optional[AccountUsageSnapshot]:
    try:
        if state is None:
            return None

        windows: list[AccountUsageWindow] = []
        details: list[str] = []

        uf = getattr(state, "used_fraction", None)
        if isinstance(uf, (int, float)) and math.isfinite(uf):
            cap_usd = getattr(state, "subscription_limit_usd", None)
            sub_usd = getattr(state, "subscription_usd", None)
            detail = None
            if sub_usd and cap_usd:
                detail = f"${sub_usd} of ${cap_usd} left"
            windows.append(
                AccountUsageWindow(
                    label="Subscription",
                    used_percent=max(0.0, min(100.0, uf * 100.0)),
                    detail=detail,
                )
            )

        sub_usd = getattr(state, "subscription_usd", None)
        if sub_usd:
            details.append(f"Subscription credits: ${sub_usd}")
        purchased_usd = getattr(state, "purchased_usd", None)
        if purchased_usd:
            details.append(f"Top-up credits: ${purchased_usd}")
        remaining_usd = getattr(state, "remaining_usd", None)
        if remaining_usd:
            details.append(f"Total usable: ${remaining_usd}")
        if getattr(state, "paid_access", True) is False:
            details.append("Status: access depleted — top up to restore")

        if not windows and not details:
            return None

        details.append("(dev fixture — HERMES_DEV_CREDITS_FIXTURE)")
        return AccountUsageSnapshot(
            provider="nous",
            source="dev-fixture",
            fetched_at=_utc_now(),
            title="Nous credits",
            windows=tuple(windows),
            details=tuple(details),
        )
    except (AttributeError, TypeError):
        return None


@dataclass(frozen=True)
class CreditsView:
    logged_in: bool
    balance_lines: tuple[str, ...] = ()
    identity_line: Optional[str] = None
    topup_url: Optional[str] = None
    depleted: bool = False


def build_credits_view(*, markdown: bool = False, timeout: float = 10.0) -> CreditsView:
    not_logged_in = CreditsView(logged_in=False)
    try:
        from hermes_cli.auth import get_provider_auth_state

        tok = (get_provider_auth_state("nous") or {}).get("access_token")
        if not (isinstance(tok, str) and tok.strip()):
            return not_logged_in
    except Exception:
        return not_logged_in

    try:
        import concurrent.futures

        from hermes_cli.nous_account import (
            get_nous_portal_account_info,
            nous_portal_topup_url,
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            account = pool.submit(get_nous_portal_account_info, force_fresh=True).result(
                timeout=timeout
            )
    except Exception:
        logger.debug("credits ▸ /credits portal fetch failed (fail-open)", exc_info=True)
        return not_logged_in

    if account is None or not getattr(account, "logged_in", False):
        return not_logged_in

    snapshot = build_nous_credits_snapshot(account)
    balance_lines: list[str] = []
    if snapshot is not None:
        rendered = render_account_usage_lines(snapshot, markdown=markdown)
        balance_lines = [
            line
            for line in rendered
            if not line.lstrip().startswith("Top up:")
            and not line.lstrip().startswith("(or run")
        ]

    email = getattr(account, "email", None)
    org_name = getattr(account, "org_name", None)
    who: list[str] = []
    if email:
        who.append(str(email))
    if org_name:
        who.append(f"org {org_name}")
    identity_line = ("Topping up as " + " / ".join(who)) if who else None

    return CreditsView(
        logged_in=True,
        balance_lines=tuple(balance_lines),
        identity_line=identity_line,
        topup_url=nous_portal_topup_url(account),
        depleted=getattr(account, "paid_service_access", None) is False,
    )


def _resolve_codex_usage_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        normalized = "https://chatgpt.com/backend-api/codex"
    if normalized.endswith("/codex"):
        normalized = normalized[: -len("/codex")]
    if "/backend-api" in normalized:
        return normalized + "/wham/usage"
    return normalized + "/api/codex/usage"


def _resolve_codex_from_credential_pool() -> Optional[dict[str, str]]:
    """Read Codex OAuth tokens from the new credential_pool format in auth.json."""
    try:
        from hermes_cli.auth import _load_auth_store

        auth_store = _load_auth_store()
        pool = auth_store.get("credential_pool") or {}
        codex_entries = pool.get("openai-codex") or []
        if not codex_entries:
            return None
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
    try:
        creds = resolve_codex_runtime_credentials(refresh_if_expiring=True)
        token_data = _read_codex_tokens()
        tokens = token_data.get("tokens") or {}
        account_id = str(tokens.get("account_id", "") or "").strip() or None
    except Exception:
        creds = _resolve_codex_from_credential_pool()
        if not creds:
            return None
        account_id = None
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


def _load_antigravity_oauth_token() -> Optional[dict]:
    """Return Antigravity access token/project for /quota tests and runtime."""
    try:
        from agent import antigravity_oauth

        access_token = antigravity_oauth.get_valid_access_token()
        creds = antigravity_oauth.load_credentials()
        project_id = ""
        if creds:
            project_id = str(
                getattr(creds, "project_id", "")
                or getattr(creds, "top_level_extras", {}).get("project_id", "")
                or ""
            ).strip()
        return {"access": access_token, "project_id": project_id}
    except Exception:
        return None


def _fetch_antigravity_quota() -> Optional[AccountUsageSnapshot]:
    try:
        from agent.antigravity_code_assist import retrieve_user_quota_antigravity
    except ImportError as exc:
        return AccountUsageSnapshot(
            provider="google-antigravity", source="oauth_quota_api",
            fetched_at=_utc_now(),
            unavailable_reason=f"Antigravity modules unavailable: {exc}",
        )
    token_info = _load_antigravity_oauth_token()
    if not token_info:
        return AccountUsageSnapshot(
            provider="google-antigravity", source="oauth_quota_api",
            fetched_at=_utc_now(),
            unavailable_reason="Not logged in — run `agy` login / install flow",
        )
    access_token = str(token_info.get("access") or "").strip()
    project_id = str(token_info.get("project_id") or "").strip()
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
    """Parse SolidJS SSR hydration output for a usage window."""
    import re
    pct_first = re.compile(
        rf'{field}:\$R\[\d+\]=\{{[^}}]*usagePercent:(-?\d+(?:\.\d+)?)[^}}]*resetInSec:(-?\d+(?:\.\d+)?)[^}}]*\}}'
    )
    reset_first = re.compile(
        rf'{field}:\$R\[\d+\]=\{{[^}}]*resetInSec:(-?\d+(?:\.\d+)?)[^}}]*usagePercent:(-?\d+(?:\.\d+)?)[^}}]*\}}'
    )
    for m in (pct_first.search(html), reset_first.search(html)):
        if not m:
            continue
        first = max(0.0, float(m.group(1)))
        second = max(0.0, float(m.group(2)))
        if m.re is pct_first:
            return {"usagePercent": first, "resetInSec": second}
        return {"usagePercent": second, "resetInSec": first}
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


# =========================================================================
# Single-provider fetch (for /usage)
# =========================================================================


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


# =========================================================================
# fetch_all_providers_quota — for /quota command
# =========================================================================

_QUOTA_FETCHERS: list[tuple[str, str, callable]] = [
    ("DEEPSEEK_API_KEY",    "deepseek",    lambda: _fetch_deepseek_balance()),
    ("OPENROUTER_API_KEY",  "openrouter",  lambda: _fetch_openrouter_account_usage(None, None)),
    ("ANTHROPIC_API_KEY",   "anthropic",   lambda: _fetch_anthropic_account_usage()),
]

_SCRAPE_FETCHERS: list[tuple[str, str, list[str], callable]] = [
    ("opencode-go", "opencode-go", ["OPENCODE_GO_WORKSPACE_ID", "OPENCODE_GO_AUTH_COOKIE"],
     lambda: _fetch_opencode_go_quota()),
]

_OAUTH_QUOTA_FETCHERS: list[tuple[str, callable]] = [
    ("openai-codex",       lambda: _fetch_codex_account_usage()),
    ("google-antigravity", lambda: _fetch_antigravity_quota()),
]

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

    for env_key, provider_id, fetcher in _QUOTA_FETCHERS:
        if not os.getenv(env_key, "").strip():
            continue
        try:
            snapshot = fetcher()
        except Exception:
            snapshot = None
        if snapshot is not None:
            results.append(snapshot)

    for provider_id, fetcher in _OAUTH_QUOTA_FETCHERS:
        try:
            snapshot = fetcher()
        except Exception:
            snapshot = None
        if snapshot is not None:
            results.append(snapshot)

    for provider_id, label, env_vars, fetcher in _SCRAPE_FETCHERS:
        if not all(os.getenv(v, "").strip() for v in env_vars):
            continue
        try:
            snapshot = fetcher()
        except Exception:
            snapshot = None
        if snapshot is not None:
            results.append(snapshot)

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

    # Credential-pool detected providers
    _CREDENTIAL_POOL_KEY_ONLY = ["cursor", "zai", "kimi-for-coding", "alibaba-coding-plan"]
    _CREDENTIAL_POOL_FETCHERS = [
        ("copilot", lambda: _fetch_copilot_quota()),
    ]
    try:
        from hermes_cli.auth import _load_auth_store
        auth_store = _load_auth_store()
        cp = auth_store.get("credential_pool") or {}

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
