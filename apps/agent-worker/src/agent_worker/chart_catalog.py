"""Chart type catalog for the CoderAgent.

The Coder's Jupyter kernel only ships matplotlib (no seaborn, no plotly, no
networkx) so every snippet here is pure matplotlib + numpy. Each entry is a
small, self-contained template the LLM can paste and then fill in with real
data — placeholders follow `str.format` style (`{x_data}`, `{title}`, ...)
so the LLM can rewrite them without breaking quotes.

Design choices
--------------
* Frozen dataclass, not pydantic: the catalog is build-time immutable data;
  no runtime validation needed and pydantic adds import cost we don't need.
* 18 entries, covering the six competition must-haves (heatmap / residual /
  convergence / tornado / pareto / network). Each `matplotlib_snippet` stays
  30–60 lines end-to-end (figure → plot → save → close) so it reliably fits
  a single Jupyter cell.
* The module also exposes `render_index_markdown()` for prompt injection —
  the full snippets are intentionally NOT rendered into the prompt to keep
  token usage bounded; only id + display_name + when_to_use + one pitfall.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class ChartType:
    """A single chart template with selection guidance and code."""

    id: str
    display_name: str
    when_to_use: str
    when_not_to_use: str
    keywords: tuple[str, ...]
    matplotlib_snippet: str
    pitfalls: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not _SLUG_RE.match(self.id):
            raise ValueError(f"ChartType.id must be a slug, got {self.id!r}")
        if not self.when_to_use.strip():
            raise ValueError(f"ChartType {self.id!r} has empty when_to_use")
        if not self.pitfalls:
            raise ValueError(f"ChartType {self.id!r} has no pitfalls listed")
        if "plt.savefig" not in self.matplotlib_snippet:
            raise ValueError(
                f"ChartType {self.id!r} snippet must include plt.savefig(...)"
            )


# ---------------------------------------------------------------- snippets
# Each snippet is written so `compile()` succeeds and (when the placeholders
# are left as-is) `exec()` runs against plausible default data. The LLM is
# expected to rewrite the data lines; the structure around them is the
# reusable part.

_LINE_PLOT = """
import numpy as np
import matplotlib.pyplot as plt

# Replace `x` and `series` with your data; defaults exercise the template.
x = np.linspace(0, 10, 100)
series = {"baseline": np.sin(x), "improved": np.sin(x) + 0.2 * np.cos(2 * x)}

fig, ax = plt.subplots(figsize=(6.4, 4.0))
for label, y in series.items():
    ax.plot(x, y, label=label, linewidth=1.6)
ax.set_xlabel("x")
ax.set_ylabel("value")
ax.set_title("折线图示例 / line plot")
ax.legend(loc="best", frameon=False)
plt.savefig("figures/line_plot.png")
plt.savefig("figures/line_plot.svg")
plt.close(fig)
""".strip()


_LINE_WITH_CI = """
import numpy as np
import matplotlib.pyplot as plt

x = np.linspace(0, 10, 100)
y_mean = np.sin(x)
y_std = 0.15 + 0.05 * np.abs(np.cos(x))
lower = y_mean - 1.96 * y_std
upper = y_mean + 1.96 * y_std

fig, ax = plt.subplots(figsize=(6.4, 4.0))
ax.plot(x, y_mean, color="C0", linewidth=1.8, label="均值 / mean")
ax.fill_between(x, lower, upper, color="C0", alpha=0.2, label="95% CI")
ax.set_xlabel("x")
ax.set_ylabel("y")
ax.set_title("带置信区间的折线图 / line with CI band")
ax.legend(loc="best", frameon=False)
plt.savefig("figures/line_with_ci.png")
plt.savefig("figures/line_with_ci.svg")
plt.close(fig)
""".strip()


_SCATTER_REGRESSION = """
import numpy as np
import matplotlib.pyplot as plt

rng = np.random.default_rng(0)
x = rng.uniform(0, 10, size=80)
y = 1.5 * x + 2.0 + rng.normal(0, 2.0, size=x.size)

# OLS via polyfit — avoids a scikit-learn dependency just for a regression line.
slope, intercept = np.polyfit(x, y, 1)
y_hat = slope * x + intercept
ss_res = np.sum((y - y_hat) ** 2)
ss_tot = np.sum((y - y.mean()) ** 2)
r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

fig, ax = plt.subplots(figsize=(6.4, 4.0))
ax.scatter(x, y, s=18, alpha=0.7, label="观测 / observed")
xs = np.linspace(x.min(), x.max(), 100)
ax.plot(xs, slope * xs + intercept, color="C3", linewidth=1.8, label="拟合 / fit")
ax.text(0.04, 0.94, f"$R^2 = {r2:.3f}$", transform=ax.transAxes,
        va="top", fontsize=10)
