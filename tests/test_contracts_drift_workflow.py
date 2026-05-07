from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_contracts_drift_uses_installed_datamodel_codegen_tool() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "uv tool install 'datamodel-code-generator[http]'" in workflow
    assert "uv tool run datamodel-codegen" not in workflow
    assert "datamodel-codegen \\" in workflow
