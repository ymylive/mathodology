---
name: chart_catalog
description: 20 canonical chart types for award-level papers — id, when-to-use, captions, pitfalls. Coder picks by id when emitting figures.
when_to_use:
  - "any time the Coder creates a figure"
allowed-tools:
  - run_python
  - run_matlab
context: inline
---

# Chart Catalog — 20 Canonical Figures for Award-Level Papers

Specification of the figure-emission contract for the Coder agent. Each chart
has a stable `id` that Coder writes into its JSON directive's
`figures_planned[*].chart_type_id` field; the Jupyter kernel then renders a
matplotlib snippet from `agent_worker.chart_catalog._CATALOG`.

All snippets use **matplotlib only** — no seaborn, plotly, networkx, or any
library outside the worker kernel's import list. Every snippet saves both PNG
(LaTeX preview) and SVG (final export) into `figures/<id>.{png,svg}` and the
Coder's `figures_saved` list must mirror those paths.

## Caption template

Every figure caption follows this shape so the export pipeline can attach the
paper-source linkage automatically:

```
Figure {n}. {display_name} — {what it shows in the present problem}.
（Source: section {sec.id} of the paper.）
```

## Catalog entries

### `line_plot` — 折线图 / Line plot

**When to use.** Show one or more continuous series over an ordered x-axis (time, iteration, parameter sweep).

**When NOT to use.** Discrete unordered categories — use bar_grouped_stacked instead.

**Match keywords.** time series, trend, 时间序列, 趋势, line

**Caption template.**