ax.set_xlabel("x")
ax.set_ylabel("y")
ax.set_title("散点 + 回归 / scatter with regression")
ax.legend(loc="lower right", frameon=False)
plt.savefig("figures/scatter_regression.png")
plt.savefig("figures/scatter_regression.svg")
plt.close(fig)
""".strip()


_HISTOGRAM_KDE = """
import numpy as np
import matplotlib.pyplot as plt

rng = np.random.default_rng(1)
data = np.concatenate([rng.normal(-1, 0.8, 400), rng.normal(2, 1.2, 600)])

# Manual Gaussian KDE on a grid — scipy.stats.gaussian_kde also works.
from scipy.stats import gaussian_kde
kde = gaussian_kde(data)
grid = np.linspace(data.min() - 1, data.max() + 1, 200)

fig, ax = plt.subplots(figsize=(6.4, 4.0))
ax.hist(data, bins=40, density=True, alpha=0.5, color="C0", label="直方图 / hist")
ax.plot(grid, kde(grid), color="C3", linewidth=1.8, label="KDE")
ax.set_xlabel("value")
ax.set_ylabel("density")
ax.set_title("直方图叠加 KDE / histogram with KDE")
ax.legend(loc="best", frameon=False)
plt.savefig("figures/histogram_kde.png")
plt.savefig("figures/histogram_kde.svg")
plt.close(fig)
""".strip()


_BOXPLOT_GROUPED = """
import numpy as np
import matplotlib.pyplot as plt

rng = np.random.default_rng(2)
groups = {"A": rng.normal(0, 1, 100), "B": rng.normal(0.5, 1.2, 100),
          "C": rng.normal(-0.3, 0.8, 100), "D": rng.normal(1.0, 1.5, 100)}
labels = list(groups.keys())
values = [groups[k] for k in labels]

fig, ax = plt.subplots(figsize=(6.4, 4.0))
bp = ax.boxplot(values, labels=labels, patch_artist=True, widths=0.6)
for patch, color in zip(bp["boxes"], plt.cm.tab10.colors):
    patch.set_facecolor(color)
    patch.set_alpha(0.6)
ax.set_ylabel("value")
ax.set_title("分组箱线图 / grouped box plot")
plt.savefig("figures/boxplot_grouped.png")
plt.savefig("figures/boxplot_grouped.svg")
plt.close(fig)
""".strip()


_VIOLIN_PLOT = """
import numpy as np
import matplotlib.pyplot as plt

rng = np.random.default_rng(3)
groups = [rng.normal(0, 1, 200), rng.normal(1.0, 1.4, 200),
          rng.normal(-0.5, 0.7, 200)]
labels = ["A", "B", "C"]

fig, ax = plt.subplots(figsize=(6.4, 4.0))
parts = ax.violinplot(groups, showmeans=True, showmedians=False)
for i, body in enumerate(parts["bodies"]):
    body.set_facecolor(plt.cm.tab10.colors[i])
    body.set_alpha(0.55)
ax.set_xticks(range(1, len(labels) + 1))
ax.set_xticklabels(labels)
ax.set_ylabel("value")
ax.set_title("小提琴图 / violin plot")
plt.savefig("figures/violinplot.png")
plt.savefig("figures/violinplot.svg")
plt.close(fig)
""".strip()


_HEATMAP_CORRELATION = """
import numpy as np
import matplotlib.pyplot as plt

rng = np.random.default_rng(4)
n, k = 200, 6
X = rng.normal(size=(n, k))
X[:, 1] = 0.8 * X[:, 0] + 0.2 * X[:, 1]
X[:, 3] = -0.6 * X[:, 2] + 0.4 * X[:, 3]
corr = np.corrcoef(X, rowvar=False)
labels = [f"x{i}" for i in range(k)]

fig, ax = plt.subplots(figsize=(6.4, 5.2))
im = ax.imshow(corr, cmap="RdBu_r", vmin=-1.0, vmax=1.0)
ax.set_xticks(range(k)); ax.set_xticklabels(labels, rotation=45, ha="right")
ax.set_yticks(range(k)); ax.set_yticklabels(labels)
for i in range(k):
    for j in range(k):
        ax.text(j, i, f"{corr[i, j]:.2f}", ha="center", va="center",
                color="white" if abs(corr[i, j]) > 0.5 else "black", fontsize=8)
ax.set_title("相关系数热图 / correlation heatmap")
fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
plt.savefig("figures/heatmap_correlation.png")
plt.savefig("figures/heatmap_correlation.svg")
plt.close(fig)
""".strip()


_HEATMAP_SENSITIVITY = """
import numpy as np
import matplotlib.pyplot as plt

# Grid-scan two parameters and score an objective.
alpha_grid = np.linspace(0.1, 2.0, 20)
beta_grid = np.linspace(0.0, 1.0, 15)
A, B = np.meshgrid(alpha_grid, beta_grid)
Z = np.exp(-((A - 1.2) ** 2 + (B - 0.4) ** 2))  # replace with real objective

