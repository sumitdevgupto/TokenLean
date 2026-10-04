"""The Anthropic ingress keeps the caller's prompt-cache markers.

An Anthropic SDK client (Claude Code, for one) marks its system prompt, tools and recent
turns with `cache_control`, so Anthropic bills the repeated prefix at a fraction of the
input rate. Translation used to flatten system and text blocks and rebuild tools, which
dropped every marker: nothing was cached and every turn paid full price. Each marker now
travels in the OpenAI shape litellm turns back into an Anthropic breakpoint, at the same
position. A request with no markers translates exactly as before."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from protocols import AnthropicProtocol

MARK = {"type": "ephemeral"}
MARK_1H = {"type": "ephemeral", "ttl": "1h"}
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}


def _parse(**body):
    body = {"model": "claude-sonnet-4-5", "max_tokens": 64, **body}
    body.setdefault("messages", [{"role": "user", "content": "hi"}])
    return AnthropicProtocol().parse_request(body)


def test_a_marked_system_block_keeps_its_marker_in_place():
    messages, _, _ = _parse(system=[
        {"type": "text", "text": "Rules A."},
        {"type": "text", "text": "Rules B.", "cache_control": MARK_1H},
        {"type": "text", "text": "Today is Tuesday."},
    ])
    assert messages[0] == {"role": "system", "content": [
        {"type": "text", "text": "Rules A."},
        {"type": "text", "text": "Rules B.", "cache_control": MARK_1H},
        {"type": "text", "text": "Today is Tuesday."},
    ]}


def test_an_unmarked_system_prompt_is_still_one_string():
    messages, _, _ = _parse(system=[{"type": "text", "text": "Rules A."},
                                    {"type": "text", "text": "Rules B."}])
    assert messages[0] == {"role": "system", "content": "Rules A.Rules B."}


def test_a_marked_text_block_keeps_its_marker():
    messages, _, _ = _parse(messages=[{"role": "user", "content": [
        {"type": "text", "text": "Read this."},
        {"type": "text", "text": "Then fix it.", "cache_control": MARK}]}])
    assert messages == [{"role": "user", "content": [
        {"type": "text", "text": "Read this."},
        {"type": "text", "text": "Then fix it.", "cache_control": MARK}]}]


def test_unmarked_text_blocks_still_collapse_to_one_string():
    messages, _, _ = _parse(messages=[{"role": "user", "content": [
        {"type": "text", "text": "Read this. "}, {"type": "text", "text": "Then fix it."}]}])
    assert messages == [{"role": "user", "content": "Read this. Then fix it."}]


def test_a_marked_image_keeps_its_marker():
    messages, _, _ = _parse(messages=[{"role": "user", "content": [
        {**IMAGE, "cache_control": MARK}, {"type": "text", "text": "What is this?"}]}])
    assert messages[0]["content"][0] == {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"},
        "cache_control": MARK}
    assert "cache_control" not in messages[0]["content"][1]


def test_a_marked_tool_use_keeps_its_marker_on_the_tool_call():
    messages, _, _ = _parse(messages=[
        {"role": "user", "content": "Build it."},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Reading the log."},
            {"type": "tool_use", "id": "t1", "name": "read_log", "input": {},
             "cache_control": MARK}]},
    ])
    assert messages[1] == {"role": "assistant", "content": "Reading the log.", "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "read_log", "arguments": "{}"},
         "cache_control": MARK}]}


def _tool_turns(result_block, follow_up=None):
    last = {"role": "user", "content": [result_block] + ([follow_up] if follow_up else [])}
    return [
        {"role": "user", "content": "Build it."},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "read_log", "input": {}}]},
        last,
    ]


def test_a_marked_tool_result_marks_its_tool_message():
    messages, _, _ = _parse(messages=_tool_turns(
        {"type": "tool_result", "tool_use_id": "t1", "content": "log text",
         "cache_control": MARK}))
    assert messages[2] == {"role": "tool", "tool_call_id": "t1", "content": "log text",
                           "cache_control": MARK}


def test_a_marker_inside_a_tool_result_marks_its_tool_message():
    messages, _, _ = _parse(messages=_tool_turns(
        {"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "text", "text": "log "}, {"type": "text", "text": "text",
                                               "cache_control": MARK}]}))
    assert messages[2] == {"role": "tool", "tool_call_id": "t1", "content": "log text",
                           "cache_control": MARK}


def test_a_turn_with_a_tool_result_and_marked_text_keeps_both_apart():
    messages, _, _ = _parse(messages=_tool_turns(
        {"type": "tool_result", "tool_use_id": "t1", "content": "log text"},
        {"type": "text", "text": "Go on.", "cache_control": MARK}))
    assert messages[2] == {"role": "tool", "tool_call_id": "t1", "content": "log text"}
    assert messages[3] == {"role": "user", "content": [
        {"type": "text", "text": "Go on.", "cache_control": MARK}]}


def test_an_orphaned_tool_result_keeps_its_marker_when_it_degrades_to_text():
    messages, _, _ = _parse(messages=[{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "nope", "content": "out", "cache_control": MARK}]}])
    assert messages == [{"role": "user", "content": [
        {"type": "text", "text": "[tool_result nope]: out", "cache_control": MARK}]}]


def test_a_malformed_tool_use_keeps_its_marker_when_it_degrades_to_text():
    messages, _, _ = _parse(messages=[{"role": "assistant", "content": [
        {"type": "tool_use", "name": "read_log", "input": {}, "cache_control": MARK}]}])
    assert messages == [{"role": "assistant", "content": [
        {"type": "text", "text": "[tool_use read_log]: {}", "cache_control": MARK}]}]


def test_a_marked_tool_keeps_its_marker():
    _, _, params = _parse(tools=[
        {"name": "read_log", "description": "Read.", "input_schema": {"type": "object"}},
        {"name": "write", "description": "Write.", "input_schema": {"type": "object"},
         "cache_control": MARK}])
    assert "cache_control" not in params["tools"][0]
    assert params["tools"][1] == {"type": "function", "function": {
        "name": "write", "description": "Write.", "parameters": {"type": "object"}},
        "cache_control": MARK}


def test_the_request_level_marker_is_kept():
    _, _, params = _parse(cache_control=MARK)
    assert params.get("cache_control") == MARK


def test_a_null_marker_is_not_a_marker():
    messages, _, params = _parse(
        system=[{"type": "text", "text": "Rules.", "cache_control": None}],
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "hi", "cache_control": None}]}],
        cache_control=None)
    assert messages == [{"role": "system", "content": "Rules."},
                        {"role": "user", "content": "hi"}]
    assert "cache_control" not in params


def test_an_unmarked_request_translates_exactly_as_before():
    messages, model, params = _parse(
        system=[{"type": "text", "text": "Rules."}],
        tools=[{"name": "read_log", "description": "Read.", "input_schema": {"type": "object"}}],
        messages=_tool_turns({"type": "tool_result", "tool_use_id": "t1", "content": "log"},
                             {"type": "text", "text": "Go on."}))
    assert model == "claude-sonnet-4-5"
    assert messages == [
        {"role": "system", "content": "Rules."},
        {"role": "user", "content": "Build it."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "read_log", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "log"},
        {"role": "user", "content": "Go on."},
    ]
    assert params == {"max_tokens": 64, "tools": [{"type": "function", "function": {
        "name": "read_log", "description": "Read.", "parameters": {"type": "object"}}}]}
