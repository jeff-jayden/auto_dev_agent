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


def test_context_ranking_expands_dependency_and_prefers_definition_role():
    engine = RepositoryCodeIndex()
    records = {
        "src/features/watch/WatchPage.js": {
            "symbols": ["WatchPage"], "imports": ["src/features/watch/Reactions.js"],
            "referenced_by": [], "search_text": "render reactions",
        },
        "src/features/watch/Player.js": {
            "symbols": ["Player"], "imports": [], "referenced_by": [], "search_text": "video player",
        },
        "src/features/watch/Reactions.js": {
            "symbols": ["Reactions"], "imports": [],
            "referenced_by": ["src/features/watch/WatchPage.js"], "search_text": "like dislike",
        },
    }
    raw = [
        {"path": "src/features/watch/WatchPage.js", "score": 1.0, "reasons": [], "chunks": []},
        {"path": "src/features/watch/Player.js", "score": 0.9, "reasons": [], "chunks": []},
    ]

    ranked = engine.rank_context_files(
        {"files": records}, "实现点赞与不喜欢互斥操作", limit=3, ranked_hits=raw,
    )

    assert ranked[0]["path"] == "src/features/watch/Reactions.js"
    assert ranked[0]["context_role"] == "primary"
    assert any("扩展的依赖" in reason for reason in ranked[0]["reasons"])


def test_dependency_graph_normalizes_parent_directory_imports(tmp_path):
    files = {
        "src/features/layout/Header.js": (
            "import SearchHistory from '../search/SearchHistory';\n"
            "export default function Header() { return SearchHistory(); }"
        ),
        "src/features/search/SearchHistory.js": (
            "export default function SearchHistory() { return null; }"
        ),
    }
    for path, content in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    engine = RepositoryCodeIndex()
    index, _, _ = engine.build(tmp_path, list(files), "head")

    assert index["files"]["src/features/layout/Header.js"]["imports"] == [
        "src/features/search/SearchHistory.js"
    ]
    assert index["files"]["src/features/search/SearchHistory.js"]["referenced_by"] == [
        "src/features/layout/Header.js"
    ]

    raw = [
        {"path": "src/features/search/SearchHistory.js", "score": 1.0, "reasons": [], "chunks": []},
        {"path": "src/features/layout/Header.js", "score": 0.5, "reasons": [], "chunks": []},
    ]
    ranked = engine.rank_context_files(
        index, "记录搜索历史并支持清除", limit=2, ranked_hits=raw,
    )
    assert [item["path"] for item in ranked] == [
        "src/features/search/SearchHistory.js",
        "src/features/layout/Header.js",
    ]


def test_context_ranking_preserves_specific_top_result_over_generic_action():
    engine = RepositoryCodeIndex()
    records = {
        "src/features/watch/Comments.js": {
            "symbols": ["Comments"], "imports": [], "referenced_by": [], "search_text": "comments",
        },
        "src/features/studio/DeleteVideo.js": {
            "symbols": ["DeleteVideo"], "imports": [], "referenced_by": [], "search_text": "delete video",
        },
        "src/features/watch/WatchPage.js": {
            "symbols": ["WatchPage"], "imports": [], "referenced_by": [], "search_text": "watch",
        },
    }
    raw = [
        {"path": "src/features/watch/Comments.js", "score": 1.0, "reasons": [], "chunks": []},
        {"path": "src/features/studio/DeleteVideo.js", "score": 0.9, "reasons": [], "chunks": []},
        {"path": "src/features/watch/WatchPage.js", "score": 0.8, "reasons": [], "chunks": []},
    ]

    ranked = engine.rank_context_files(
        {"files": records}, "实现视频评论发布、删除以及按时间排序", limit=3, ranked_hits=raw,
    )

    assert ranked[0]["path"] == "src/features/watch/Comments.js"


def test_context_ranking_promotes_high_precision_compound_role():
    engine = RepositoryCodeIndex()
    records = {
        "src/features/watch/Player.js": {
            "symbols": ["Player"], "imports": [], "referenced_by": [], "search_text": "player",
        },
        "src/features/watch/Recommendations.js": {
            "symbols": ["Recommendations"], "imports": [], "referenced_by": [], "search_text": "related autoplay",
        },
        "src/features/watch/WatchPage.js": {
            "symbols": ["WatchPage"], "imports": [], "referenced_by": [], "search_text": "watch",
        },
    }
    raw = [
        {"path": "src/features/watch/Player.js", "score": 1.0, "reasons": [], "chunks": []},
        {"path": "src/features/watch/Recommendations.js", "score": 0.9, "reasons": [], "chunks": []},
        {"path": "src/features/watch/WatchPage.js", "score": 0.8, "reasons": [], "chunks": []},
    ]

    ranked = engine.rank_context_files(
        {"files": records}, "实现观看页相关推荐列表和自动连播开关", limit=3, ranked_hits=raw,
    )

    assert ranked[0]["path"] == "src/features/watch/Recommendations.js"


def test_context_ranking_reserves_direct_results_while_expanding_graph():
    engine = RepositoryCodeIndex()
    records = {
        "src/Feature.js": {
            "symbols": ["Feature"], "imports": [], "referenced_by": ["src/Page.js"],
            "search_text": "feature",
        },
        "src/Second.js": {
            "symbols": ["Second"], "imports": [], "referenced_by": [], "search_text": "second",
        },
        "src/Third.js": {
            "symbols": ["Third"], "imports": [], "referenced_by": [], "search_text": "third",
        },
        "src/Page.js": {
            "symbols": ["Page"], "imports": ["src/Feature.js"], "referenced_by": [],
            "search_text": "page feature",
        },
    }
    raw = [
        {"path": "src/Feature.js", "score": 1.0, "reasons": [], "chunks": []},
        {"path": "src/Second.js", "score": 0.9, "reasons": [], "chunks": []},
        {"path": "src/Third.js", "score": 0.8, "reasons": [], "chunks": []},
    ]

    ranked = engine.rank_context_files(
        {"files": records}, "修改 Feature 功能", limit=4, ranked_hits=raw,
    )

    paths = [item["path"] for item in ranked]
    assert "src/Feature.js" in paths
    assert "src/Second.js" in paths
    assert "src/Page.js" in paths


def test_generic_plan_language_does_not_expand_write_scope():
    repository = RepositoryAnalysis(language="javascript", framework="react", file_count=4,
        entrypoints=["src/index.js"], context_pack=RepositoryContextPack(
            primary_files=["src/pages/YourVideos.js"], dependency_files=["src/App.js", "src/index.js"], test_files=[]))
    plan = TechnicalPlan(approach="先检查入口和调用链再修改", affected_files=["src/pages/YourVideos.js"],
        implementation_steps=["检查入口"], test_plan=[], risks=[])
    assert RequirementPlanningAgent.ensure_dependency_scope(plan, repository, "Your Videos 页面切换 tab") == []
    assert plan.affected_files == ["src/pages/YourVideos.js"]
    assert RequirementPlanningAgent.ensure_dependency_scope(plan, repository, "更新页面导入路径")