fig, ax = plt.subplots(figsize=(6.4, 4.4))
im = ax.pcolormesh(A, B, Z, cmap="viridis", shading="auto")
ax.set_xlabel(r"$\\alpha$")
ax.set_ylabel(r"$\\beta$")
ax.set_title("参数灵敏度热图 / parameter sensitivity heatmap")
# Mark the argmax.
iy, ix = np.unravel_index(np.argmax(Z), Z.shape)
ax.plot(A[iy, ix], B[iy, ix], marker="*", color="white", markersize=14,
        markeredgecolor="black", linewidth=0)
fig.colorbar(im, ax=ax, label="objective")
plt.savefig("figures/heatmap_sensitivity.png")
plt.savefig("figures/heatmap_sensitivity.svg")
plt.close(fig)
""".strip()


_CONTOUR_2D = """
import numpy as np
import matplotlib.pyplot as plt

x = np.linspace(-3, 3, 200)
y = np.linspace(-3, 3, 200)
X, Y = np.meshgrid(x, y)
Z = (1 - X) ** 2 + 100 * (Y - X ** 2) ** 2  # Rosenbrock — replace as needed

fig, ax = plt.subplots(figsize=(6.4, 4.8))
# Log levels so the valley is visible despite the wide dynamic range.
levels = np.logspace(-1, 3.5, 20)
cs = ax.contour(X, Y, Z, levels=levels, cmap="viridis", linewidths=0.8)
ax.clabel(cs, inline=True, fontsize=7, fmt="%.1f")
ax.plot(1.0, 1.0, marker="*", color="red", markersize=14, linewidth=0,
        label="global min")
ax.set_xlabel("x"); ax.set_ylabel("y")
ax.set_title("等高线 / contour of f(x,y)")
ax.legend(loc="upper left", frameon=False)
plt.savefig("figures/contour_2d.png")
plt.savefig("figures/contour_2d.svg")
plt.close(fig)
""".strip()


_SURFACE_3D = """
import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 — register 3d projection

x = np.linspace(-2, 2, 60)
y = np.linspace(-2, 2, 60)
X, Y = np.meshgrid(x, y)
Z = np.sin(np.sqrt(X ** 2 + Y ** 2)) * np.exp(-0.1 * (X ** 2 + Y ** 2))

fig = plt.figure(figsize=(6.4, 4.8))
ax = fig.add_subplot(111, projection="3d")
surf = ax.plot_surface(X, Y, Z, cmap="viridis", linewidth=0, antialiased=True,
                       alpha=0.9)
ax.set_xlabel("x"); ax.set_ylabel("y"); ax.set_zlabel("f(x,y)")
ax.set_title("三维响应面 / 3D surface")
fig.colorbar(surf, ax=ax, shrink=0.7, pad=0.1)
plt.savefig("figures/surface_3d.png")
plt.savefig("figures/surface_3d.svg")
plt.close(fig)
""".strip()


_BAR_GROUPED = """
import numpy as np
import matplotlib.pyplot as plt

categories = ["Q1", "Q2", "Q3", "Q4"]
series = {"2022": [12, 18, 15, 22], "2023": [14, 20, 17, 25],
          "2024": [16, 22, 19, 28]}
x = np.arange(len(categories))
width = 0.8 / len(series)

fig, ax = plt.subplots(figsize=(6.4, 4.0))
for i, (label, values) in enumerate(series.items()):
    offset = (i - (len(series) - 1) / 2) * width
    ax.bar(x + offset, values, width=width, label=label)
ax.set_xticks(x); ax.set_xticklabels(categories)
ax.set_ylabel("value")
ax.set_title("分组柱状图 / grouped bar chart")
ax.legend(loc="best", frameon=False)
plt.savefig("figures/bar_grouped_stacked.png")
plt.savefig("figures/bar_grouped_stacked.svg")
plt.close(fig)
""".strip()


_RADAR_CHART = """
import numpy as np
import matplotlib.pyplot as plt

axes_labels = ["accuracy", "speed", "robust", "interpret", "cost"]
scores = {"model A": [0.85, 0.70, 0.60, 0.75, 0.80],
          "model B": [0.78, 0.88, 0.72, 0.65, 0.70]}

k = len(axes_labels)
angles = np.linspace(0, 2 * np.pi, k, endpoint=False).tolist()
angles += angles[:1]  # close the polygon

fig, ax = plt.subplots(figsize=(5.6, 5.6), subplot_kw={"projection": "polar"})
for label, values in scores.items():
    vals = list(values) + [values[0]]
    ax.plot(angles, vals, linewidth=1.8, label=label)
    ax.fill(angles, vals, alpha=0.15)
ax.set_xticks(angles[:-1]); ax.set_xticklabels(axes_labels)
ax.set_ylim(0, 1)
ax.set_title("雷达图 / radar chart", y=1.08)
ax.legend(loc="upper right", bbox_to_anchor=(1.25, 1.05), frameon=False)
plt.savefig("figures/radar_chart.png")
plt.savefig("figures/radar_chart.svg")
plt.close(fig)
""".strip()


_RESIDUAL_PLOT = r"""
import numpy as np
import matplotlib.pyplot as plt

