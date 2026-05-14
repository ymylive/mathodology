"""Auto-export PDF hook: disabled / success / failure / non-PDF payload."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from agent_worker.pipeline import _auto_export_pdf


def _emitter() -> MagicMock:
    m = MagicMock()
    m.emit = AsyncMock()
    return m


def _settings(enabled: bool = True, timeout: float = 600.0) -> SimpleNamespace:
    return SimpleNamespace(auto_export_pdf=enabled, auto_export_timeout_s=timeout)


@pytest.mark.asyncio
async def test_auto_export_disabled_returns_none(tmp_path: Path) -> None:
    gateway = MagicMock()
    gateway.export_paper = AsyncMock()
    result = await _auto_export_pdf(
        gateway=gateway,
        run_id=uuid4(),
        run_dir=tmp_path,
        settings=_settings(enabled=False),
        emitter=_emitter(),
    )
    assert result is None
    gateway.export_paper.assert_not_called()


@pytest.mark.asyncio
async def test_auto_export_success_writes_pdf(tmp_path: Path) -> None:
    gateway = MagicMock()
    gateway.export_paper = AsyncMock(return_value=b"%PDF-1.7\n...bytes...")
    result = await _auto_export_pdf(
        gateway=gateway,
        run_id=uuid4(),
        run_dir=tmp_path,
        settings=_settings(),
        emitter=_emitter(),
    )
    assert result == tmp_path / "paper.pdf"
    assert (tmp_path / "paper.pdf").read_bytes().startswith(b"%PDF")


@pytest.mark.asyncio
async def test_auto_export_non_pdf_payload_returns_none(tmp_path: Path) -> None:
    gateway = MagicMock()
    gateway.export_paper = AsyncMock(return_value=b"some error html, no PDF magic")
    emitter = _emitter()
    result = await _auto_export_pdf(
        gateway=gateway,
        run_id=uuid4(),
        run_dir=tmp_path,
        settings=_settings(),
        emitter=emitter,
    )
    assert result is None
    assert not (tmp_path / "paper.pdf").exists()
    # warning log was emitted
    warn_kinds = [
        c.args[0]
        for c in emitter.emit.await_args_list
        if c.args and c.args[0] == "log"
    ]
    assert warn_kinds


@pytest.mark.asyncio
async def test_auto_export_http_failure_is_non_fatal(tmp_path: Path) -> None:
    import httpx

    gateway = MagicMock()
    gateway.export_paper = AsyncMock(
        side_effect=httpx.ConnectError("tectonic missing")
    )
    result = await _auto_export_pdf(
        gateway=gateway,
        run_id=uuid4(),
        run_dir=tmp_path,
        settings=_settings(),
        emitter=_emitter(),
    )
    assert result is None
    assert not (tmp_path / "paper.pdf").exists()


@pytest.mark.asyncio
async def test_auto_export_passes_timeout_to_gateway(tmp_path: Path) -> None:
    gateway = MagicMock()
    gateway.export_paper = AsyncMock(return_value=b"%PDF-1.7 ok")
    await _auto_export_pdf(
        gateway=gateway,
        run_id=uuid4(),
        run_dir=tmp_path,
        settings=_settings(timeout=42.0),
        emitter=_emitter(),
    )
    call_kwargs: dict[str, Any] = gateway.export_paper.await_args.kwargs
    assert call_kwargs["compile_timeout_s"] == 42.0
    assert call_kwargs["format"] == "pdf"
