"""Optional TypeSafe/Jev judgment for cron delivery outcomes.

The scheduler remains the source of truth for delivery. This module only asks Jev to
classify an already-observed outcome so a later notification aggregator can decide
whether to suppress, digest, or surface it. Jev never sends, retries, or marks a
message delivered.
"""

from __future__ import annotations

import json
import logging
import socket
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_DEFAULT_MODEL = "jev-latest"
_DEFAULT_TIMEOUT_SECONDS = 5.0
_MAX_ERROR_LENGTH = 160
_ANSWER_FIELDS = ("choice", "noul", "score", "confidence")


def _typesafe_config(config: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Return the cron delivery TypeSafe block, tolerating old/malformed configs."""
    if not isinstance(config, Mapping):
        return {}
    cron = config.get("cron")
    delivery = cron.get("delivery") if isinstance(cron, Mapping) else None
    block = delivery.get("typesafe") if isinstance(delivery, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def _read_api_key() -> str:
    """Read the profile-scoped secret without leaking another multiplexed profile."""
    try:
        from agent.secret_scope import get_secret

        return str(get_secret("TYPESAFE_API_KEY", "") or "").strip()
    except Exception:
        # A missing scope is a configuration problem, not a reason to break delivery.
        # Fail closed: never fall back to the process environment, which may contain
        # another profile's credential under gateway multiplexing.
        logger.debug("TypeSafe API key unavailable in the active profile secret scope")
        return ""


def _enabled(config: Optional[Mapping[str, Any]]) -> bool:
    block = _typesafe_config(config)
    return block.get("enabled") is True


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _delivery_status(
    delivery_error: Optional[str],
    unverified_targets: list[str],
    queued_targets: list[str],
    target_count: int,
) -> str:
    if delivery_error:
        return "delivery_failed"
    if queued_targets:
        return "delivery_queued"
    if unverified_targets:
        return "delivery_unverified"
    if target_count > 0:
        return "delivered"
    return "no_external_delivery"


def _build_state(
    job: Mapping[str, Any],
    *,
    delivery_error: Optional[str],
    unverified_targets: list[str],
    queued_targets: list[str],
    target_count: int,
    for_failure: bool,
    content: str,
    config: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a bounded, privacy-conscious state payload for Jev."""
    block = _typesafe_config(config)
    state: dict[str, Any] = {
        "kind": "hermes_cron_delivery",
        "job_id": str(job.get("id") or ""),
        "job_name": _truncate(job.get("name") or job.get("id") or "cron job", 120),
        "run_status": str(job.get("last_status") or "unknown"),
        "delivery_status": _delivery_status(
            delivery_error, unverified_targets, queued_targets, target_count),
        "delivery_error": _truncate(delivery_error, 500) if delivery_error else None,
        "unverified_targets": list(unverified_targets),
        "queued_targets": list(queued_targets),
        "target_count": max(0, int(target_count)),
        "failure_notice": bool(for_failure),
    }
    # Sending cron output to a third party is deliberately opt-in. The default state
    # is enough to classify delivery incidents and avoids exporting user content.
    if block.get("include_content") is True:
        try:
            max_chars = max(0, min(int(block.get("max_content_chars", 1200)), 4000))
        except (TypeError, ValueError):
            max_chars = 1200
        if max_chars:
            state["content_excerpt"] = _truncate(content, max_chars)
    return state


def _questions() -> dict[str, dict[str, Any]]:
    """Structured questions used by the first notification-aggregation slice."""
    return {
        "notification_class": {
            "type": "choice",
            "instructions": (
                "How should this already-recorded Hermes cron outcome be surfaced? "
                "Do not reinterpret a delivered message as failed."
            ),
            "criteria": {
                "silent_success": "Fully delivered, no warning, no user action, no new incident.",
                "brief_only": "Informative or non-critical; hold for the consolidated morning brief.",
                "immediate_alert": "A delivery failure, repeated/unverified delivery, or time-sensitive incident.",
                "human_review": "Conflicting, incomplete, or ambiguous evidence requiring an operator.",
            },
        },
        "incident_type": {
            "type": "choice",
            "instructions": "What is the primary operational category of this cron outcome?",
            "criteria": {
                "none": "No operational issue is evidenced.",
                "delivery_failure": "A target could not be delivered or returned an error.",
                "delivery_unverified": "A target accepted the send but supplied no positive evidence.",
                "delivery_queued": "Delivery was admitted to a durable queue and is not settled yet.",
                "partial_delivery": "Some part or target was delivered while another part was lost.",
                "provider_or_config": "The underlying provider, credentials, or configuration is implicated.",
                "other": "An incident exists but does not fit the other categories.",
            },
        },
        "action_required": {
            "type": "noul",
            "instructions": "Does this outcome require a human or automated follow-up beyond the normal digest?",
            "criteria": {
                "true": "A retry, investigation, approval, or immediate operator attention is warranted.",
                "false": "No follow-up is needed beyond normal storage or the morning brief.",
            },
        },
        "severity": {
            "type": "score",
            "instructions": "Rate the operational severity of this outcome.",
            "criteria": ["normal", "low", "medium", "high", "critical"],
        },
        "notify_now": {
            "type": "noul",
            "instructions": "Should this candidate bypass the morning digest and be surfaced immediately?",
            "criteria": {
                "true": "The incident is actionable or time-sensitive now.",
                "false": "It can be grouped or held for the morning digest.",
            },
        },
    }


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, URLError):
        return "network_error"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "invalid_response"
    return _truncate(type(exc).__name__, _MAX_ERROR_LENGTH)


