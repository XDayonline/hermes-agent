"""Tests for the optional TypeSafe/Jev cron-delivery judgment."""

from __future__ import annotations

import json

from cron import typesafe_delivery as ts


class _Response:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._payload


def _config(**overrides):
    block = {
        "enabled": True,
        "model": "jev-latest",
        "timeout_seconds": 5,
        "include_content": False,
    }
    block.update(overrides)
    return {"cron": {"delivery": {"typesafe": block}}}


def _job():
    return {"id": "job-1", "name": "Morning brief", "deliver": "telegram:-100:42"}


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(ts, "_read_api_key", lambda: "secret-that-must-not-escape")
    assert ts.evaluate_delivery(
        _job(), delivery_error=None, unverified_targets=[], queued_targets=[],
        target_count=1, config=None,
    ) is None


def test_missing_key_is_structured_and_does_not_call_network(monkeypatch):
    called = []
    monkeypatch.setattr(ts, "_read_api_key", lambda: "")
    monkeypatch.setattr(ts, "urlopen", lambda *args, **kwargs: called.append(args))

    result = ts.evaluate_delivery(
        _job(), delivery_error="telegram returned 500", unverified_targets=[],
        queued_targets=[], target_count=1, config=_config(),
    )

    assert result is not None
    assert result["status"] == "not_configured"
    assert result["reason"] == "missing_api_key"
    assert called == []


def test_request_uses_structured_questions_and_keeps_content_out_by_default(monkeypatch):
    captured = {}
    response = {
        "model": "jev-1.13.0",
        "answers": {
            "notification_class": {
                "type": "choice", "choice": "immediate_alert", "confidence": 0.91,
            },
            "incident_type": {
                "type": "choice", "choice": "delivery_failure", "confidence": 0.94,
            },
            "action_required": {"type": "noul", "noul": 0.97},
            "severity": {"type": "score", "score": 3.7, "confidence": 0.88},
            "notify_now": {"type": "noul", "noul": 0.96},
        },
        "usage": {"input_tokens": 123, "output_tokens": 45},
    }

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(response)

    monkeypatch.setattr(ts, "_read_api_key", lambda: "test-key")
    monkeypatch.setattr(ts, "urlopen", fake_urlopen)

    result = ts.evaluate_delivery(
        _job(),
        delivery_error="telegram returned 500",
        unverified_targets=[],
        queued_targets=[],
        target_count=1,
        content="A private cron report that must not be exported by default.",
        config=_config(),
    )

    assert result is not None
    body = json.loads(captured["request"].data.decode("utf-8"))
    assert captured["request"].full_url == "https://api.typesafe.ai/v1/systemone"
    assert captured["timeout"] == 5.0
    assert body["model"] == "jev-latest"
    assert "content_excerpt" not in body["state"]
    assert body["state"]["delivery_status"] == "delivery_failed"
    assert body["questions"]["notification_class"]["type"] == "choice"
    assert body["questions"]["action_required"]["type"] == "noul"
    assert result["status"] == "ok"
    assert result["answers"]["notification_class"]["choice"] == "immediate_alert"
    assert result["usage"] == {"input_tokens": 123, "output_tokens": 45}


def test_content_requires_explicit_opt_in(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Response({"model": "jev-latest", "answers": {}, "usage": {}})

    monkeypatch.setattr(ts, "_read_api_key", lambda: "test-key")
    monkeypatch.setattr(ts, "urlopen", fake_urlopen)
    ts.evaluate_delivery(
        _job(), delivery_error=None, unverified_targets=[], queued_targets=[],
        target_count=1, content="visible only with opt-in",
        config=_config(include_content=True, max_content_chars=20),
    )
    assert captured["body"]["state"]["content_excerpt"] == "visible only with..."


def test_persistence_is_best_effort(monkeypatch):
    updates = []
    monkeypatch.setattr(ts, "_read_api_key", lambda: "")
    monkeypatch.setattr("cron.jobs.update_job", lambda job_id, values: updates.append((job_id, values)))

    job = _job()
    result = ts.evaluate_and_record_delivery(
        job, delivery_error=None, unverified_targets=["telegram:-100:42"],
        queued_targets=[], target_count=1, config=_config(),
    )

    assert result is not None
    assert result["status"] == "not_configured"
    assert job["last_delivery_judgment"]["reason"] == "missing_api_key"
    assert updates == [("job-1", {"last_delivery_judgment": result})]


def test_local_output_is_not_sent_to_typesafe(monkeypatch):
    job = {"id": "local", "deliver": "local"}
    monkeypatch.setattr(ts, "_read_api_key", lambda: "secret")
    assert ts.evaluate_delivery(
        job, delivery_error=None, unverified_targets=[], queued_targets=[],
        target_count=0, config=_config(),
    ) is None
