"""Small helper functions the Coder's Jupyter kernel gets pre-loaded with.

Kept deliberately tiny (<80 lines of actual code) so the whole thing can be
inlined into `_MPL_BOOTSTRAP_SRC` and injected into the user namespace at
kernel startup — no sys.path hackery, no import-from-worker issues.

Exported names (all land in the kernel globals via `exec` of the inlined
source):
    styled_figure(figsize=(6.4, 4.0)) -> (fig, ax)
    save_figure(fig, fig_id, caption, width=0.8) -> None
    annotate_peak(ax, x, y, label=None) -> None

This module itself is NOT imported by the kernel at runtime. It exists so the
helpers can be unit-tested in the worker process and so the source of truth
for the inlined string lives in a real `.py` file (the bootstrap reader in
`kernel/manager.py` pulls the source verbatim).
"""

from __future__ import annotations

# The body below is the canonical source — `kernel/manager.py` reads this
# file's `HELPER_SOURCE` constant and stitches it into the bootstrap cell.
#
# Intentional minimalism: no logging, no structured events, just matplotlib
# + a JSON print line. The Coder reads stdout to confirm the save happened.

HELPER_SOURCE = '''
import json as _json
from pathlib import Path as _Path

def styled_figure(figsize=(6.4, 4.0)):
    """Project-standard figure: rcParams from bootstrap already apply; this
    just wraps `plt.subplots` so user code reads consistently.
    """
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=figsize)
    return fig, ax

def save_figure(fig, fig_id, caption, width=0.8):
    """Save PNG + SVG under `figures/<fig_id>.{png,svg}` and print a one-line
    JSON marker so the agent loop (which watches kernel.stdout) has an extra
    breadcrumb beyond the usual `figures_saved` registration.
    """
    import matplotlib.pyplot as plt
    import re as _re
    if not _re.match(r"^[a-z][a-z0-9_]*$", fig_id):
        raise ValueError(
            "fig_id must be snake_case slug (lowercase, digits, underscore)"
        )
    if not (0.0 < float(width) <= 1.0):
        raise ValueError("width must be in (0, 1]")
    figures_dir = _Path("figures")
    figures_dir.mkdir(exist_ok=True)
    png = figures_dir / (fig_id + ".png")
    svg = figures_dir / (fig_id + ".svg")
    fig.savefig(str(png))
    fig.savefig(str(svg))
    plt.close(fig)
    print(_json.dumps({
        "__figure__": True,
        "id": fig_id,
        "caption": caption,
        "width": float(width),
        "path_png": "figures/" + fig_id + ".png",
        "path_svg": "figures/" + fig_id + ".svg",
    }, ensure_ascii=False))

def annotate_peak(ax, x, y, label=None):
    """Mark a single point with a small arrow + optional label."""
    text = label if label is not None else "peak (%.3g, %.3g)" % (x, y)
    ax.annotate(
        text,
        xy=(x, y),
        xytext=(12, 12),
        textcoords="offset points",
        fontsize=9,
        arrowprops=dict(arrowstyle="->", lw=0.8, color="black"),
    )
'''


__all__ = ["HELPER_SOURCE"]