def _answer_summary(answer: Any) -> dict[str, Any]:
    if not isinstance(answer, Mapping):
        return {}
    return {field: answer[field] for field in _ANSWER_FIELDS if field in answer}


def _normalize_response(response: Mapping[str, Any]) -> dict[str, Any]:
    raw_answers = response.get("answers")
    answers = raw_answers if isinstance(raw_answers, Mapping) else {}
    usage = response.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    return {
        "provider": "typesafe",
        "status": "ok",
        "model": str(response.get("model") or _DEFAULT_MODEL),
        "answers": {
            key: _answer_summary(value) for key, value in answers.items()
            if key in {"notification_class", "incident_type", "action_required", "severity", "notify_now"}
        },
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        },
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def evaluate_delivery(
    job: Mapping[str, Any],
    *,
    delivery_error: Optional[str],
    unverified_targets: list[str],
    queued_targets: list[str],
    target_count: int,
    for_failure: bool = False,
    content: str = "",
    config: Optional[Mapping[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Evaluate one deterministic delivery outcome with Jev.

    Returns ``None`` when disabled/local-only. Errors become a structured ``status=error``
    result and are intentionally not raised into the cron delivery path.
    """
    if not _enabled(config):
        return None
    if str(job.get("deliver") or "local").strip().lower() == "local":
        return None

    api_key = _read_api_key()
    if not api_key:
        return {
            "provider": "typesafe",
            "status": "not_configured",
            "reason": "missing_api_key",
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

    block = _typesafe_config(config)
    try:
        timeout = max(1.0, min(float(block.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)), 15.0))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SECONDS
    model = str(block.get("model") or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    state = _build_state(
        job,
        delivery_error=delivery_error,
        unverified_targets=unverified_targets,
        queued_targets=queued_targets,
        target_count=target_count,
        for_failure=for_failure,
        content=content,
        config=config,
    )
    payload = {"state": state, "model": model, "questions": _questions()}
    request = Request(
        _ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed HTTPS endpoint
            raw = response.read()
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, Mapping):
            raise ValueError("response is not an object")
        return _normalize_response(decoded)
    except Exception as exc:  # noqa: BLE001 - provider must never break delivery
        logger.warning(
            "TypeSafe delivery judgment failed for cron job %s: %s",
            job.get("id") or "?", _safe_error(exc))
        return {
            "provider": "typesafe",
            "status": "error",
            "error_type": _safe_error(exc),
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }


def evaluate_and_record_delivery(
    job: dict[str, Any],
    *,
    delivery_error: Optional[str],
    unverified_targets: list[str],
    queued_targets: list[str],
    target_count: int,
    for_failure: bool = False,
    content: str = "",
    config: Optional[Mapping[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Evaluate and persist the verdict without changing delivery state."""
    judgment = evaluate_delivery(
        job,
        delivery_error=delivery_error,
        unverified_targets=unverified_targets,
        queued_targets=queued_targets,
        target_count=target_count,
        for_failure=for_failure,
        content=content,
        config=config,
    )
    if judgment is None:
        return None
    job["last_delivery_judgment"] = judgment
    try:
        from cron.jobs import update_job

        update_job(job["id"], {"last_delivery_judgment": judgment})
    except Exception as exc:  # pragma: no cover - defensive; one-shot jobs may be gone
        logger.debug(
            "Job '%s': could not persist TypeSafe delivery judgment: %s",
            job.get("id", "?"), exc)
    return judgment
