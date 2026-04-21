"""Prompt registry for agent-worker.

Prompts live on disk as TOML files under `prompts/<agent>/<version>.toml`.
Each file declares system text, a user-template (with `{{ var }}` placeholders),
model preferences, token budgets, and a response schema descriptor.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from mm_contracts import ReasoningEffort
from pydantic import BaseModel, ConfigDict

_PROMPTS_DIR = Path(__file__).parent

# Simple {{ var }} substitution. Whitespace around the name is tolerated; the
# captured group is the variable name.
_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class PromptSpec(BaseModel):
    """A loaded prompt spec — everything an agent needs to build an LLM call."""

    model_config = ConfigDict(extra="forbid")

    version: str
    agent: str
    model_preference: list[str]
    token_budget_in: int
    token_budget_out: int
    temperature: float
    system: dict[str, Any]  # {"text": str}
    user_template: dict[str, Any]  # {"text": str}
    response_schema: dict[str, Any]  # {"kind": str, "name": str}
    # Optional per-agent override for reasoning effort. `None` means inherit
    # the run-level setting from `ProblemInput.reasoning_effort`.
    reasoning_effort: ReasoningEffort | None = None

    def render_user(self, **vars: Any) -> str:
        """Render the user template. Missing vars → empty string (never raises)."""
        template: str = self.user_template.get("text", "")

        def _sub(match: re.Match[str]) -> str:
            value = vars.get(match.group(1), "")
            return "" if value is None else str(value)

        return _VAR_RE.sub(_sub, template)


def load_prompt(agent_name: str, version: str = "v1") -> PromptSpec:
    """Load `prompts/<agent_name>/<version>.toml` and validate as PromptSpec."""
    path = _PROMPTS_DIR / agent_name / f"{version}.toml"
    if not path.is_file():
        raise FileNotFoundError(f"prompt not found: {path}")
    with path.open("rb") as f:
        data = tomllib.load(f)
    return PromptSpec.model_validate(data)


__all__ = ["PromptSpec", "load_prompt"]
