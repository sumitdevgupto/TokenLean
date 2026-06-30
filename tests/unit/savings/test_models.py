"""Unit tests for savings/models.py — SavingsRecord and StepSaving."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from datetime import datetime, timezone
import pytest
from savings.models import SavingsRecord, StepSaving


def _record(baseline=400, final=200, model="gpt-4o") -> SavingsRecord:
    rec = SavingsRecord(
        request_id="req-1",
        user_id="user-1",
        timestamp=datetime.now(timezone.utc),
        model_requested=model,
        routed_model=model,
        baseline_tokens=baseline,
    )
    rec.final_tokens_sent = final
    return rec


# ─── StepSaving ────────────────────────────────────────────────────────────────

class TestStepSaving:
    def test_absolute_saving_positive(self):
        s = StepSaving("G01", "compression", 200, 120)
        assert s.absolute_saving == 80

    def test_absolute_saving_never_negative(self):
        s = StepSaving("G01", "compression", 100, 150)
        assert s.absolute_saving == 0

    def test_absolute_saving_zero_when_equal(self):
        s = StepSaving("G06", "routing", 100, 100)
        assert s.absolute_saving == 0


# ─── SavingsRecord ────────────────────────────────────────────────────────────

class TestSavingsRecord:
    def test_total_absolute_saving(self):
        rec = _record(baseline=400, final=200)
        assert rec.total_absolute_saving == 200

    def test_total_absolute_saving_never_negative(self):
        rec = _record(baseline=100, final=150)
        assert rec.total_absolute_saving == 0

    def test_total_pct_saving(self):
        rec = _record(baseline=400, final=200)
        assert rec.total_pct_saving == 50.0

    def test_total_pct_saving_zero_baseline(self):
        rec = _record(baseline=0, final=0)
        assert rec.total_pct_saving == 0.0

    def test_total_pct_saving_no_savings(self):
        rec = _record(baseline=100, final=100)
        assert rec.total_pct_saving == 0.0

    def test_cost_saving_usd(self):
        rec = _record()
        rec.cost_baseline_usd = 0.010
        rec.cost_actual_usd = 0.003
        assert abs(rec.cost_saving_usd - 0.007) < 1e-7

    def test_cost_saving_usd_not_negative(self):
        rec = _record()
        rec.cost_baseline_usd = 0.001
        rec.cost_actual_usd = 0.002
        # This may be negative (cost went up) but the property doesn't clamp it
        # — verify it's just the arithmetic difference
        assert rec.cost_saving_usd == round(0.001 - 0.002, 6)

    def test_add_step_appends(self):
        rec = _record()
        rec.add_step("G01", "compression", 200, 120)
        assert len(rec.step_savings) == 1
        assert rec.step_savings[0].group == "G01"

    def test_add_multiple_steps(self):
        rec = _record()
        rec.add_step("G01", "desc1", 200, 120)
        rec.add_step("G06", "desc2", 200, 200)
        assert len(rec.step_savings) == 2

    # ─── to_langfuse_metadata ─────────────────────────────────────────────────

    def test_metadata_required_keys(self):
        rec = _record(baseline=400, final=200)
        rec.add_step("G01", "compression", 400, 200)
        meta = rec.to_langfuse_metadata()
        required = [
            "request_id", "user_id", "timestamp", "model_requested",
            "routed_model", "baseline_tokens", "final_tokens_sent",
            "response_tokens", "total_abs_saving", "total_pct_saving",
            "cache_hit", "cache_level", "bypassed",
            "cost_baseline_usd", "cost_actual_usd", "cost_saving_usd",
            "step_savings",
        ]
        for key in required:
            assert key in meta, f"Missing key: {key}"

    def test_metadata_step_savings_structure(self):
        rec = _record(baseline=400, final=200)
        rec.add_step("G01", "compressed", 400, 200)
        meta = rec.to_langfuse_metadata()
        assert "G01" in meta["step_savings"]
        step = meta["step_savings"]["G01"]
        for key in ("description", "tokens_before", "tokens_after", "abs_saving", "pct_saving_vs_baseline"):
            assert key in step, f"Missing step key: {key}"

    def test_metadata_pct_saving_vs_baseline_correct(self):
        rec = _record(baseline=400, final=200)
        rec.add_step("G01", "desc", 400, 200)
        meta = rec.to_langfuse_metadata()
        # G01 saved 200 out of 400 baseline = 50%
        assert meta["step_savings"]["G01"]["pct_saving_vs_baseline"] == 50.0

    def test_metadata_pct_saving_zero_baseline(self):
        rec = _record(baseline=0, final=0)
        rec.add_step("G01", "desc", 0, 0)
        meta = rec.to_langfuse_metadata()
        assert meta["step_savings"]["G01"]["pct_saving_vs_baseline"] == 0.0

    def test_metadata_total_values_match_properties(self):
        rec = _record(baseline=300, final=150)
        meta = rec.to_langfuse_metadata()
        assert meta["total_abs_saving"] == rec.total_absolute_saving
        assert meta["total_pct_saving"] == rec.total_pct_saving
        assert meta["cost_saving_usd"] == rec.cost_saving_usd


# ─── B1: three-number model (x/y/z) + two distinct savings ──────────────────────

class TestSavingsRecordB1:
    def _rec(self, baseline=400, proxy=300, provider=200):
        rec = SavingsRecord(
            request_id="r", user_id="u",
            timestamp=datetime.now(timezone.utc),
            model_requested="gpt-4o", routed_model="gpt-4o",
            baseline_tokens=baseline,
        )
        rec.proxy_optimised_tokens = proxy        # y
        rec.provider_prompt_tokens = provider     # z
        rec.final_tokens_sent = provider          # G18 sets final == z
        return rec

    def test_proxy_savings_estimate(self):
        rec = self._rec(baseline=400, proxy=300)
        assert rec.proxy_tokens_saved == 100        # x − y
        assert rec.proxy_pct_saving == 25.0

    def test_actual_savings_uses_provider(self):
        rec = self._rec(baseline=400, provider=200)
        assert rec.actual_tokens_saved == 200       # x − z
        assert rec.actual_pct_saving == 50.0
        # actual_* mirrors the legacy total_* pair
        assert rec.actual_tokens_saved == rec.total_absolute_saving
        assert rec.actual_pct_saving == rec.total_pct_saving

    def test_proxy_pct_zero_baseline(self):
        rec = self._rec(baseline=0, proxy=0)
        assert rec.proxy_pct_saving == 0.0

    def test_proxy_saving_never_negative(self):
        rec = self._rec(baseline=100, proxy=150)
        assert rec.proxy_tokens_saved == 0

    def test_metadata_exposes_xyz_and_two_savings(self):
        rec = self._rec(baseline=400, proxy=300, provider=200)
        meta = rec.to_langfuse_metadata()
        assert meta["tokens_user_sent"] == 400          # x
        assert meta["tokens_after_proxy"] == 300        # y
        assert meta["tokens_provider_billed"] == 200    # z
        assert meta["proxy_savings_abs"] == 100
        assert meta["proxy_savings_pct"] == 25.0
        assert meta["actual_savings_abs"] == 200
        assert meta["actual_savings_pct"] == 50.0

    def test_provider_billed_may_be_none_before_g18(self):
        rec = SavingsRecord(
            request_id="r", user_id="u",
            timestamp=datetime.now(timezone.utc),
            model_requested="gpt-4o", routed_model="gpt-4o",
            baseline_tokens=400,
        )
        rec.proxy_optimised_tokens = 300
        meta = rec.to_langfuse_metadata()
        assert meta["tokens_provider_billed"] is None
        # proxy savings still computable from x/y alone
        assert meta["proxy_savings_pct"] == 25.0
