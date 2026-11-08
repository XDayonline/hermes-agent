"""OpenAI-compatible facade for Antigravity native OAuth inference."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional

import httpx

from agent import antigravity_oauth
from agent.antigravity_code_assist import (
    ANTIGRAVITY_CODE_ASSIST_ENDPOINT,
    CodeAssistError,
    ProjectContext,
    build_headers,
    resolve_project_context,
)
from agent.gemini_cloudcode_adapter import (
    GeminiCloudCodeClient,
    _resolve_antigravity_model_id,
    _GeminiStreamChunk,
    _gemini_http_error,
    _iter_sse_events,
    _translate_gemini_response,
    _translate_stream_event,
    build_gemini_request,
    wrap_code_assist_request,
)

logger = logging.getLogger(__name__)

MARKER_BASE_URL = "antigravity-pa://google"

# JSON-Schema combinators Anthropic rejects anywhere inside ``input_schema`` when a Claude tier
# is served through Code Assist.
_COMBINATOR_KEYS = ("anyOf", "oneOf", "allOf")


def _collapse_schema_combinators(node: Any) -> Any:
    """Replace every remaining union/combinator node by a permissive shell.

    Claude through Code Assist 400s the whole turn with "JSON schema is invalid. It must match
    JSON Schema draft 2020-12" when ``input_schema`` still contains ``anyOf``/``oneOf``/``allOf``
    at ANY depth; the provider then falls back, so the user's chosen tier silently runs on another
    model. ``tools.schema_sanitizer`` only collapses *nullable* unions and strips combinators at
    the top level, so a genuine union (``boolean | array``, per-action operation shapes) survives.

    The shell keeps the human description — handlers validate their own arguments — and the input
    is never mutated, so callers holding the original tool list are unaffected.
    """
    if isinstance(node, list):
        return [_collapse_schema_combinators(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: _collapse_schema_combinators(value) for key, value in node.items()}
    if any(key in out for key in _COMBINATOR_KEYS):
        description = out.get("description") or out.get("title")
        return {"description": description} if isinstance(description, str) and description else {}
    return out


def _prepare_tools_for_claude(tools: Any, wire_model: str) -> Any:
    """Collapse schema combinators for Claude tiers only; other families keep their unions."""
    if not isinstance(tools, list) or not tools:
        return tools
    if not str(wire_model or "").lower().startswith("claude"):
        return tools
    prepared: list[Any] = []
    collapsed: list[str] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("parameters"), dict):
            prepared.append(tool)
            continue
        parameters = _collapse_schema_combinators(fn["parameters"])
        if parameters != fn["parameters"]:
            collapsed.append(str(fn.get("name", "<tool>")))
        prepared.append({**tool, "function": {**fn, "parameters": parameters}})
    if collapsed:
        logger.debug(
            "antigravity: collapsed JSON-Schema unions for Claude tier in %s (they 400 "
            "input_schema validation)", ", ".join(collapsed),
        )
    return prepared


def _dump_rejected_payload(status: int, wrapped: dict[str, Any]) -> None:
    """Persist the exact Code Assist payload Google rejected (debug aid)."""
    try:
        import json as _json
        import os as _os
        import pathlib as _pathlib
        import time as _time

        path = _pathlib.Path(
            _os.environ.get("HERMES_AGY_DUMP", "/home/gsc/.hermes/logs/agy-reject.json")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _json.dumps(
                {"status": status, "ts": _time.time(), "wrapped": wrapped},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


class AntigravityCloudCodeClient(GeminiCloudCodeClient):
    """Minimal OpenAI-SDK-compatible facade over Antigravity Code Assist."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_headers: Optional[dict[str, str]] = None,
        project_id: str = "",
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key or "antigravity-oauth",
            base_url=base_url or MARKER_BASE_URL,
            default_headers=default_headers,
            project_id=project_id,
            **kwargs,
        )

    def _ensure_project_context(self, access_token: str, model: str) -> ProjectContext:
        if self._project_context is not None:
            return self._project_context  # type: ignore[return-value]

        env_project = antigravity_oauth.resolve_project_id_from_env()
        creds = antigravity_oauth.load_credentials()
        stored_project = creds.project_id if creds else ""
        if stored_project:
            self._project_context = ProjectContext(
                project_id=stored_project,
                managed_project_id=creds.managed_project_id if creds else "",
                source="stored",
            )
            return self._project_context

        ctx = resolve_project_context(
            access_token,
            configured_project_id=self._configured_project_id,
            env_project_id=env_project,
        )
        if ctx.project_id or ctx.managed_project_id:
            antigravity_oauth.update_project_ids(
                project_id=ctx.project_id,
                managed_project_id=ctx.managed_project_id,
            )
        self._project_context = ctx
        return ctx

    def _create_chat_completion(
        self,
        *,
        model: str = "gemini-3-flash-agent",
        messages: Optional[list[dict[str, Any]]] = None,
        stream: bool = False,
        tools: Any = None,
        tool_choice: Any = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        stop: Any = None,
        extra_body: Optional[dict[str, Any]] = None,
        timeout: Any = None,
        **_: Any,
    ) -> Any:
        access_token = antigravity_oauth.get_valid_access_token()
        requested_model = model
        wire_model = _resolve_antigravity_model_id(model)
        # Claude tiers served through Code Assist reject any nested JSON-Schema combinator in
        # ``input_schema``; collapse them before the payload is built.
        tools = _prepare_tools_for_claude(tools, wire_model)
        ctx = self._ensure_project_context(access_token, wire_model)

        thinking_config = None
        if isinstance(extra_body, dict):
            thinking_config = extra_body.get("thinking_config") or extra_body.get("thinkingConfig")

        inner = build_gemini_request(
            messages=messages or [],
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            thinking_config=thinking_config,
        )
        wrapped = wrap_code_assist_request(
            project_id=ctx.project_id,
            model=wire_model,
            inner_request=inner,
        )

        headers = build_headers(access_token)
        headers.update(self._default_headers)

        if stream:
            return self._stream_completion(model=requested_model, wrapped=wrapped, headers=headers)

        url = f"{ANTIGRAVITY_CODE_ASSIST_ENDPOINT}/v1internal:generateContent"
        response = self._http.post(url, json=wrapped, headers=headers)
        if response.status_code != 200:
            _dump_rejected_payload(response.status_code, wrapped)
            raise _gemini_http_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise CodeAssistError(
                f"Invalid JSON from Antigravity Code Assist: {exc}",
                code="antigravity_code_assist_invalid_json",
            ) from exc
        return _translate_gemini_response(payload, model=requested_model)

    def _stream_completion(
        self,
        *,
        model: str,
        wrapped: dict[str, Any],
        headers: dict[str, str],
    ) -> Iterator[_GeminiStreamChunk]:
        url = f"{ANTIGRAVITY_CODE_ASSIST_ENDPOINT}/v1internal:streamGenerateContent?alt=sse"
        stream_headers = dict(headers)
        stream_headers["Accept"] = "text/event-stream"

        def _generator() -> Iterator[_GeminiStreamChunk]:
            try:
                with self._http.stream("POST", url, json=wrapped, headers=stream_headers) as response:
                    if response.status_code != 200:
                        response.read()
                        _dump_rejected_payload(response.status_code, wrapped)
                        raise _gemini_http_error(response)
                    tool_call_counter: list[int] = [0]
                    for event in _iter_sse_events(response):
                        yield from _translate_stream_event(event, model, tool_call_counter)
            except httpx.HTTPError as exc:
                raise CodeAssistError(
                    f"Antigravity streaming request failed: {exc}",
                    code="antigravity_code_assist_stream_error",
                ) from exc

        return _generator()
