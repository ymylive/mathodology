"""Paper-editor tool catalogue.

Exposes the six tools the PaperEditorAgent dispatches to plus the shared
`ToolContext` / `ToolResult` / `Tool` protocol. See `tools.py` for details.
"""

from agent_worker.editor_tools.tools import (
    EditConstantTool,
    EditSectionTool,
    ReadPaperTool,
    RecompilePdfTool,
    RegenerateFigureTool,
    RunCellTool,
    Tool,
    ToolContext,
    ToolResult,
    build_tool_registry,
)

__all__ = [
    "EditConstantTool",
    "EditSectionTool",
    "ReadPaperTool",
    "RecompilePdfTool",
    "RegenerateFigureTool",
    "RunCellTool",
    "Tool",
    "ToolContext",
    "ToolResult",
    "build_tool_registry",
]
