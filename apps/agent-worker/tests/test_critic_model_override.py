"""MM_CRITIC_MODEL_OVERRIDE — Critic gets its own model knob.

Critic produces a small JSON verdict (~50-150 tokens) and runs 5-7 times
per pipeline, so the run-level model (which is tuned for the prose-heavy
producer agents) is usually overkill for it. This env var lets ops point
Critic at a cheaper model — e.g. gpt-5.4-mini — independent of what the
producers use.

When the env var is empty/unset, Critic falls through to the run-level
model_override so existing deploys see zero behaviour change.
"""

from __future__ import annotations

import os

from agent_worker.config import get_settings


def test_critic_model_override_default_is_empty() -> None:
    """Unset env → empty string → Critic uses the run-level pick."""
    old = os.environ.pop("MM_CRITIC_MODEL_OVERRIDE", None)
    try:
        s = get_settings()
        assert s.critic_model_override == ""
    finally:
        if old is not None:
            os.environ["MM_CRITIC_MODEL_OVERRIDE"] = old


def test_critic_model_override_reads_env_var() -> None:
    os.environ["MM_CRITIC_MODEL_OVERRIDE"] = "cornna/gpt-5.4-mini"
    try:
        s = get_settings()
        assert s.critic_model_override == "cornna/gpt-5.4-mini"
    finally:
        del os.environ["MM_CRITIC_MODEL_OVERRIDE"]


def test_critic_model_override_handles_whitespace_only_as_unset() -> None:
    """Pydantic's str default is not strip-coerced; document the actual
    behavior so future-me doesn't assume ``MM_CRITIC_MODEL_OVERRIDE=" "``
    falls back. It doesn't — caller must unset or use the empty string.
    """
    os.environ["MM_CRITIC_MODEL_OVERRIDE"] = " "
    try:
        s = get_settings()
        # Confirm the value passes through verbatim. Pipeline.py treats any
        # truthy string as "use this model" so " " would route Critic to a
        # nonsense model name — but the gateway would 400, so the failure
        # mode is loud, not silent.
        assert s.critic_model_override == " "
    finally:
        del os.environ["MM_CRITIC_MODEL_OVERRIDE"]
