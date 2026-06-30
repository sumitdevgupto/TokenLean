"""
Tests for G20 — Prompt Optimization Pipeline.

Validates:
  - Built-in optimizer reduces token count
  - Quality threshold gating
  - Dry-run mode
  - Config-driven optimizer selection
  - Filler phrase removal
  - Redis push (mocked)
"""
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "proxy"))

from run_prompt_optimization import (
    optimize_builtin,
    estimate_tokens_simple,
    run_optimization,
    load_eval_dataset,
)


# ─── Enable/disable toggle ───────────────────────────────────────────────────

def test_disabled_skips():
    """G20 is a no-op and pushes nothing when enabled=false."""
    config = {
        "groups": {
            "G20_prompt_optimization": {
                "enabled": False,
                "optimizer": "builtin",
            },
            "G2_template_registry": {
                "budgets": {
                    "test-template": {
                        "prompt": "It is important to make sure to in order to help the user.",
                    }
                }
            },
        }
    }
    mock_push = MagicMock(return_value=True)
    with patch("run_prompt_optimization.get_current_template", return_value=None), \
         patch("run_prompt_optimization.push_optimized_template", mock_push):
        result = run_optimization(config, "test-template", eval_data=[], dry_run=False)

    assert result["status"] == "disabled"
    mock_push.assert_not_called()


def test_enabled_defaults_true_when_absent():
    """When enabled key is absent, optimization still runs (backward compatible)."""
    config = {
        "groups": {
            "G20_prompt_optimization": {
                "optimizer": "builtin",
                "quality_threshold": 0.95,
                "max_prompt_tokens": 4000,
            },
            "G2_template_registry": {
                "budgets": {
                    "test-template": {
                        "prompt": "It is important to make sure to in order to help the user resolve issues.",
                    }
                }
            },
        }
    }
    with patch("run_prompt_optimization.get_current_template", return_value=None):
        result = run_optimization(config, "test-template", eval_data=[], dry_run=True)
    assert result["status"] == "optimized"


# ─── Built-in optimizer ─────────────────────────────────────────────────────

def test_builtin_removes_filler_phrases():
    prompt = (
        "It is important to make sure to respond politely. "
        "In order to help the user, please note that you should be thorough. "
        "Due to the fact that users expect quality, ensure that you are helpful."
    )
    result, score = optimize_builtin(prompt, quality_threshold=0.95, max_prompt_tokens=4000)
    assert result is not None
    assert len(result) < len(prompt)
    assert score == 1.0
    # Filler phrases should be replaced
    assert "it is important to" not in result.lower()
    assert "in order to" not in result.lower()
    assert "due to the fact that" not in result.lower()


def test_builtin_collapses_whitespace():
    prompt = "Hello    world.\n\n\n\nThis   is   a   test.\n\n\n"
    result, score = optimize_builtin(prompt, quality_threshold=0.95, max_prompt_tokens=4000)
    assert result is not None
    assert "    " not in result
    assert "\n\n\n" not in result


def test_builtin_no_improvement_returns_none():
    prompt = "Be concise."
    result, score = optimize_builtin(prompt, quality_threshold=0.95, max_prompt_tokens=4000)
    # Very short prompt with no filler — should return None (no improvement)
    # or a result if whitespace was trimmed
    if result is not None:
        assert estimate_tokens_simple(result) <= estimate_tokens_simple(prompt)


def test_estimate_tokens_simple():
    assert estimate_tokens_simple("") == 0 or estimate_tokens_simple("") >= 0
    assert estimate_tokens_simple("hello world") > 0
    assert estimate_tokens_simple("a" * 400) == 100  # 400 chars / 4


# ─── Config-driven optimization ──────────────────────────────────────────────

def test_run_optimization_builtin_dry_run():
    """Built-in optimizer in dry-run mode."""
    config = {
        "groups": {
            "G20_prompt_optimization": {
                "enabled": True,
                "optimizer": "builtin",
                "quality_threshold": 0.95,
                "max_prompt_tokens": 4000,
                "model": "gpt-4o-mini",
            },
            "G2_template_registry": {
                "budgets": {
                    "test-template": {
                        "prompt": "It is important to make sure to respond to all customer queries. In order to help them, please note that you should be thorough and professional in your responses.",
                        "total_input_max": 800,
                    }
                }
            },
        }
    }

    with patch("run_prompt_optimization.get_current_template", return_value=None):
        result = run_optimization(config, "test-template", eval_data=[], dry_run=True)

    assert result["status"] == "optimized"
    assert result["dry_run"] is True
    assert result["reduction_pct"] > 0
    assert result["optimized_tokens"] < result["original_tokens"]


def test_run_optimization_no_prompt():
    """Error when template has no prompt."""
    config = {"groups": {"G20_prompt_optimization": {"optimizer": "builtin"}, "G2_template_registry": {"budgets": {}}}}
    with patch("run_prompt_optimization.get_current_template", return_value=None):
        result = run_optimization(config, "nonexistent", eval_data=[], dry_run=True)
    assert result["status"] == "error"