rng = np.random.default_rng(5)
x = np.linspace(0, 10, 120)
y_true = 0.8 * x + 1.5
y_obs = y_true + rng.normal(0, 0.8, size=x.size)
slope, intercept = np.polyfit(x, y_obs, 1)
y_hat = slope * x + intercept
resid = y_obs - y_hat

fig, ax = plt.subplots(figsize=(6.4, 4.0))
ax.scatter(y_hat, resid, s=18, alpha=0.7)
ax.axhline(0.0, color="C3", linewidth=1.4)
ax.set_xlabel(r"fitted value  $\hat{y}$")
ax.set_ylabel(r"residual  $y - \hat{y}$")
ax.set_title("残差图 / residual plot")
plt.savefig("figures/residual_plot.png")
plt.savefig("figures/residual_plot.svg")
plt.close(fig)
""".strip()


_QQ_PLOT = """
import numpy as np
import matplotlib.pyplot as plt
from scipy import stats

rng = np.random.default_rng(6)
sample = rng.normal(0.2, 1.1, 200)

# scipy.stats.probplot returns (theoretical, ordered) and fitted line params.
(osm, osr), (slope, intercept, _) = stats.probplot(sample, dist="norm")

fig, ax = plt.subplots(figsize=(6.4, 4.0))
ax.scatter(osm, osr, s=18, alpha=0.7, label="样本分位数 / sample")
line_x = np.array([osm.min(), osm.max()])
ax.plot(line_x, slope * line_x + intercept, color="C3", linewidth=1.6,
        label="参考直线 / reference")
ax.set_xlabel("theoretical quantile")
ax.set_ylabel("sample quantile")
ax.set_title("Q-Q 图 / Q-Q plot vs Normal")
ax.legend(loc="best", frameon=False)
plt.savefig("figures/qq_plot.png")
plt.savefig("figures/qq_plot.svg")
plt.close(fig)
""".strip()


_ROC_CURVE = """
import numpy as np
import matplotlib.pyplot as plt
from sklearn.metrics import roc_curve, auc

rng = np.random.default_rng(7)
y_true = rng.integers(0, 2, size=500)
y_score = 0.7 * y_true + 0.3 * rng.random(size=500)
fpr, tpr, _ = roc_curve(y_true, y_score)
roc_auc = auc(fpr, tpr)

fig, ax = plt.subplots(figsize=(5.6, 5.2))
ax.plot(fpr, tpr, linewidth=1.8, label=f"ROC (AUC = {roc_auc:.3f})")
ax.plot([0, 1], [0, 1], linestyle="--", color="gray", linewidth=1.0,
        label="random")
ax.set_xlabel("false positive rate")
ax.set_ylabel("true positive rate")
ax.set_title("ROC 曲线 / ROC curve")
ax.set_xlim(0, 1); ax.set_ylim(0, 1.01)
ax.legend(loc="lower right", frameon=False)
plt.savefig("figures/roc_curve.png")
plt.savefig("figures/roc_curve.svg")
plt.close(fig)
""".strip()


_CONFUSION_MATRIX = """
import numpy as np
import matplotlib.pyplot as plt

rng = np.random.default_rng(8)
y_true = rng.integers(0, 3, size=300)
y_pred = y_true.copy()
flip = rng.random(size=300) < 0.25
y_pred[flip] = rng.integers(0, 3, size=flip.sum())
classes = ["A", "B", "C"]
k = len(classes)
cm = np.zeros((k, k), dtype=int)
for t, p in zip(y_true, y_pred):
    cm[t, p] += 1
cm_norm = cm / cm.sum(axis=1, keepdims=True).clip(min=1)

fig, ax = plt.subplots(figsize=(5.6, 5.0))
im = ax.imshow(cm_norm, cmap="Blues", vmin=0.0, vmax=1.0)
ax.set_xticks(range(k)); ax.set_xticklabels(classes)
ax.set_yticks(range(k)); ax.set_yticklabels(classes)
ax.set_xlabel("predicted"); ax.set_ylabel("true")
for i in range(k):
    for j in range(k):
        label = f"{cm[i, j]}\\n({cm_norm[i, j]:.2f})"
        ax.text(j, i, label, ha="center", va="center", fontsize=9,
                color="white" if cm_norm[i, j] > 0.5 else "black")
ax.set_title("混淆矩阵 / confusion matrix (normalised rows)")
fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
plt.savefig("figures/confusion_matrix.png")
plt.savefig("figures/confusion_matrix.svg")
plt.close(fig)
""".strip()


_CONVERGENCE_CURVE = """
import numpy as np
import matplotlib.pyplot as plt

# Replace with your optimiser's per-iteration loss history.
iters = np.arange(1, 101)
loss_train = 2.0 * np.exp(-iters / 20.0) + 0.1 + 0.02 * np.random.default_rng(9).random(iters.size)
loss_val = 2.1 * np.exp(-iters / 22.0) + 0.15 + 0.03 * np.random.default_rng(10).random(iters.size)

