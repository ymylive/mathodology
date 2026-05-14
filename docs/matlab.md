---
name: matlab
description: MATLAB / Octave execution backend for the Coder agent — when to choose it over Python, the harness contract, and worked examples for MCM/CUMCM problems.
when_to_use:
  - "the problem statement names MATLAB or Simulink explicitly"
  - "constrained nonlinear optimization with fmincon / intlinprog / quadprog"
  - "control systems work (tf, ss, bode, step, lqr, root-locus)"
  - "classical signal / wavelet processing (filter, fft, wavedec, cwt)"
  - "mixed-integer programming with rich constraints"
  - "symbolic math when SymPy stalls (syms, solve, simplify)"
  - "matching a winner-paper baseline written in MATLAB (ode45, eigs)"
allowed-tools:
  - run_matlab  # routes to MatlabSession (matlab -batch or octave --no-gui)
  - run_python  # the default; documented here for symmetry
arguments:
  - name: language
    type: enum
    values: [python, matlab]
    default: python
context: inline  # rules small enough to inline in the Coder prompt
model: cornna/gpt-5.5  # inherits from run-level reasoning_effort
effort: high
hooks:
  pre_run:
    - assert_paper_source_linkage_comment_present
    - assert_headless_render_preamble_when_plotting
  post_run:
    - diff_figures_dir_and_emit_kernel_figure_events
---

# MATLAB Execution Capability

Specification for the Coder agent's MATLAB backend. Audience: developers extending the worker, and future Claude sessions deciding when to emit MATLAB instead of Python.

The YAML frontmatter above follows the [Claude Code SKILL.md][skill-md-ref] schema so a discovery layer can list this capability at <1% of the system prompt and load the body on demand. The actual Coder prompt enforcement lives in `apps/agent-worker/src/agent_worker/prompts/coder/v1.toml § MATLAB`.

[skill-md-ref]: https://github.com/ymylive/claude-code-sourcemap/blob/main/restored-src/src/skills/bundled/skillify.ts

## Overview

Some MCM/CUMCM problems are easier to win with MATLAB toolboxes than with the Python scientific stack: constrained optimization (`fmincon`, `intlinprog`), control systems (`tf`/`ss`/`bode`), signal/wavelet decomposition, and symbolic algebra when SymPy stalls. To keep parity with the existing Python `KernelSession` while giving Coder access to those toolboxes, we added a per-turn `language` field on the Coder JSON directive. Coder picks the language; the worker dispatches to the right session.

Production runtime is `matlab -batch` against an installed MATLAB R2023b+. Dev and CI use GNU `octave --no-gui`, which covers ~99% of the syntax we emit. When neither binary is on `PATH`, the session falls back to `NoOpBackend`, which returns a structured error `CellExecution` (no toolbox installed) so the rest of the pipeline keeps running and surfaces a clean failure to the Critic.

## Architecture

```
CoderAgent.run()
  |-- directive.language == "python" -> KernelSession (Jupyter) -- existing
  +-- directive.language == "matlab" -> MatlabSession -> backend
                                                       |-- MatlabBatchBackend
                                                       |-- OctaveCliBackend
                                                       +-- NoOpBackend
```

State is NOT shared across the two sessions. Cross-language data exchange goes through `.mat` files in `runs/<run_id>/matlab/`. Figures land in `runs/<run_id>/figures/` so the export pipeline picks them up uniformly.

Anticipated file layout:

- `apps/agent-worker/src/agent_worker/matlab/session.py` — `MatlabSession` class
- `apps/agent-worker/src/agent_worker/matlab/backends.py` — three backend implementations
- `apps/agent-worker/src/agent_worker/agents/coder.py` — directive routing on `language`
- `apps/agent-worker/src/agent_worker/prompts/coder/v1.toml` — prompt rules describing the contract below

## Skill catalog — when to pick MATLAB

Pick MATLAB when the problem maps to one of:

- Constrained nonlinear optimization (`fmincon`, `intlinprog`, `quadprog`) — e.g. 2022 CUMCM A 蔬菜类商品的自动定价与补货决策.
- Control systems (`tf`, `ss`, `bode`, `step`, `lqr`) — common in 国赛工业控制 and 2021 MCM C-style supply chain dynamics.
- Signal / wavelet processing (`filter`, `fft`, `wavedec`, `cwt`) — e.g. 2023 CUMCM C 信号去噪 variants.
- Mixed-integer programming with rich constraints where PuLP/CVXPY get clunky — e.g. 2020 CUMCM B 穿越沙漠.
- Symbolic math when SymPy stalls or returns unsimplified expressions (`syms`, `solve`, `simplify`).
- ODE integration (`ode45`, `ode23s`, `ode15s`) when matching a known winning paper baseline written in MATLAB.
- Sparse / structured eigensolvers (`eigs`, `chol`) where Octave outpaces `scipy.linalg` on >10k-dim systems.
- Image processing on grayscale arrays when a MATLAB demo paper is the cited prior art.

