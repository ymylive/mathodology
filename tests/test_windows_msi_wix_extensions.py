import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _command_block(script: str, command: str) -> str:
    match = re.search(rf"{re.escape(command)}.*?\|\| exit /b 1", script, re.S)
    assert match is not None, f"{command} invocation not found"
    return match.group(0)


def test_path_entry_uses_wix_environment_element() -> None:
    wxs = (ROOT / "installer/windows/Mathodology.wxs").read_text(encoding="utf-8")

    assert "<Environment Id=\"MathodologyPath\"" in wxs
    assert "<util:EnvironmentVariable" not in wxs


def test_wix_util_extension_is_loaded_for_any_remaining_util_namespace_use() -> None:
    wxs = (ROOT / "installer/windows/Mathodology.wxs").read_text(encoding="utf-8")
    build_script = (ROOT / "installer/windows/build-msi.cmd").read_text(encoding="utf-8")

    if "xmlns:util=" not in wxs and "<util:" not in wxs:
        return

    assert "-ext WixUtilExtension" in _command_block(build_script, "candle.exe")
    assert "-ext WixUtilExtension" in _command_block(build_script, "light.exe")
