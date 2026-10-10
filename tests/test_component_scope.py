from pathlib import Path
from unittest.mock import Mock

import pytest

from agents.code_development_agent import CodeDevelopmentAgent
from agents.requirement_planning_agent import RequirementPlanningAgent, RequirementPlanningResponse
from domain.models import (NewComponentRequest, RepositoryAnalysis, RequirementAnalysis,
                           TechnicalPlan, Task, ReplacementDevelopmentProposal, TextReplacement)
from tools.component_scope import approve_component_requests, validate_component_requests
from tools.component_scope import approve_feedback_components


def request(path="src/pages/YourVideos.js"):
    return NewComponentRequest(path=path, reason="Extract Your Videos page component")


@pytest.mark.parametrize("path", ["src/**", "src/pages/*.js", "../outside.js",
    "src/pages/../../outside.js", "src/App.js", "src/pages/.env", "C:/escape.js"])
def test_invalid_component_paths_are_rejected(path):
    with pytest.raises(ValueError):
        validate_component_requests([request(path)])


def test_atomic_validation_and_existing_file_rejection(tmp_path):
    task = Task(id="t", title="page", requirement="extract page", status="developing",
                technical_plan=plan())
    with pytest.raises(ValueError):
        approve_component_requests(task, [request(), request("src/**")], tmp_path, "developer")
    assert task.technical_plan.affected_files == ["src/App.js"]
    target = tmp_path / "src/pages/YourVideos.js"
    target.parent.mkdir(parents=True)
    target.write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError):
        validate_component_requests([request()], tmp_path)


def plan():
    return TechnicalPlan(approach="extract component", affected_files=["src/App.js"],
                         implementation_steps=["create and import"], test_plan=["test"], risks=[])


def test_explicit_feedback_grants_only_requested_component(tmp_path):
    task = Task(id="t", title="page", requirement="page", status="developing",
                workspace=str(tmp_path), technical_plan=plan())
    assert approve_feedback_components(task, "Create src/pages/YourVideos.js", "user") == ["src/pages/YourVideos.js"]
    assert task.technical_plan.affected_files == ["src/App.js", "src/pages/YourVideos.js"]
    with pytest.raises(ValueError):
        approve_feedback_components(task, "Create src/config/secret.js", "user")
    assert "src/config/secret.js" not in task.technical_plan.affected_files


def test_initial_plan_accepts_valid_new_component_request():
    gateway = Mock(enabled=True)
    gateway.build_langchain_model = None
    proposed = plan()
    proposed.new_files = [request()]
    gateway.generate_structured.return_value = RequirementPlanningResponse(
        analysis=RequirementAnalysis(summary="page", user_story="view page",
                                     acceptance_criteria=["visible"], assumptions=[]), plan=proposed)
    repository = RepositoryAnalysis(language="javascript", framework="react", file_count=1,
                                    important_files=["src/App.js"])
    _, result = RequirementPlanningAgent(gateway).plan("page", "extract YourVideos", repository)
    assert result.affected_files == ["src/App.js", "src/pages/YourVideos.js"]
    assert result.new_files[0].path == "src/pages/YourVideos.js"


def test_developer_creates_component_and_import_in_same_proposal(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    app = tmp_path / "src/App.js"
    app.parent.mkdir()
    app.write_text("export const App = () => null;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "baseline"], check=True)
    gateway = Mock(enabled=True, supports_context_tool_loop=False)
    gateway.generate_structured.return_value = ReplacementDevelopmentProposal(
        new_files=[request()], replacements=[
            TextReplacement(path="src/pages/YourVideos.js", search="",
                            replace="export default function YourVideos() { return null; }\n"),
            TextReplacement(path="src/App.js", search=app.read_text(encoding="utf-8"),
                            replace="import YourVideos from './pages/YourVideos';\nexport const App = YourVideos;\n"),
        ])
    task = Task(id="t", title="page", requirement="extract component", status="developing",
                analysis=RequirementAnalysis(summary="page", user_story="view", acceptance_criteria=["page"], assumptions=[]),
                technical_plan=plan(), repository_analysis=RepositoryAnalysis(
                    language="javascript", file_count=1, important_files=["src/App.js"],
                    test_command='python -c "print(1)"'))
    outcome = CodeDevelopmentAgent(gateway).run(task, tmp_path)
    assert outcome.kind == "success", outcome.error
    assert (tmp_path / "src/pages/YourVideos.js").is_file()
    assert "import YourVideos" in app.read_text(encoding="utf-8")
    assert task.metadata["scope_expansion_requests"][0]["approved_files"] == ["src/pages/YourVideos.js"]
