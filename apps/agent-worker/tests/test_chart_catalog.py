"""Tests for `agent_worker.chart_catalog`.

Covers:
- every ChartType has legal shape (slug id, non-empty descriptions, pitfalls,
  `plt.savefig` in its snippet);
- `get()` returns the right object and raises on unknown ids;
- every snippet `compile()`s as valid Python;
- the prompt-facing markdown index contains every id and stays small.

A deliberate non-goal: `exec()`ing every snippet in pytest. That is exercised
by a manual smoke script during authoring — running all 20 under Agg adds
~10s per test run and the `compile()` check already catches 90% of regressions.
"""

from __future__ import annotations

import re

import pytest

from agent_worker import chart_catalog
from agent_worker.chart_catalog import (
    ChartType,
    all_chart_types,
    get,
    ids,
    render_index_markdown,
)

SLUG = re.compile(r"^[a-z][a-z0-9_]*$")

# Competition must-haves — if one goes missing we want to know loudly.
REQUIRED_IDS = {
    "heatmap_sensitivity",
    "heatmap_correlation",
    "residual_plot",
    "convergence_curve",
    "tornado_sensitivity",
    "pareto_front",
    "network_graph",
}


# ---------------------------------------------------------------- shape tests


def test_catalog_has_at_least_15_entries() -> None:
    assert len(all_chart_types()) >= 15


def test_catalog_has_all_required_chart_types() -> None:
    present = set(ids())
    missing = REQUIRED_IDS - present
    assert not missing, f"required chart types missing: {sorted(missing)}"


def test_ids_are_unique() -> None:
    all_ids = [c.id for c in all_chart_types()]
    assert len(all_ids) == len(set(all_ids))


@pytest.mark.parametrize("chart", list(all_chart_types()), ids=lambda c: c.id)
def test_every_chart_has_valid_shape(chart: ChartType) -> None:
    assert SLUG.match(chart.id), f"bad slug: {chart.id!r}"
    assert chart.display_name.strip(), f"{chart.id} missing display_name"
    assert chart.when_to_use.strip(), f"{chart.id} missing when_to_use"
    assert chart.when_not_to_use.strip(), f"{chart.id} missing when_not_to_use"
    assert len(chart.pitfalls) >= 1, f"{chart.id} needs at least one pitfall"
    assert len(chart.keywords) >= 1, f"{chart.id} needs at least one keyword"


@pytest.mark.parametrize("chart", list(all_chart_types()), ids=lambda c: c.id)
def test_every_snippet_saves_a_figure(chart: ChartType) -> None:
    snippet = chart.matplotlib_snippet
    assert "plt.savefig" in snippet, f"{chart.id} snippet never calls savefig"
    # We want both a PNG and an SVG saved — the Writer requires the SVG for
    # LaTeX export. The call pattern should reference the chart id as filename.
    assert f"figures/{chart.id}.png" in snippet, (
        f"{chart.id} snippet must save PNG under figures/{chart.id}.png"
    )
    assert f"figures/{chart.id}.svg" in snippet, (
        f"{chart.id} snippet must save SVG under figures/{chart.id}.svg"
    )
    assert "plt.close" in snippet, (
        f"{chart.id} snippet must close the figure to avoid leaking memory"
    )


@pytest.mark.parametrize("chart", list(all_chart_types()), ids=lambda c: c.id)
def test_every_snippet_compiles(chart: ChartType) -> None:
    # `compile()` catches syntax / indentation errors without actually
    # executing matplotlib; fast and robust for CI.
    compile(chart.matplotlib_snippet, f"<catalog:{chart.id}>", "exec")