fig, ax = plt.subplots(figsize=(6.4, 4.0))
ax.plot(iters, loss_train, linewidth=1.6, label="train")
ax.plot(iters, loss_val, linewidth=1.6, label="validation")
ax.set_yscale("log")
ax.set_xlabel("iteration / epoch")
ax.set_ylabel("loss (log scale)")
ax.set_title("收敛曲线 / convergence curve")
ax.legend(loc="best", frameon=False)
plt.savefig("figures/convergence_curve.png")
plt.savefig("figures/convergence_curve.svg")
plt.close(fig)
""".strip()


_TORNADO_SENSITIVITY = """
import numpy as np
import matplotlib.pyplot as plt

# Effect on objective when each parameter is swept from low to high, others fixed.
params = ["alpha", "beta", "gamma", "delta", "epsilon"]
low = np.array([-0.8, -0.3, -0.6, -0.1, -0.4])
high = np.array([+0.9, +0.2, +0.7, +0.5, +0.3])
order = np.argsort(high - low)  # biggest swing at top
params = [params[i] for i in order]
low = low[order]; high = high[order]

y = np.arange(len(params))
fig, ax = plt.subplots(figsize=(6.4, 4.0))
ax.barh(y, high, color="C0", alpha=0.8, label="high")
ax.barh(y, low, color="C3", alpha=0.8, label="low")
ax.axvline(0.0, color="black", linewidth=0.8)
ax.set_yticks(y); ax.set_yticklabels(params)
ax.set_xlabel("effect on objective")
ax.set_title("Tornado 灵敏度图 / tornado sensitivity")
ax.legend(loc="lower right", frameon=False)
plt.savefig("figures/tornado_sensitivity.png")
plt.savefig("figures/tornado_sensitivity.svg")
plt.close(fig)
""".strip()


_PARETO_FRONT = """
import numpy as np
import matplotlib.pyplot as plt

rng = np.random.default_rng(11)
# Two objectives to MINIMISE. Replace with your solutions.
n = 200
obj1 = rng.uniform(0, 10, size=n)
obj2 = 10 - 0.5 * obj1 + rng.normal(0, 1.2, size=n)

# Non-dominated sort: a point is on the front iff no other point dominates it.
idx = np.argsort(obj1)
o1_s = obj1[idx]; o2_s = obj2[idx]
front_mask = np.zeros(n, dtype=bool)
best = np.inf
for i, v in enumerate(o2_s):
    if v < best:
        front_mask[i] = True
        best = v
front_o1 = o1_s[front_mask]
front_o2 = o2_s[front_mask]

fig, ax = plt.subplots(figsize=(6.4, 4.4))
ax.scatter(obj1, obj2, s=14, alpha=0.5, color="gray", label="候选 / candidates")
ax.scatter(front_o1, front_o2, s=36, color="C3", label="Pareto front")
ax.plot(front_o1, front_o2, color="C3", linewidth=1.2)
ax.set_xlabel("objective 1 (minimise)")
ax.set_ylabel("objective 2 (minimise)")
ax.set_title("Pareto 前沿 / Pareto front")
ax.legend(loc="best", frameon=False)
plt.savefig("figures/pareto_front.png")
plt.savefig("figures/pareto_front.svg")
plt.close(fig)
""".strip()


_NETWORK_GRAPH = """
import numpy as np
import matplotlib.pyplot as plt

# Worker kernel does NOT have networkx installed — do a pure-matplotlib layout.
nodes = ["A", "B", "C", "D", "E", "F"]
edges = [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D"),
         ("C", "E"), ("D", "F"), ("E", "F")]

# Circular layout — fine for |V| <= ~20. For larger graphs, install networkx
# or pass precomputed (x, y) coordinates.
k = len(nodes)
theta = np.linspace(0, 2 * np.pi, k, endpoint=False)
pos = {n: (np.cos(t), np.sin(t)) for n, t in zip(nodes, theta)}

fig, ax = plt.subplots(figsize=(5.6, 5.6))
for u, v in edges:
    xu, yu = pos[u]; xv, yv = pos[v]
    ax.plot([xu, xv], [yu, yv], color="gray", linewidth=1.2, zorder=1)
xs = [pos[n][0] for n in nodes]; ys = [pos[n][1] for n in nodes]
ax.scatter(xs, ys, s=420, color="C0", zorder=2, edgecolor="black")
for n, (x, y) in pos.items():
    ax.text(x, y, n, ha="center", va="center", color="white",
            fontweight="bold", zorder=3)