def test_run_optimization_push_to_redis():
    """Non-dry-run should push to Redis."""
    config = {
        "groups": {
            "G20_prompt_optimization": {
                "optimizer": "builtin",
                "quality_threshold": 0.95,
                "max_prompt_tokens": 4000,
            },
            "G2_template_registry": {
                "budgets": {
                    "test-tpl": {
                        "prompt": "Please note that it is important to make sure to ensure that you in order to help.",
                    }
                }
            },
        }
    }

    mock_push = MagicMock(return_value=True)
    with patch("run_prompt_optimization.get_current_template", return_value=None), \
         patch("run_prompt_optimization.push_optimized_template", mock_push):
        result = run_optimization(config, "test-tpl", eval_data=[], dry_run=False)

    assert result["status"] == "optimized"
    assert result.get("pushed") is True
    mock_push.assert_called_once()


# ─── Eval dataset loading ────────────────────────────────────────────────────

def test_load_eval_dataset(tmp_path):
    """JSONL loading works."""
    data = [{"input": "test1", "expected": "answer1"}, {"input": "test2", "expected": "answer2"}]
    path = tmp_path / "eval.jsonl"
    with open(path, "w") as f:
        for item in data:
            f.write(json.dumps(item) + "\n")

    loaded = load_eval_dataset(str(path))
    assert len(loaded) == 2
    assert loaded[0]["input"] == "test1"


# ─── Optimizer fallback chain ────────────────────────────────────────────────

def test_opik_fallback_to_builtin():
    """When Opik and DSPy are unavailable, falls back to built-in."""
    config = {
        "groups": {
            "G20_prompt_optimization": {
                "optimizer": "MIPROv2",
                "quality_threshold": 0.95,
                "max_prompt_tokens": 4000,
                "model": "gpt-4o-mini",
            },
            "G2_template_registry": {
                "budgets": {
                    "test-fallback": {
                        "prompt": "It is important to ensure that you make sure to help users in order to resolve their issues.",
                    }
                }
            },
        }
    }

    with patch("run_prompt_optimization.get_current_template", return_value=None), \
         patch("run_prompt_optimization._opik_available", False), \
         patch("run_prompt_optimization._dspy_available", False):
        result = run_optimization(config, "test-fallback", eval_data=[], dry_run=True)

    assert result["status"] == "optimized"
    assert result["reduction_pct"] > 0


# ─── Quality gate ────────────────────────────────────────────────────────────

def test_quality_gate_rejects_unverified_dspy_prompt():
    """DSPy returns quality_score=None (unverified) — must NOT be pushed."""
    config = {
        "groups": {
            "G20_prompt_optimization": {
                "enabled": True,
                "optimizer": "dspy",
                "quality_threshold": 0.95,
                "max_prompt_tokens": 4000,
            },
            "G2_template_registry": {
                "budgets": {
                    "test-dspy": {"prompt": "Please help the user in order to resolve their issue."}
                }
            },
        }
    }
    mock_push = MagicMock(return_value=True)
    # optimize_with_dspy returns (prompt, None) → unverified
    with patch("run_prompt_optimization.get_current_template", return_value=None), \
         patch("run_prompt_optimization.optimize_with_dspy", return_value=("shorter prompt", None)), \
         patch("run_prompt_optimization.push_optimized_template", mock_push):
        result = run_optimization(config, "test-dspy", eval_data=[], dry_run=False)

    assert result["status"] == "quality_gate_failed"
    assert result["quality_score"] is None
    mock_push.assert_not_called()


def test_quality_gate_rejects_below_threshold():
    """A verified-but-low quality score must NOT be pushed."""
    config = {
        "groups": {
            "G20_prompt_optimization": {
                "enabled": True,
                "optimizer": "dspy",
                "quality_threshold": 0.95,
                "max_prompt_tokens": 4000,
            },
            "G2_template_registry": {
                "budgets": {
                    "test-low": {"prompt": "Please help the user in order to resolve their issue."}
                }
            },
        }
    }
    mock_push = MagicMock(return_value=True)
    with patch("run_prompt_optimization.get_current_template", return_value=None), \
         patch("run_prompt_optimization.optimize_with_dspy", return_value=("shorter prompt", 0.80)), \
         patch("run_prompt_optimization.push_optimized_template", mock_push):
        result = run_optimization(config, "test-low", eval_data=[], dry_run=False)

    assert result["status"] == "quality_gate_failed"
    assert result["quality_score"] == 0.80
    mock_push.assert_not_called()


def test_quality_gate_accepts_at_threshold():
    """A score >= threshold passes the gate and is pushed."""
    config = {
        "groups": {
            "G20_prompt_optimization": {
                "enabled": True,
                "optimizer": "dspy",
                "quality_threshold": 0.95,
                "max_prompt_tokens": 4000,
            },
            "G2_template_registry": {
                "budgets": {
                    "test-pass": {"prompt": "Please help the user in order to resolve their issue."}
                }
            },
        }
    }
    mock_push = MagicMock(return_value=True)
    with patch("run_prompt_optimization.get_current_template", return_value=None), \
         patch("run_prompt_optimization.optimize_with_dspy", return_value=("shorter prompt", 0.97)), \
         patch("run_prompt_optimization.push_optimized_template", mock_push):
        result = run_optimization(config, "test-pass", eval_data=[], dry_run=False)

    assert result["status"] == "optimized"
    assert result.get("pushed") is True
    mock_push.assert_called_once()