@pytest.mark.parametrize("chart", list(all_chart_types()), ids=lambda c: c.id)
def test_every_snippet_uses_only_matplotlib_and_numpy(chart: ChartType) -> None:
    # Worker kernel only guarantees matplotlib, numpy, scipy, sklearn, pandas.
    # Seaborn and plotly are NOT installed — catch accidental imports early.
    forbidden = {"seaborn", "plotly", "bokeh", "altair", "networkx"}
    for line in chart.matplotlib_snippet.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("import ", "from ")):
            continue
        # Extract module head: `import foo.bar` / `from foo.bar import x`.
        tokens = stripped.split()
        if tokens[0] == "import":
            mod = tokens[1].split(".")[0]
        else:  # "from X import ..."
            mod = tokens[1].split(".")[0]
        assert mod not in forbidden, (
            f"{chart.id} imports forbidden module {mod!r} "
            f"(kernel only ships matplotlib/numpy/scipy/sklearn/pandas)"
        )


# ------------------------------------------------------------------- lookups


def test_get_returns_matching_chart() -> None:
    c = get("heatmap_sensitivity")
    assert isinstance(c, ChartType)
    assert c.id == "heatmap_sensitivity"


def test_get_unknown_id_raises_keyerror() -> None:
    with pytest.raises(KeyError) as exc:
        get("no_such_chart_please")
    # Error message must list the known ids so the caller can self-correct.
    assert "no_such_chart_please" in str(exc.value)


def test_ids_matches_catalog_order() -> None:
    assert ids() == tuple(c.id for c in all_chart_types())


# --------------------------------------------------------------- markdown index


def test_render_index_markdown_contains_every_id() -> None:
    md = render_index_markdown()
    for chart_id in ids():
        assert f"`{chart_id}`" in md, f"id {chart_id} absent from index"


def test_render_index_markdown_is_prompt_sized() -> None:
    # We commit to a tight budget: the whole index must fit in ~2k chars so
    # adding it to the Coder prompt doesn't inflate token usage meaningfully.
    md = render_index_markdown()
    assert len(md) < 6000, f"index too large ({len(md)} chars)"


def test_render_index_markdown_has_table_header() -> None:
    md = render_index_markdown()
    first_two = md.splitlines()[:2]
    assert first_two[0].startswith("|") and "id" in first_two[0]
    assert "---" in first_two[1]


# ----------------------------------------------------- dataclass invariants


def test_chart_type_rejects_bad_slug() -> None:
    with pytest.raises(ValueError):
        ChartType(
            id="Bad-ID",
            display_name="x",
            when_to_use="y",
            when_not_to_use="z",
            keywords=("a",),
            matplotlib_snippet="plt.savefig('figures/x.png')",
            pitfalls=("w",),
        )


def test_chart_type_rejects_snippet_without_savefig() -> None:
    with pytest.raises(ValueError):
        ChartType(
            id="no_save",
            display_name="x",
            when_to_use="y",
            when_not_to_use="z",
            keywords=("a",),
            matplotlib_snippet="print('hi')",
            pitfalls=("w",),
        )


def test_chart_type_rejects_empty_pitfalls() -> None:
    with pytest.raises(ValueError):
        ChartType(
            id="nopit",
            display_name="x",
            when_to_use="y",
            when_not_to_use="z",
            keywords=("a",),
            matplotlib_snippet="plt.savefig('figures/x.png')",
            pitfalls=(),
        )


# ---------------------------------------------------- helper source sanity

def test_helper_source_is_injected_into_bootstrap() -> None:
    """The kernel bootstrap must pull in `_chart_helpers.HELPER_SOURCE` so
    `styled_figure` / `save_figure` are defined in user code's globals.
    """
    from agent_worker._chart_helpers import HELPER_SOURCE
    from agent_worker.kernel.manager import _MPL_BOOTSTRAP_SRC

    assert "def styled_figure" in HELPER_SOURCE
    assert "def save_figure" in HELPER_SOURCE
    assert HELPER_SOURCE.strip() in _MPL_BOOTSTRAP_SRC


def test_helper_source_compiles() -> None:
    from agent_worker._chart_helpers import HELPER_SOURCE

    compile(HELPER_SOURCE, "<chart_helpers>", "exec")


def test_module_exports_match_all() -> None:
    # Sanity: the public surface is small and intentional.
    assert set(chart_catalog.__all__) == {
        "ChartType",
        "all_chart_types",
        "get",
        "ids",
        "render_index_markdown",
    }
