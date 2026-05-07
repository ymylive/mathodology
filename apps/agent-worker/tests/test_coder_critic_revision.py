from __future__ import annotations

from agent_worker.agents import CoderAgent
from agent_worker.pipeline import CriticPolicy, _review_and_maybe_rerun_coder
from mm_contracts import (
    AnalyzerOutput,
    ApproachSketch,
    CellExecution,
    CoderOutput,
    CritiqueFinding,
    CritiqueReport,
    ModelSpec,
    ProblemInput,
)


class _FakeCritic:
    def __init__(self, reports: list[CritiqueReport]) -> None:
        self.reports = reports
        self.calls = 0

    async def review(self, **_: object) -> CritiqueReport:
        self.calls += 1
        return self.reports.pop(0)


class _FakeCoder:
    def __init__(self, output: CoderOutput | list[CoderOutput]) -> None:
        self.outputs = output if isinstance(output, list) else [output]
        self.max_iterations_seen: list[int] = []
        self.calls = 0

    async def run(
        self,
        problem: ProblemInput,
        analysis: AnalyzerOutput,
        spec: ModelSpec,
        max_iterations: int = 7,
    ) -> CoderOutput:
        self.calls += 1
        self.max_iterations_seen.append(max_iterations)
        return self.outputs[min(self.calls - 1, len(self.outputs) - 1)]


def test_coder_builds_critique_revision_problem_text() -> None:
    original = CoderOutput(
        cells=[
            CellExecution(
                index=0,
                source="print('no validation')",
                stdout="no validation\n",
            )
        ],
        final_summary="Computed a baseline only.",
        notebook_path="/tmp/run/notebook.ipynb",
    )
    critique = CritiqueReport(
        target_agent="coder",
        target_schema="CoderOutput",
        passed=False,
        score=0.45,
        summary="Missing sensitivity analysis.",
        findings=[
            CritiqueFinding(
                severity="major",
                area="validation",
                message="No sensitivity or validation evidence was produced.",
                evidence="Only one baseline cell appears in CoderOutput.",
                required_change="Add sensitivity analysis and report quantitative results.",
            )
        ],
        required_changes=["Add sensitivity analysis."],
    )

    problem = ProblemInput(problem_text="Optimize allocation under uncertain demand.")
    analysis = AnalyzerOutput(
        restated_problem="Optimize allocation under uncertain demand.",
        sub_questions=["Find allocation", "Test sensitivity"],
        proposed_approaches=[
            ApproachSketch(name="LP", rationale="Fits allocation", methods=["LP"])
        ],
    )
    spec = ModelSpec(
        chosen_approach="linear programming",
        rationale="Fits constrained allocation.",
        algorithm_outline=["Solve baseline", "Run sensitivity"],
        validation_strategy="Sensitivity sweep over demand.",
    )

    revised_problem = CoderAgent.build_revision_problem(
        problem=problem,
        analysis=analysis,
        spec=spec,
        original_output=original,
        critique=critique,
    )

    assert "Critic requested one corrective Coder pass" in revised_problem.problem_text
    assert "Missing sensitivity analysis" in revised_problem.problem_text
    assert "Computed a baseline only." in revised_problem.problem_text
    assert "Optimize allocation under uncertain demand." in revised_problem.problem_text


async def test_coder_critic_rerun_uses_policy_iteration_cap() -> None:
    original = CoderOutput(
        cells=[
            CellExecution(
                index=0,
                source="print('baseline')",
                stdout="baseline\n",
            )
        ],
        final_summary="Computed a baseline only.",
        notebook_path="/tmp/run/notebook.ipynb",
    )
    revised = CoderOutput(
        cells=[
            CellExecution(
                index=0,
                source="print('baseline and sensitivity')",
                stdout="baseline and sensitivity\n",
            )
        ],
        final_summary="Computed baseline and sensitivity.",
        notebook_path="/tmp/run/notebook.ipynb",
    )
    first_report = CritiqueReport(
        target_agent="coder",
        target_schema="CoderOutput",
        passed=False,
        score=0.45,
        summary="Missing sensitivity analysis.",
        findings=[
            CritiqueFinding(
                severity="major",
                area="validation",
                message="No sensitivity evidence was produced.",
                evidence="Only one baseline cell appears.",
                required_change="Add sensitivity analysis.",
            ),
            CritiqueFinding(
                severity="major",
                area="figures",
                message="No figures were registered.",
                evidence="Figure list is empty.",
                required_change="Register at least one figure.",
            ),
        ],
        required_changes=["Add sensitivity analysis."],
    )
    followup_report = CritiqueReport(
        target_agent="coder",
        target_schema="CoderOutput",
        passed=True,
        score=0.92,
        summary="Coder output now includes validation.",
        findings=[],
        required_changes=[],
    )
    problem = ProblemInput(problem_text="Optimize allocation under uncertain demand.")
    analysis = AnalyzerOutput(
        restated_problem="Optimize allocation under uncertain demand.",
        sub_questions=["Find allocation", "Test sensitivity"],
        proposed_approaches=[
            ApproachSketch(name="LP", rationale="Fits allocation", methods=["LP"])
        ],
    )
    spec = ModelSpec(
        chosen_approach="linear programming",
        rationale="Fits constrained allocation.",
        algorithm_outline=["Solve baseline", "Run sensitivity"],
        validation_strategy="Sensitivity sweep over demand.",
    )
    critic = _FakeCritic([first_report, followup_report])
    coder = _FakeCoder(revised)

    result = await _review_and_maybe_rerun_coder(
        critic=critic,  # type: ignore[arg-type]
        coder=coder,  # type: ignore[arg-type]
        problem=problem,
        analysis=analysis,
        spec=spec,
        coder_out=original,
        policy=CriticPolicy(coder_revision_iterations=2),
    )

    assert result is revised
    assert coder.max_iterations_seen == [2]
    assert critic.calls == 2