> Figure {n}. 折线图 / Line plot — {what line_plot shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Too many overlapping series (>5) become unreadable — split the panel.

**Secondary pitfalls.**
- Don't draw a line between unordered categories; use a bar chart.

### `line_with_ci` — 带置信区间的折线图 / Line plot with CI band

**When to use.** A single trend whose uncertainty matters: bootstrap CI, prediction band, Monte-Carlo quantiles.

**When NOT to use.** When the band width is constant — a legend note is cheaper than a band.

**Match keywords.** confidence interval, band, 置信区间, bootstrap

**Caption template.**

> Figure {n}. 带置信区间的折线图 / Line plot with CI band — {what line_with_ci shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** State the CI level (95%, 1σ, ...) in the legend or caption.

**Secondary pitfalls.**
- If lower > upper at some x, fill_between silently inverts — validate.

### `scatter_regression` — 散点 + 回归线 / Scatter with regression fit

**When to use.** Two continuous variables where you want to show both the raw cloud and an OLS fit (R² annotated).

**When NOT to use.** Strongly non-linear relationships — fit curves separately and use contour_2d or a parametric model.

**Match keywords.** correlation, regression, scatter, R2, 回归

**Caption template.**

> Figure {n}. 散点 + 回归线 / Scatter with regression fit — {what scatter_regression shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** R² on a non-linear relationship is misleading — check residuals.

**Secondary pitfalls.**
- Heavy overplot hides density; drop alpha or switch to hexbin.

### `histogram_kde` — 直方图叠 KDE / Histogram with KDE overlay

**When to use.** Show the empirical distribution of one continuous variable, optionally across sub-groups.

**When NOT to use.** Discrete-integer data — use a bar chart of counts.

**Match keywords.** distribution, density, 直方图, KDE

**Caption template.**

> Figure {n}. 直方图叠 KDE / Histogram with KDE overlay — {what histogram_kde shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Bandwidth matters — Scott's rule is the default but may over-smooth.

**Secondary pitfalls.**
- Bin width also matters — try several and pick what matches the data.

### `boxplot_grouped` — 分组箱线图 / Grouped boxplot

**When to use.** Compare the distribution (median, IQR, outliers) of one variable across 2–10 groups.

**When NOT to use.** Small samples (n<10 per group) — use a strip/swarm plot of raw points.

**Match keywords.** boxplot, groups, IQR, outliers, 分组

**Caption template.**

> Figure {n}. 分组箱线图 / Grouped boxplot — {what boxplot_grouped shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** A box hides bimodality — if in doubt, prefer violinplot.

**Secondary pitfalls.**
- Outliers are defined by 1.5·IQR by default; say so in the caption.

### `violinplot` — 小提琴图 / Violin plot

**When to use.** Like boxplot but you also want to see the shape of each group's distribution.

**When NOT to use.** Tiny samples — the KDE inside each violin is unreliable below n≈30.

**Match keywords.** violin, distribution, shape, 分布

**Caption template.**

> Figure {n}. 小提琴图 / Violin plot — {what violinplot shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** KDE bandwidth can under- or over-smooth — document what you used.

**Secondary pitfalls.**
- Truncate the violin at data extrema or the tails look unreasonable.

### `heatmap_correlation` — 相关系数热图 / Correlation heatmap

**When to use.** Show pairwise Pearson/Spearman correlations for 4–20 variables.

**When NOT to use.** More than ~20 variables — the labels collapse; cluster first or use a dendrogram.

**Match keywords.** correlation, matrix, 相关, heatmap

**Caption template.**

> Figure {n}. 相关系数热图 / Correlation heatmap — {what heatmap_correlation shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Use a diverging cmap (e.g. RdBu_r) centred at 0 — viridis misleads.

**Secondary pitfalls.**
- Annotate cell values only when the matrix is small enough.

### `heatmap_sensitivity` — 灵敏度热图 / Parameter sensitivity heatmap

**When to use.** Scan two parameters (α, β) and score an objective on the grid — show the response surface as colour.

**When NOT to use.** Three or more parameters — use small multiples or slicing, or surface_3d for a single slice.

**Match keywords.** sensitivity, parameter, grid, 灵敏度, 扫描

**Caption template.**

> Figure {n}. 灵敏度热图 / Parameter sensitivity heatmap — {what heatmap_sensitivity shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Coarse grids lie — refine near the argmax.

**Secondary pitfalls.**
- Mark the argmax explicitly (a star glyph) so the reader sees it.

### `contour_2d` — 等高线图 / 2D contour plot

**When to use.** Visualise f(x, y) over a region; especially good for optimisation landscapes like Rosenbrock, Himmelblau.

**When NOT to use.** The function has discontinuities or narrow spikes — contours look messy; prefer a heatmap.

**Match keywords.** contour, level set, objective landscape, 等高线

**Caption template.**

> Figure {n}. 等高线图 / 2D contour plot — {what contour_2d shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Linear levels hide wide dynamic range — use logspace.

**Secondary pitfalls.**
- Without clabel / colourbar the reader can't tell level values.

### `surface_3d` — 三维响应面 / 3D surface

**When to use.** Dramatic visualisation of a 2D response surface for presentations.

**When NOT to use.** Precise value reading — occlusion makes 3D plots unreliable; contour_2d is usually better for a paper figure.

**Match keywords.** 3d, surface, response surface, 三维

**Caption template.**

> Figure {n}. 三维响应面 / 3D surface — {what surface_3d shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Viewing angle changes the message — pick one and state it.

**Secondary pitfalls.**
- Import `mpl_toolkits.mplot3d` (noqa: F401) before `projection='3d'`.

### `bar_grouped_stacked` — 分组 / 堆叠柱状图 / Grouped or stacked bars

**When to use.** Compare a small number of categories (<12) across 2–4 series.

**When NOT to use.** Continuous x-axis — that is a line plot's job.

**Match keywords.** bar chart, categories, grouped, stacked, 柱状

**Caption template.**

> Figure {n}. 分组 / 堆叠柱状图 / Grouped or stacked bars — {what bar_grouped_stacked shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Stacked bars hide individual series values — grouped is often clearer.

**Secondary pitfalls.**
- Sort categories by value (or by meaningful order) — alphabetical is rarely useful.

### `radar_chart` — 雷达图 / Radar (spider) chart

**When to use.** Compare ≤4 items on 4–8 already-normalised metrics.

**When NOT to use.** The metrics live on different scales or have natural ordering — radar charts distort both. A parallel-coordinates plot is safer.

**Match keywords.** radar, spider, multi-criteria, 多维评分

**Caption template.**

> Figure {n}. 雷达图 / Radar (spider) chart — {what radar_chart shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Axis order changes the perceived area — pick a deliberate order.

**Secondary pitfalls.**
- Normalise metrics to [0, 1] before plotting.

### `residual_plot` — 残差图 / Residual plot

**When to use.** Diagnose a regression: residuals vs fitted should look like a random band around 0.

**When NOT to use.** Classification models — use a confusion matrix or ROC curve.

**Match keywords.** residual, regression diagnostic, 残差

**Caption template.**

> Figure {n}. 残差图 / Residual plot — {what residual_plot shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Patterns (funnel, curve) in residuals = model misspecification — say what you saw.

**Secondary pitfalls.**
- Plot residuals vs fitted, not vs x — trends in fitted values are what matter.

### `qq_plot` — Q-Q 图 / Q-Q plot (normality)

**When to use.** Check whether residuals (or any sample) are approximately normal — points should fall on the reference line.

**When NOT to use.** Discrete data — quantiles are staircase-shaped and mislead.

**Match keywords.** qq, normality, quantile, 正态性

**Caption template.**

> Figure {n}. Q-Q 图 / Q-Q plot (normality) — {what qq_plot shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Heavy tails bend the ends of the plot — report the deviation.

**Secondary pitfalls.**
- Sample size matters: n<30 Q-Q plots are noisy.

### `roc_curve` — ROC 曲线 / ROC curve with AUC

**When to use.** Binary classifier quality independent of the decision threshold.

**When NOT to use.** Highly imbalanced classes — prefer Precision-Recall; AUC is too optimistic there.

**Match keywords.** roc, auc, classifier, binary

**Caption template.**

> Figure {n}. ROC 曲线 / ROC curve with AUC — {what roc_curve shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Put AUC in the legend — the curve alone is hard to read.

**Secondary pitfalls.**
- Plot the y=x chance line for reference.

### `confusion_matrix` — 混淆矩阵 / Confusion matrix

**When to use.** Classification error breakdown per class, typically normalised by row.

**When NOT to use.** Regression tasks — use a residual plot.

**Match keywords.** confusion, classification, error breakdown, 混淆矩阵

**Caption template.**

> Figure {n}. 混淆矩阵 / Confusion matrix — {what confusion_matrix shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Always annotate whether counts or row-normalised rates are shown.

**Secondary pitfalls.**
- Row-normalise so imbalanced classes are comparable.

### `convergence_curve` — 收敛曲线 / Convergence / training curve

**When to use.** Optimiser loss vs iteration, training and validation together — shows if you converged and whether you overfit.

**When NOT to use.** Single-shot non-iterative methods (closed-form OLS, LP solvers).

**Match keywords.** convergence, loss, epoch, training, 收敛

**Caption template.**

> Figure {n}. 收敛曲线 / Convergence / training curve — {what convergence_curve shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Use log scale for loss when it spans orders of magnitude.

**Secondary pitfalls.**
- Always plot validation alongside training; training-only plots hide overfitting.

### `tornado_sensitivity` — Tornado 灵敏度图 / Tornado sensitivity

**When to use.** Rank the per-parameter one-at-a-time effect on the objective. Largest-swing parameter at the top.

**When NOT to use.** Strong parameter interactions — OAT sensitivity misses them; use Sobol indices instead.

**Match keywords.** tornado, sensitivity, importance, one at a time

**Caption template.**

> Figure {n}. Tornado 灵敏度图 / Tornado sensitivity — {what tornado_sensitivity shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** State the low/high perturbation magnitude — ±1σ? ±10%? ±full range?

**Secondary pitfalls.**
- OAT ignores interactions; caveat the result in the caption.

### `pareto_front` — Pareto 前沿 / Pareto front

**When to use.** Two conflicting objectives you want to minimise — plot all candidates and highlight the non-dominated set.

**When NOT to use.** >3 objectives — 2D projections mislead. Use parallel coordinates.

**Match keywords.** pareto, multi-objective, tradeoff, 多目标

**Caption template.**

> Figure {n}. Pareto 前沿 / Pareto front — {what pareto_front shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** Say which direction is 'better' for each axis — minimise or maximise.

**Secondary pitfalls.**
- The non-dominated sort assumes minimisation on both axes; flip sign if needed.

### `network_graph` — 关系网络图 / Network graph

**When to use.** Small (|V|≤20) graph with nodes and edges — flow networks, dependency graphs, social ties.

**When NOT to use.** Dense graphs with hundreds of nodes — use an adjacency heatmap.

**Match keywords.** network, graph, nodes, edges, 关系图

**Caption template.**

> Figure {n}. 关系网络图 / Network graph — {what network_graph shows on the present data}. （Source: section {sec.id}）

**Primary pitfall.** networkx is NOT installed in the worker kernel; use a manual layout.

**Secondary pitfalls.**
- Circular layout hides clusters — for structured graphs compute positions by hand.

## Selection workflow

1. Read the Modeler's `figures_planned[*]`; each entry already names a chart id.
2. If Coder needs to add a figure not in the plan, pick the id whose **When to
   use** is the closest semantic match and emit a `figures_planned` patch
   alongside the snippet.
3. Never invent an id; the kernel will reject unknown ids at validation time
   (see `chart_catalog.get(chart_id)` — it raises `KeyError`).
4. Each snippet calls `plt.savefig(...)` twice (PNG + SVG). Do NOT change the
   filenames — the export pipeline globs them by id.

## Programmatic access

```python
from agent_worker.chart_catalog import all_chart_types, get, ids, render_index_markdown

get("tornado_sensitivity").matplotlib_snippet  # full template
ids()                                              # tuple of slugs
render_index_markdown()                            # compact table for prompt
```