ax.set_xlim(-1.4, 1.4); ax.set_ylim(-1.4, 1.4)
ax.set_aspect("equal"); ax.axis("off")
ax.set_title("关系网络图 / network graph")
plt.savefig("figures/network_graph.png")
plt.savefig("figures/network_graph.svg")
plt.close(fig)
""".strip()


# --------------------------------------------------------------- catalog table


_CATALOG: tuple[ChartType, ...] = (
    ChartType(
        id="line_plot",
        display_name="折线图 / Line plot",
        when_to_use=(
            "Show one or more continuous series over an ordered x-axis "
            "(time, iteration, parameter sweep)."
        ),
        when_not_to_use=(
            "Discrete unordered categories — use bar_grouped_stacked instead."
        ),
        keywords=("time series", "trend", "时间序列", "趋势", "line"),
        matplotlib_snippet=_LINE_PLOT,
        pitfalls=(
            "Too many overlapping series (>5) become unreadable — split the panel.",
            "Don't draw a line between unordered categories; use a bar chart.",
        ),
    ),
    ChartType(
        id="line_with_ci",
        display_name="带置信区间的折线图 / Line plot with CI band",
        when_to_use=(
            "A single trend whose uncertainty matters: bootstrap CI, prediction "
            "band, Monte-Carlo quantiles."
        ),
        when_not_to_use=(
            "When the band width is constant — a legend note is cheaper than a band."
        ),
        keywords=("confidence interval", "band", "置信区间", "bootstrap"),
        matplotlib_snippet=_LINE_WITH_CI,
        pitfalls=(
            "State the CI level (95%, 1σ, ...) in the legend or caption.",
            "If lower > upper at some x, fill_between silently inverts — validate.",
        ),
    ),
    ChartType(
        id="scatter_regression",
        display_name="散点 + 回归线 / Scatter with regression fit",
        when_to_use=(
            "Two continuous variables where you want to show both the raw cloud "
            "and an OLS fit (R² annotated)."
        ),
        when_not_to_use=(
            "Strongly non-linear relationships — fit curves separately and use "
            "contour_2d or a parametric model."
        ),
        keywords=("correlation", "regression", "scatter", "R2", "回归"),
        matplotlib_snippet=_SCATTER_REGRESSION,
        pitfalls=(
            "R² on a non-linear relationship is misleading — check residuals.",
            "Heavy overplot hides density; drop alpha or switch to hexbin.",
        ),
    ),
    ChartType(
        id="histogram_kde",
        display_name="直方图叠 KDE / Histogram with KDE overlay",
        when_to_use=(
            "Show the empirical distribution of one continuous variable, "
            "optionally across sub-groups."
        ),
        when_not_to_use=(
            "Discrete-integer data — use a bar chart of counts."
        ),
        keywords=("distribution", "density", "直方图", "KDE"),
        matplotlib_snippet=_HISTOGRAM_KDE,
        pitfalls=(
            "Bandwidth matters — Scott's rule is the default but may over-smooth.",
            "Bin width also matters — try several and pick what matches the data.",
        ),
    ),
    ChartType(
        id="boxplot_grouped",
        display_name="分组箱线图 / Grouped boxplot",
        when_to_use=(
            "Compare the distribution (median, IQR, outliers) of one variable "
            "across 2–10 groups."
        ),
        when_not_to_use=(
            "Small samples (n<10 per group) — use a strip/swarm plot of raw points."
        ),
        keywords=("boxplot", "groups", "IQR", "outliers", "分组"),
        matplotlib_snippet=_BOXPLOT_GROUPED,
        pitfalls=(
            "A box hides bimodality — if in doubt, prefer violinplot.",
            "Outliers are defined by 1.5·IQR by default; say so in the caption.",
        ),
    ),
    ChartType(
        id="violinplot",
        display_name="小提琴图 / Violin plot",
        when_to_use=(
            "Like boxplot but you also want to see the shape of each group's distribution."
        ),
        when_not_to_use=(
            "Tiny samples — the KDE inside each violin is unreliable below n≈30."
        ),
        keywords=("violin", "distribution", "shape", "分布"),
        matplotlib_snippet=_VIOLIN_PLOT,
        pitfalls=(
            "KDE bandwidth can under- or over-smooth — document what you used.",
            "Truncate the violin at data extrema or the tails look unreasonable.",
        ),
    ),
    ChartType(
        id="heatmap_correlation",
        display_name="相关系数热图 / Correlation heatmap",
        when_to_use=(
            "Show pairwise Pearson/Spearman correlations for 4–20 variables."
        ),
        when_not_to_use=(
            "More than ~20 variables — the labels collapse; cluster first or "
            "use a dendrogram."
        ),
        keywords=("correlation", "matrix", "相关", "heatmap"),
        matplotlib_snippet=_HEATMAP_CORRELATION,
        pitfalls=(
            "Use a diverging cmap (e.g. RdBu_r) centred at 0 — viridis misleads.",
            "Annotate cell values only when the matrix is small enough.",
        ),
    ),
    ChartType(
        id="heatmap_sensitivity",
        display_name="灵敏度热图 / Parameter sensitivity heatmap",
        when_to_use=(
            "Scan two parameters (α, β) and score an objective on the grid — "
            "show the response surface as colour."
        ),
        when_not_to_use=(
            "Three or more parameters — use small multiples or slicing, or "
            "surface_3d for a single slice."
        ),
        keywords=("sensitivity", "parameter", "grid", "灵敏度", "扫描"),
        matplotlib_snippet=_HEATMAP_SENSITIVITY,
        pitfalls=(
            "Coarse grids lie — refine near the argmax.",
            "Mark the argmax explicitly (a star glyph) so the reader sees it.",
        ),
    ),
    ChartType(
        id="contour_2d",
        display_name="等高线图 / 2D contour plot",
        when_to_use=(
            "Visualise f(x, y) over a region; especially good for optimisation "
            "landscapes like Rosenbrock, Himmelblau."
        ),
        when_not_to_use=(
            "The function has discontinuities or narrow spikes — contours look "
            "messy; prefer a heatmap."
        ),
        keywords=("contour", "level set", "objective landscape", "等高线"),
        matplotlib_snippet=_CONTOUR_2D,
        pitfalls=(
            "Linear levels hide wide dynamic range — use logspace.",
            "Without clabel / colourbar the reader can't tell level values.",
        ),
    ),
    ChartType(
        id="surface_3d",
        display_name="三维响应面 / 3D surface",
        when_to_use=(
            "Dramatic visualisation of a 2D response surface for presentations."
        ),
        when_not_to_use=(
            "Precise value reading — occlusion makes 3D plots unreliable; "
            "contour_2d is usually better for a paper figure."
        ),
        keywords=("3d", "surface", "response surface", "三维"),
        matplotlib_snippet=_SURFACE_3D,
        pitfalls=(
            "Viewing angle changes the message — pick one and state it.",
            "Import `mpl_toolkits.mplot3d` (noqa: F401) before `projection='3d'`.",
        ),
    ),
    ChartType(
        id="bar_grouped_stacked",
        display_name="分组 / 堆叠柱状图 / Grouped or stacked bars",
        when_to_use=(
            "Compare a small number of categories (<12) across 2–4 series."
        ),
        when_not_to_use=(
            "Continuous x-axis — that is a line plot's job."
        ),
        keywords=("bar chart", "categories", "grouped", "stacked", "柱状"),
        matplotlib_snippet=_BAR_GROUPED,
        pitfalls=(
            "Stacked bars hide individual series values — grouped is often clearer.",
            "Sort categories by value (or by meaningful order) — alphabetical is rarely useful.",
        ),
    ),
    ChartType(
        id="radar_chart",
        display_name="雷达图 / Radar (spider) chart",
        when_to_use=(
            "Compare ≤4 items on 4–8 already-normalised metrics."
        ),
        when_not_to_use=(
            "The metrics live on different scales or have natural ordering — "
            "radar charts distort both. A parallel-coordinates plot is safer."
        ),
        keywords=("radar", "spider", "multi-criteria", "多维评分"),
        matplotlib_snippet=_RADAR_CHART,
        pitfalls=(
            "Axis order changes the perceived area — pick a deliberate order.",
            "Normalise metrics to [0, 1] before plotting.",
        ),
    ),
    ChartType(
        id="residual_plot",
        display_name="残差图 / Residual plot",
        when_to_use=(
            "Diagnose a regression: residuals vs fitted should look like a "
            "random band around 0."
        ),
        when_not_to_use=(
            "Classification models — use a confusion matrix or ROC curve."
        ),
        keywords=("residual", "regression diagnostic", "残差"),
        matplotlib_snippet=_RESIDUAL_PLOT,
        pitfalls=(
            "Patterns (funnel, curve) in residuals = model misspecification — say what you saw.",
            "Plot residuals vs fitted, not vs x — trends in fitted values are what matter.",
        ),
    ),
    ChartType(
        id="qq_plot",
        display_name="Q-Q 图 / Q-Q plot (normality)",
        when_to_use=(
            "Check whether residuals (or any sample) are approximately normal — "
            "points should fall on the reference line."
        ),
        when_not_to_use=(
            "Discrete data — quantiles are staircase-shaped and mislead."
        ),
        keywords=("qq", "normality", "quantile", "正态性"),
        matplotlib_snippet=_QQ_PLOT,
        pitfalls=(
            "Heavy tails bend the ends of the plot — report the deviation.",
            "Sample size matters: n<30 Q-Q plots are noisy.",
        ),
    ),
    ChartType(
        id="roc_curve",
        display_name="ROC 曲线 / ROC curve with AUC",
        when_to_use=(
            "Binary classifier quality independent of the decision threshold."
        ),
        when_not_to_use=(
            "Highly imbalanced classes — prefer Precision-Recall; AUC is too "
            "optimistic there."
        ),
        keywords=("roc", "auc", "classifier", "binary"),
        matplotlib_snippet=_ROC_CURVE,
        pitfalls=(
            "Put AUC in the legend — the curve alone is hard to read.",
            "Plot the y=x chance line for reference.",
        ),
    ),
    ChartType(
        id="confusion_matrix",
        display_name="混淆矩阵 / Confusion matrix",
        when_to_use=(
            "Classification error breakdown per class, typically normalised by row."
        ),
        when_not_to_use=(
            "Regression tasks — use a residual plot."
        ),
        keywords=("confusion", "classification", "error breakdown", "混淆矩阵"),
        matplotlib_snippet=_CONFUSION_MATRIX,
        pitfalls=(
            "Always annotate whether counts or row-normalised rates are shown.",
            "Row-normalise so imbalanced classes are comparable.",
        ),
    ),
    ChartType(
        id="convergence_curve",
        display_name="收敛曲线 / Convergence / training curve",
        when_to_use=(
            "Optimiser loss vs iteration, training and validation together — "
            "shows if you converged and whether you overfit."
        ),
        when_not_to_use=(
            "Single-shot non-iterative methods (closed-form OLS, LP solvers)."
        ),
        keywords=("convergence", "loss", "epoch", "training", "收敛"),
        matplotlib_snippet=_CONVERGENCE_CURVE,
        pitfalls=(
            "Use log scale for loss when it spans orders of magnitude.",
            "Always plot validation alongside training; training-only plots hide overfitting.",
        ),
    ),
    ChartType(
        id="tornado_sensitivity",
        display_name="Tornado 灵敏度图 / Tornado sensitivity",
        when_to_use=(
            "Rank the per-parameter one-at-a-time effect on the objective. "
            "Largest-swing parameter at the top."
        ),
        when_not_to_use=(
            "Strong parameter interactions — OAT sensitivity misses them; "
            "use Sobol indices instead."
        ),
        keywords=("tornado", "sensitivity", "importance", "one at a time"),
        matplotlib_snippet=_TORNADO_SENSITIVITY,
        pitfalls=(
            "State the low/high perturbation magnitude — ±1σ? ±10%? ±full range?",
            "OAT ignores interactions; caveat the result in the caption.",
        ),
    ),
    ChartType(
        id="pareto_front",
        display_name="Pareto 前沿 / Pareto front",
        when_to_use=(
            "Two conflicting objectives you want to minimise — plot all "
            "candidates and highlight the non-dominated set."
        ),
        when_not_to_use=(
            ">3 objectives — 2D projections mislead. Use parallel coordinates."
        ),
        keywords=("pareto", "multi-objective", "tradeoff", "多目标"),
        matplotlib_snippet=_PARETO_FRONT,
        pitfalls=(
            "Say which direction is 'better' for each axis — minimise or maximise.",
            "The non-dominated sort assumes minimisation on both axes; flip sign if needed.",
        ),
    ),
    ChartType(
        id="network_graph",
        display_name="关系网络图 / Network graph",
        when_to_use=(
            "Small (|V|≤20) graph with nodes and edges — flow networks, "
            "dependency graphs, social ties."
        ),
        when_not_to_use=(
            "Dense graphs with hundreds of nodes — use an adjacency heatmap."
        ),
        keywords=("network", "graph", "nodes", "edges", "关系图"),
        matplotlib_snippet=_NETWORK_GRAPH,
        pitfalls=(
            "networkx is NOT installed in the worker kernel; use a manual layout.",
            "Circular layout hides clusters — for structured graphs compute positions by hand.",
        ),
    ),
)


_BY_ID: dict[str, ChartType] = {c.id: c for c in _CATALOG}


def all_chart_types() -> tuple[ChartType, ...]:
    """Return every registered ChartType, in declaration order."""
    return _CATALOG


def get(chart_id: str) -> ChartType:
    """Look up a ChartType by id. Raises KeyError if unknown."""
    try:
        return _BY_ID[chart_id]
    except KeyError as e:
        raise KeyError(
            f"unknown chart_type_id {chart_id!r}; "
            f"known: {sorted(_BY_ID)}"
        ) from e


def ids() -> tuple[str, ...]:
    """All known chart-type ids, in declaration order."""
    return tuple(c.id for c in _CATALOG)


def render_index_markdown(chart_types: Iterable[ChartType] | None = None) -> str:
    """Render a compact markdown table for prompt injection.

    Columns: `id | display_name | when_to_use | primary pitfall`. The table
    is intentionally small (one pitfall per row) to keep prompt tokens under
    ~2k even with all 20 entries.
    """
    items = tuple(chart_types) if chart_types is not None else _CATALOG
    lines = [
        "| id | name | when to use | primary pitfall |",
        "| --- | --- | --- | --- |",
    ]
    for c in items:
        primary_pitfall = c.pitfalls[0] if c.pitfalls else ""
        # Pipes inside cells would break the table — escape defensively.
        name = c.display_name.replace("|", "\\|")
        when = c.when_to_use.replace("|", "\\|").replace("\n", " ").strip()
        pit = primary_pitfall.replace("|", "\\|").replace("\n", " ").strip()
        lines.append(f"| `{c.id}` | {name} | {when} | {pit} |")
    return "\n".join(lines)


__all__ = [
    "ChartType",
    "all_chart_types",
    "get",
    "ids",
    "render_index_markdown",
]