async def test_coder_critic_rerun_allows_two_revision_rounds() -> None:
    original = CoderOutput(
        cells=[
            CellExecution(index=0, source="print('baseline')", stdout="baseline\n")
        ],
        final_summary="Computed a baseline only.",
        notebook_path="/tmp/run/notebook.ipynb",
    )
    first_revision = CoderOutput(
        cells=[
            CellExecution(
                index=0,
                source="print('baseline and sensitivity')",
                stdout="baseline and sensitivity\n",
            )
        ],
        final_summary="Computed baseline and sensitivity.",
        notebook_path="/tmp/run/notebook.ipynb",
    )
    second_revision = CoderOutput(
        cells=[
            CellExecution(
                index=0,
                source="print('baseline sensitivity figure')",
                stdout="baseline sensitivity figure\n",
            )
        ],
        final_summary="Computed baseline, sensitivity, and a figure.",
        notebook_path="/tmp/run/notebook.ipynb",
    )
    first_report = CritiqueReport(
        target_agent="coder",
        target_schema="CoderOutput",
        passed=False,
        score=0.45,
        summary="Missing sensitivity analysis.",
        findings=[
            CritiqueFinding(
                severity="major",
                area="validation",
                message="No sensitivity evidence was produced.",
                evidence="Only one baseline cell appears.",
                required_change="Add sensitivity analysis.",
            ),
            CritiqueFinding(
                severity="major",
                area="figures",
                message="No figures were registered.",
                evidence="Figure list is empty.",
                required_change="Register at least one figure.",
            ),
        ],
        required_changes=["Add sensitivity analysis and figures."],
    )
    second_report = CritiqueReport(
        target_agent="coder",
        target_schema="CoderOutput",
        passed=True,
        score=0.79,
        summary="Sensitivity exists but score is still below threshold.",
        findings=[],
        required_changes=[],
    )
    final_report = CritiqueReport(
        target_agent="coder",
        target_schema="CoderOutput",
        passed=True,
        score=0.92,
        summary="Coder output now includes validation and figures.",
        findings=[],
        required_changes=[],
    )
    problem = ProblemInput(problem_text="Optimize allocation under uncertain demand.")
    analysis = AnalyzerOutput(
        restated_problem="Optimize allocation under uncertain demand.",
        sub_questions=["Find allocation", "Test sensitivity"],
        proposed_approaches=[
            ApproachSketch(name="LP", rationale="Fits allocation", methods=["LP"])
        ],
    )
    spec = ModelSpec(
        chosen_approach="linear programming",
        rationale="Fits constrained allocation.",
        algorithm_outline=["Solve baseline", "Run sensitivity"],
        validation_strategy="Sensitivity sweep over demand.",
    )
    critic = _FakeCritic([first_report, second_report, final_report])
    coder = _FakeCoder([first_revision, second_revision])

    result = await _review_and_maybe_rerun_coder(
        critic=critic,  # type: ignore[arg-type]
        coder=coder,  # type: ignore[arg-type]
        problem=problem,
        analysis=analysis,
        spec=spec,
        coder_out=original,
        policy=CriticPolicy(max_revision_rounds=2, coder_revision_iterations=2),
    )

    assert result is second_revision
    assert coder.max_iterations_seen == [2, 2]
    assert critic.calls == 3
