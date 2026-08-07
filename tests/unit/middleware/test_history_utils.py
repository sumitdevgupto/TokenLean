"""Unit tests for the shared history helpers (G10 + G26).

``safe_window_split`` and ``summarise_turns`` moved out of ``g10_memory`` so G26 could
reuse them without a cross-group import. These tests pin the two properties that make
that move safe: G10's private aliases still resolve to the shared functions (so every
existing patch target keeps working), and the summariser's new span/length caps default
to G10's original values.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from middleware.history_utils import safe_window_split, summarise_turns


class TestSafeWindowSplit:
    def test_plain_cut_when_no_tool_messages(self):
        turns = [{"role": "user"}, {"role": "assistant"}, {"role": "user"}, {"role": "assistant"}]
        assert safe_window_split(turns, 2) == 2

    def test_snaps_back_over_leading_tool_results(self):
        # A blind cut at index 2 would start the tail on a tool result whose declaring
        # assistant turn is in the summarised region → unmatched tool_call_id → provider 400.
        turns = [
            {"role": "user"},
            {"role": "assistant", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1"},
            {"role": "tool", "tool_call_id": "c2"},
            {"role": "assistant"},
        ]
        assert safe_window_split(turns, 3) == 1  # back to the assistant that declared them

    def test_returns_zero_when_no_clean_boundary(self):
        turns = [{"role": "tool", "tool_call_id": "c1"} for _ in range(4)]
        assert safe_window_split(turns, 2) == 0

    def test_keep_larger_than_history_keeps_everything(self):
        turns = [{"role": "user"}, {"role": "assistant"}]
        assert safe_window_split(turns, 99) == 0

    def test_negative_keep_is_clamped_not_an_indexerror(self):
        """A negative value used to push `start` past the end of the list and raise
        IndexError — which a caller's broad handler swallows, silently disabling
        compaction for good instead of failing loudly."""
        turns = [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]
        assert safe_window_split(turns, -5) == 0
        assert safe_window_split([], -1) == 0


class TestG10AliasesStillResolve:
    def test_g10_private_names_are_the_shared_functions(self):
        """G10 alias-imports both helpers under their old private names, so existing call
        sites and test patch targets (`middleware.g10_memory._summarise`) keep working."""
        from middleware import g10_memory
        assert g10_memory._safe_window_split is safe_window_split
        assert g10_memory._summarise is summarise_turns


@pytest.mark.asyncio
class TestSummariseTurnsCaps:
    @staticmethod
    def _wire(monkeypatch, seen):
        fake_adapter = MagicMock()
        fake_adapter.name = "openai"
        fake_adapter.requires_api_key.return_value = True
        fake_adapter.build_call.return_value = ("gpt-4o-mini", {"api_key": "sk"})

        async def _acompletion(**kwargs):
            seen.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="summary"))])

        import litellm
        monkeypatch.setattr("providers.get_adapter", lambda *a, **k: fake_adapter)
        monkeypatch.setattr("providers.get_provider_entry", lambda *a, **k: {})
        monkeypatch.setattr("auth.api_key_manager.get_llm_provider_key", lambda *a, **k: "sk")
        monkeypatch.setattr(litellm, "acompletion", _acompletion)

    async def test_defaults_reproduce_g10_behaviour(self, make_ctx, monkeypatch):
        seen = {}
        self._wire(monkeypatch, seen)
        turns = [{"role": "user", "content": f"turn {i}"} for i in range(40)]
        assert await summarise_turns(turns, "gpt-4o-mini", make_ctx([])) == "summary"
        assert seen["max_tokens"] == 150                       # G10's original cap
        prompt = seen["messages"][0]["content"]
        assert "turn 19" in prompt and "turn 20" not in prompt  # G10's original 20-turn window

    async def test_g26_can_widen_the_span_and_the_summary(self, make_ctx, monkeypatch):
        """G26's span is a whole conversation, not a sliding window — a 20-turn view would
        silently drop everything said in the MIDDLE of a long thread."""
        seen = {}
        self._wire(monkeypatch, seen)
        turns = [{"role": "user", "content": f"turn {i}"} for i in range(40)]
        await summarise_turns(turns, "gpt-4o-mini", make_ctx([]), max_turns=80, max_tokens=400)
        assert seen["max_tokens"] == 400
        assert "turn 39" in seen["messages"][0]["content"]

    async def test_empty_span_short_circuits(self, make_ctx):
        assert await summarise_turns([], "gpt-4o-mini", make_ctx([])) == ""

    async def test_oversized_span_is_trimmed_to_the_token_budget(self, make_ctx, monkeypatch):
        """A message COUNT cap cannot bound SIZE: 80 messages of a large-payload agentic
        thread can exceed the summariser model's own window, and a failed summariser means
        no compaction happens at all. The oldest turns go first."""
        seen = {}
        self._wire(monkeypatch, seen)
        turns = [{"role": "user", "content": f"turn {i} " + "x" * 4000} for i in range(20)]
        await summarise_turns(turns, "gpt-4o-mini", make_ctx([]),
                              max_turns=80, max_input_tokens=3000)
        prompt = seen["messages"][0]["content"]
        assert "turn 19" in prompt, "the newest turns must be kept"
        assert "turn 0 " not in prompt, "the oldest turns must be dropped to fit the budget"

    async def test_multimodal_parts_become_placeholders_not_base64(self, make_ctx, monkeypatch):
        """Serialising an image_url part would paste a megabyte of base64 into the
        summarisation prompt — burning the budget on data the summariser cannot read."""
        seen = {}
        self._wire(monkeypatch, seen)
        blob = "data:image/png;base64," + "A" * 5000
        turns = [{"role": "user", "content": [
            {"type": "text", "text": "describe this diagram"},
            {"type": "image_url", "image_url": {"url": blob}},
        ]}]
        await summarise_turns(turns, "gpt-4o-mini", make_ctx([]))
        prompt = seen["messages"][0]["content"]
        assert "describe this diagram" in prompt
        assert "AAAA" not in prompt
        assert "[image_url]" in prompt
