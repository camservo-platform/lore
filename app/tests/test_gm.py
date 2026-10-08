from lore.gm import echo_content, style_message
from lore.web.app import _complete_history


def test_echo_content_keeps_everything_without_fallback():
    blocks = [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "Hi"},
              {"type": "tool_use", "id": "t1", "name": "roll_dice", "input": {}}]
    assert echo_content(blocks) == blocks


def test_echo_content_drops_model_internal_blocks_before_last_fallback():
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "a"},
        {"type": "text", "text": "partial"},
        {"type": "tool_use", "id": "t0", "name": "x", "input": {}},
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        {"type": "thinking", "thinking": "", "signature": "b"},
        {"type": "tool_use", "id": "t1", "name": "roll_dice", "input": {}},
    ]
    assert [b["type"] for b in echo_content(blocks)] == ["text", "fallback", "thinking", "tool_use"]


def test_complete_history_drops_unanswered_tool_call_only():
    user = {"role": "user", "content": "alice: hi"}
    answered = {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]}
    dangling = {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]}
    assert _complete_history([user, answered]) == [user, answered]
    assert _complete_history([user, dangling]) == [user]
    assert _complete_history([]) == []


def test_style_messages():
    assert style_message("speech")["role"] == "system"
    assert "markdown" in style_message("speech")["content"]
    assert "text mode" in style_message("text")["content"]
