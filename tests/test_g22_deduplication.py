"""B2-T: Tests for G22 semantic deduplication middleware."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from middleware import RequestContext
from middleware.g22_deduplication import G22Deduplication, _ngram_cosine, _cosine
from savings.models import SavingsRecord


def _make_ctx(messages, enabled=True, threshold=0.97):
    savings = SavingsRecord(
        request_id="req-g22",
        user_id="u1",
        timestamp=datetime.now(timezone.utc),
        model_requested="gpt-4o",
        routed_model="gpt-4o",
        baseline_tokens=200,
    )
    return RequestContext(
        request_id="req-g22",
        user_id="u1",
        original_messages=list(messages),
        messages=list(messages),
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config={
            "groups": {
                "g22_deduplication": {
                    "enabled": enabled,
                    "dedup_threshold": threshold,
                    "use_embeddings": False,  # use n-gram heuristic in tests (no ML deps)
                }
            }
        },
        savings=savings,
    )


class TestSimilarityHelpers:
    def test_ngram_cosine_identical_strings(self):
        assert _ngram_cosine("hello world", "hello world") == pytest.approx(1.0)

    def test_ngram_cosine_different_strings_below_one(self):
        sim = _ngram_cosine("hello world", "goodbye moon")
        assert 0.0 <= sim < 1.0

    def test_ngram_cosine_empty_string_returns_zero(self):
        assert _ngram_cosine("", "hello") == 0.0

    def test_cosine_identical_vectors(self):
        v = [1.0, 0.0, 1.0]
        assert _cosine(v, v) == pytest.approx(1.0)

    def test_cosine_orthogonal_vectors(self):
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


class TestG22Deduplication:
    @pytest.mark.asyncio
    async def test_disabled_returns_ctx_unchanged(self):
        msgs = [
            {"role": "user", "content": "What is Python?"},
            {"role": "user", "content": "What is Python?"},
        ]
        ctx = _make_ctx(msgs, enabled=False)
        g22 = G22Deduplication()
        result = await g22.process_request(ctx)
        assert len(result.messages) == 2

    @pytest.mark.asyncio
    async def test_exact_duplicate_user_turns_collapsed(self):
        content = "What is the capital of France?"
        msgs = [
            {"role": "user", "content": content},
            {"role": "user", "content": content},
            {"role": "user", "content": content},
        ]
        ctx = _make_ctx(msgs, enabled=True, threshold=0.97)
        g22 = G22Deduplication()
        result = await g22.process_request(ctx)
        assert result.messages == [{"role": "user", "content": content}]

    @pytest.mark.asyncio
    async def test_a_double_sent_question_keeps_the_latest_word_for_word(self):
        """The finding: the run used to become "[summarised: 2 similar turns]", so the model
        received no question and no order number while a saving was recorded."""
        msgs = [
            {"role": "system", "content": "You are a support assistant."},
            {"role": "user", "content": "Can you check order 1234?"},
            {"role": "user", "content": "Can you check order 1234??", "name": "alice"},
        ]
        ctx = _make_ctx(msgs, enabled=True, threshold=0.92)
        result = await G22Deduplication().process_request(ctx)
        assert result.messages == [msgs[0], msgs[2]]

    @pytest.mark.asyncio
    async def test_a_run_followed_by_a_different_turn_keeps_its_last_member(self):
        msgs = [
            {"role": "assistant", "content": "The deployment finished without errors."},
            {"role": "assistant", "content": "The deployment finished without errors!"},
            {"role": "assistant", "content": "Next, run the database migration script."},
        ]
        ctx = _make_ctx(msgs, enabled=True, threshold=0.9)
        result = await G22Deduplication().process_request(ctx)
        assert result.messages == [msgs[1], msgs[2]]

    @pytest.mark.asyncio
    async def test_the_saving_is_recorded_in_tokens(self):
        from savings.calculator import count_messages_tokens
        content = "What is the capital of France?"
        msgs = [{"role": "user", "content": content}] * 3
        ctx = _make_ctx(msgs, enabled=True, threshold=0.97)
        result = await G22Deduplication().process_request(ctx)
        steps = [s for s in result.savings.step_savings if s.group == "G22"]
        assert len(steps) == 1
        assert steps[0].tokens_before == count_messages_tokens(msgs, "gpt-4o")
        assert steps[0].tokens_after == count_messages_tokens(msgs[:1], "gpt-4o")

    @pytest.mark.asyncio
    async def test_nothing_dropped_records_no_step(self):
        msgs = [{"role": "user", "content": "Tell me about Python."},
                {"role": "user", "content": "Explain quantum mechanics in detail."}]
        ctx = _make_ctx(msgs, enabled=True, threshold=0.97)
        result = await G22Deduplication().process_request(ctx)
        assert result.messages == msgs
        assert not [s for s in result.savings.step_savings if s.group == "G22"]

    @pytest.mark.asyncio
    async def test_unique_turns_preserved(self):
        msgs = [
            {"role": "user", "content": "Tell me about Python programming language."},
            {"role": "user", "content": "Explain quantum mechanics in detail."},
        ]
        ctx = _make_ctx(msgs, enabled=True, threshold=0.97)
        g22 = G22Deduplication()
        result = await g22.process_request(ctx)
        # These are very different — should not be collapsed (ngram similarity will be low)
        assert len(result.messages) == 2

    @pytest.mark.asyncio
    async def test_single_message_no_op(self):
        msgs = [{"role": "user", "content": "Hello"}]
        ctx = _make_ctx(msgs, enabled=True)
        g22 = G22Deduplication()
        result = await g22.process_request(ctx)
        assert len(result.messages) == 1
        assert result.messages[0]["content"] == "Hello"

    @pytest.mark.asyncio
    async def test_system_messages_not_collapsed(self):
        msgs = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "system", "content": "You are a helpful assistant."},
        ]
        ctx = _make_ctx(msgs, enabled=True, threshold=0.97)
        g22 = G22Deduplication()
        result = await g22.process_request(ctx)
        assert len(result.messages) == 2

    @pytest.mark.asyncio
    async def test_savings_recorded(self):
        content = "What is the capital of France?"
        msgs = [
            {"role": "user", "content": content},
            {"role": "user", "content": content},
        ]
        ctx = _make_ctx(msgs, enabled=True, threshold=0.97)
        g22 = G22Deduplication()
        result = await g22.process_request(ctx)
        steps = result.savings.step_savings
        assert any(s.group == "G22" for s in steps)

    @pytest.mark.asyncio
    async def test_mixed_roles_not_cross_collapsed(self):
        """User turns and assistant turns with same text are NOT cross-collapsed."""
        content = "Python is great."
        msgs = [
            {"role": "user", "content": content},
            {"role": "assistant", "content": content},
        ]
        ctx = _make_ctx(msgs, enabled=True, threshold=0.97)
        g22 = G22Deduplication()
        result = await g22.process_request(ctx)
        # Different roles — should stay separate
        assert len(result.messages) == 2

    @pytest.mark.asyncio
    async def test_process_response_passthrough(self):
        ctx = _make_ctx([{"role": "user", "content": "Hi"}], enabled=True)
        g22 = G22Deduplication()
        response = {"choices": [{"message": {"content": "Hello"}}]}
        result = await g22.process_response(ctx, response)
        assert result == response

    @pytest.mark.asyncio
    async def test_below_threshold_not_collapsed(self):
        """Turns with low similarity (threshold very high at 0.999) are not collapsed."""
        msgs = [
            {"role": "user", "content": "What is Python?"},
            {"role": "user", "content": "What is Java?"},
        ]
        ctx = _make_ctx(msgs, enabled=True, threshold=0.999)
        g22 = G22Deduplication()
        result = await g22.process_request(ctx)
        assert len(result.messages) == 2


# The first request's use_embeddings latched for the process: a tenant overlay or a reload
# that changed it, or the model, was ignored until restart.
class TestTheModelFollowsEachRequestsConfig:

    def test_use_embeddings_is_read_per_request(self, monkeypatch):
        import middleware.g22_deduplication as g22_mod
        loads = []
        monkeypatch.setattr(g22_mod, "_get_model", lambda cfg: loads.append(cfg["embedding_model"]) or "model")
        g22 = G22Deduplication()
        assert g22._get_embedding_model({"use_embeddings": False, "embedding_model": "m1"}) is None
        assert g22._get_embedding_model({"use_embeddings": True, "embedding_model": "m1"}) == "model"
        assert g22._get_embedding_model({"use_embeddings": False, "embedding_model": "m1"}) is None

    def test_each_model_is_loaded_once_even_when_it_fails(self, monkeypatch):
        import middleware.g22_deduplication as g22_mod
        loads = []
        monkeypatch.setattr(g22_mod, "_get_model",
                            lambda cfg: loads.append(cfg["embedding_model"]) or None)
        g22 = G22Deduplication()
        for name in ("m1", "m1", "m2", "m1", "m2"):
            assert g22._get_embedding_model({"use_embeddings": True, "embedding_model": name}) is None
        assert loads == ["m1", "m2"]

    def test_an_injected_model_is_used_unless_embeddings_are_off(self):
        g22 = G22Deduplication(embedding_model="injected")
        assert g22._get_embedding_model({}) == "injected"
        assert g22._get_embedding_model({"use_embeddings": False}) is None


# ---------------------------------------------------------------------------
# T24 — Per-tenant threshold parameterisation
# ---------------------------------------------------------------------------

def _make_ctx_tenant(messages, tenant_id, global_threshold=0.97, tenant_thresholds=None):
    savings = SavingsRecord(
        request_id="req-g22-t24",
        user_id="u1",
        timestamp=datetime.now(timezone.utc),
        model_requested="gpt-4o",
        routed_model="gpt-4o",
        baseline_tokens=200,
    )
    ctx = RequestContext(
        request_id="req-g22-t24",
        user_id="u1",
        original_messages=list(messages),
        messages=list(messages),
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config={
            "groups": {
                "g22_deduplication": {
                    "enabled": True,
                    "dedup_threshold": global_threshold,
                    "use_embeddings": False,
                    "tenant_thresholds": tenant_thresholds or {},
                }
            }
        },
        savings=savings,
    )
    ctx.tenant_id = tenant_id
    return ctx


# Two identical user turns — any threshold <1.0 should collapse them
_DUPE_MSGS = [
    {"role": "user", "content": "What is the capital of France?"},
    {"role": "user", "content": "What is the capital of France?"},
]


@pytest.mark.asyncio
class TestPerTenantThreshold:
    async def test_tenant_threshold_overrides_global_collapses(self):
        """Tenant-specific low threshold causes collapse even when global would not."""
        # Global threshold set very high (won't collapse), tenant threshold low (will collapse)
        ctx = _make_ctx_tenant(
            _DUPE_MSGS,
            tenant_id="nova-med",
            global_threshold=0.999,
            tenant_thresholds={"nova-med": 0.50},
        )
        result = await G22Deduplication().process_request(ctx)
        assert result.messages == [_DUPE_MSGS[-1]]

    async def test_tenant_threshold_overrides_global_preserves(self):
        """Tenant-specific high threshold preserves turns that global would collapse."""
        # Two semantically similar but non-identical turns — n-gram sim is high but <1.0
        similar_msgs = [
            {"role": "user", "content": "What is the capital of France?"},
            {"role": "user", "content": "What is the main city of France?"},
        ]
        ctx = _make_ctx_tenant(
            similar_msgs,
            tenant_id="shop-bot",
            global_threshold=0.50,   # global would collapse (sim > 0.50)
            tenant_thresholds={"shop-bot": 0.999},  # tenant threshold > actual sim → no collapse
        )
        result = await G22Deduplication().process_request(ctx)
        assert len(result.messages) == 2

    async def test_unknown_tenant_falls_back_to_global(self):
        """Tenant not in tenant_thresholds uses global dedup_threshold."""
        ctx = _make_ctx_tenant(
            _DUPE_MSGS,
            tenant_id="unknown-tenant",
            global_threshold=0.50,  # low → will collapse identical turns
            tenant_thresholds={"nova-med": 0.999},
        )
        result = await G22Deduplication().process_request(ctx)
        assert len(result.messages) == 1

    async def test_empty_tenant_thresholds_uses_global(self):
        ctx = _make_ctx_tenant(
            _DUPE_MSGS,
            tenant_id="any-tenant",
            global_threshold=0.50,
            tenant_thresholds={},
        )
        result = await G22Deduplication().process_request(ctx)
        assert len(result.messages) == 1

    async def test_no_tenant_id_falls_back_to_global(self):
        """ctx.tenant_id = None → global threshold applies."""
        ctx = _make_ctx_tenant(
            _DUPE_MSGS,
            tenant_id=None,
            global_threshold=0.50,
            tenant_thresholds={"nova-med": 0.999},
        )
        result = await G22Deduplication().process_request(ctx)
        assert len(result.messages) == 1
