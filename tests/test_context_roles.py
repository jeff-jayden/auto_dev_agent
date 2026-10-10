"""验证需求核心实现收敛、辅助上下文保留与技术方案不自动扩权。"""

from unittest.mock import patch

from agents import RequirementPlanningAgent
from code_intelligence.indexer import RepositoryCodeIndex
from domain.models import RepositoryAnalysis, RepositoryContextPack, TechnicalPlan


def test_named_page_is_only_core_and_context_is_retained(tmp_path):
    files = {
        "src/pages/YourVideos.js": "export function YourVideos() { return null; }",
        "src/App.js": "import { YourVideos } from './pages/YourVideos';\nexport function App() { return YourVideos(); }",
        "src/App.css": ".page { color: red; }",
        "src/index.js": "import App from './App';\nApp();",
        "src/reportWebVitals.js": "export function reportWebVitals() {}",
        "src/pages/YourVideos.css": ".tabs { color: blue; }",
        "src/App.test.js": "import App from './App';\ntest('page', () => App());",
    }
    for path, content in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    engine = RepositoryCodeIndex()
    index, mode, changed = engine.build(tmp_path, list(files), "base")
    hits = [{"path": path, "score": 1, "reasons": []} for path in files]
    with patch.object(engine, "rank_files", return_value=hits):
        context, _ = engine.context_pack(tmp_path, index, "在 Your Videos 页面实现四个 tab 切换", ["src/index.js"], mode, changed)
    assert context.primary_files == ["src/pages/YourVideos.js"]
    assert "src/App.js" in context.dependency_files
    assert "src/pages/YourVideos.css" in context.dependency_files
    assert "src/App.test.js" in context.test_files
    assert not set(context.primary_files) & set(context.dependency_files)
    assert next(item for item in context.files if item.path == "src/App.js").role == "dependency"


def test_explicit_multiple_files_and_style_only_request():
    engine = RepositoryCodeIndex()
    records = {path: {"symbols": []} for path in ["src/App.css", "src/App.js", "src/pages/YourVideos.js"]}
    assert engine._primary_files(records, list(records), "修改页面背景色") == ["src/App.css"]
    assert engine._primary_files(records, list(records), "修改 src/App.js 和 src/App.css") == ["src/App.css", "src/App.js"]
    assert engine._primary_files(records, list(records), "给 YourVideos 增加切换") == ["src/pages/YourVideos.js"]


def test_generic_plan_language_does_not_expand_write_scope():
    repository = RepositoryAnalysis(language="javascript", framework="react", file_count=4,
        entrypoints=["src/index.js"], context_pack=RepositoryContextPack(
            primary_files=["src/pages/YourVideos.js"], dependency_files=["src/App.js", "src/index.js"], test_files=[]))
    plan = TechnicalPlan(approach="先检查入口和调用链再修改", affected_files=["src/pages/YourVideos.js"],
        implementation_steps=["检查入口"], test_plan=[], risks=[])
    assert RequirementPlanningAgent.ensure_dependency_scope(plan, repository, "Your Videos 页面切换 tab") == []
    assert plan.affected_files == ["src/pages/YourVideos.js"]
    assert RequirementPlanningAgent.ensure_dependency_scope(plan, repository, "更新页面导入路径")
