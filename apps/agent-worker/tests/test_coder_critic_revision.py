from __future__ import annotations

from agent_worker.agents import CoderAgent
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