Pick Python (default) when:

- Data wrangling, CSV/Excel ingest, joins (pandas).
- Deep learning, gradient-based training (PyTorch / JAX).
- Statistical modeling beyond OLS (statsmodels, scikit-learn).
- Geospatial, geometry, or anything needing `shapely`/`geopandas`.
- Web scraping or any I/O against the search/MCP layer.
- Final paper figure rendering — matplotlib + `styled_figure` is the canonical path.

## Harness contract — what the Coder agent MUST do

Five rules the Coder commits to whenever it emits MATLAB:

1. **Headless rendering.** Every cell that produces a figure starts with:
   ```matlab
   set(0, 'DefaultFigureVisible', 'off');
   graphics_toolkit('gnuplot');  % Octave-compatible
   ```
2. **Dual-format figure save.** Save both PNG (preview) and SVG (LaTeX export) under `figures/<id>.png` and `figures/<id>.svg`, then register them in the directive's `figures_saved` list — same contract as the Python backend.
3. **Paper-source linkage.** First line of every cell is a comment of the form:
   ```matlab
   % --- Section 4.2 of paper: 库存补货模型求解 ---
   ```
4. **Reproducible seeding.** Any randomness uses `rng(20240202)` (MATLAB) or `rand('state', 20240202); randn('state', 20240202)` (Octave) at the top of the cell.
5. **Cross-cell state via `.mat` only.** Each MATLAB cell is a fresh `matlab -batch` invocation; no globals, no workspace persists. Use `save('runs/<id>/matlab/foo.mat', 'var1', 'var2')` and `load(...)` to bridge cells.

## Backend selection precedence

Resolved once per `MatlabSession`:

1. `MM_MATLAB_BACKEND` env var — if set to `matlab` or `octave`, force that backend (errors if the chosen binary is missing).
2. Else `shutil.which("matlab")` → `MatlabBatchBackend`.
3. Else `shutil.which("octave")` → `OctaveCliBackend`.
4. Else `NoOpBackend` — returns a `CellExecution` with `status="error"` and a clear message; the run continues.

## Worked example — 3-turn cross-language workflow

Turn 1 (`language: "python"`) — load, clean, hand off:

```python
import pandas as pd
from scipy.io import savemat

df = pd.read_csv("data/demand.csv").dropna()
savemat("runs/abc/matlab/data.mat",
        {"demand": df["qty"].values, "price": df["price"].values})
```

Turn 2 (`language: "matlab"`) — solve:

```matlab
% --- Section 3.1 of paper: 非线性补货优化 ---
set(0, 'DefaultFigureVisible', 'off');
rng(20240202);
load('runs/abc/matlab/data.mat');
f = @(x) -sum(price .* x) + 0.1 * sum(x.^2);
x0 = ones(size(demand));
[x_opt, fval] = fmincon(f, x0, [], [], [], [], zeros(size(x0)), demand);
save('runs/abc/matlab/results.mat', 'x_opt', 'fval');
```

Turn 3 (`language: "python"`) — load results, render the final figure:

```python
from scipy.io import loadmat
import matplotlib.pyplot as plt

res = loadmat("runs/abc/matlab/results.mat")
plt.plot(res["x_opt"].ravel())
plt.savefig("runs/abc/figures/replenishment.svg")
```

## Testing

- `tests/test_matlab_session.py` — 8 unit tests with `subprocess` mocked; covers backend resolution, env override, figure registration, seeding injection, error surfacing.
- One integration test auto-skips via `pytest.importorskip`-style guard when `shutil.which("octave")` is `None`.
- Local dev install: `brew install octave` (~700 MB). Skipping is fine — `NoOpBackend` keeps the pipeline runnable end-to-end.

## Known limitations

- Octave is ~99% MATLAB-compatible but lacks Simulink, App Designer, parts of Statistics & ML Toolbox, `parfor` (runs serially), and several Image Processing Toolbox functions. The Coder prompt enumerates the safe subset.
- `matlab -batch` has ~10–30 s startup per invocation; do not pre-emptively split work into many tiny cells. One MATLAB cell per logical step is the right granularity.
- `.mat` handoff files are V7 by default; both `scipy.io.loadmat` and MATLAB/Octave read them without flags. Avoid V7.3 (HDF5) unless arrays exceed 2 GB.
