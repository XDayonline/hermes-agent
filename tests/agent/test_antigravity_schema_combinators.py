import copy

from agent.antigravity_cloudcode_adapter import (
    _collapse_schema_combinators,
    _prepare_tools_for_claude,
)


def _tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "skill_manage",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operations": {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"type": "object", "properties": {"content": {"type": "string"}},
                                     "required": ["content"]},
                                    {"type": "object", "properties": {"old_string": {"type": "string"}}},
                                ]
                            },
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "notify": {"anyOf": [{"type": "boolean"}, {"type": "array"}]},
                        "command": {"type": "string"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}},
        },
    ]


def test_collapse_removes_nested_combinators_and_keeps_description():
    schema = {
        "type": "object",
        "properties": {
            "notify": {"description": "noise or patterns",
                       "anyOf": [{"type": "boolean"}, {"type": "array"}]},
            "nested": {"type": "array", "items": {"oneOf": [{"type": "string"}, {"type": "integer"}]}},
            "command": {"type": "string"},
        },
    }
    result = _collapse_schema_combinators(schema)
    assert "anyOf" not in repr(result)
    assert "oneOf" not in repr(result)
    assert result["properties"]["notify"] == {"description": "noise or patterns"}
    assert result["properties"]["nested"]["items"] == {}
    assert result["properties"]["command"] == {"type": "string"}


def test_collapse_leaves_plain_schemas_unchanged():
    schema = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    original = copy.deepcopy(schema)
    assert _collapse_schema_combinators(schema) == original
    assert schema == original


def test_prepare_tools_collapses_only_for_claude_tiers():
    gemini = _prepare_tools_for_claude(_tools(), "gemini-3.8-flash-high")
    assert "anyOf" in repr(gemini)

    claude = _prepare_tools_for_claude(_tools(), "claude-opus-5-5-medium")
    assert "anyOf" not in repr(claude)
    assert claude[0]["function"]["parameters"]["properties"]["operations"]["items"] == {}
    assert claude[2] == _tools()[2]


def test_prepare_tools_does_not_mutate_caller_list():
    tools = _tools()
    before = copy.deepcopy(tools)
    claude = _prepare_tools_for_claude(tools, "claude-opus-5-5-high")
    assert tools == before
    assert claude is not tools


def test_prepare_tools_tolerates_empty_or_malformed_input():
    assert _prepare_tools_for_claude(None, "claude-opus-5-5-medium") is None
    assert _prepare_tools_for_claude([], "claude-opus-5-5-medium") == []
    junk = [{"type": "function", "function": {"name": "x"}}, "not-a-dict"]
    assert _prepare_tools_for_claude(junk, "claude-opus-5-5-medium") == junk
