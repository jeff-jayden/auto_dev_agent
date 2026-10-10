import json
import re
from pathlib import Path, PurePosixPath


FIXTURE = Path(__file__).parent / "fixtures" / "cfe_unit_commit_rag_cases.json"
INDEXABLE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".py"}


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_cfe_unit_commit_dataset_has_48_unique_code_cases():
    dataset = load_fixture()
    cases = dataset["cases"]

    assert len(cases) == 48
    assert [case["requirement_id"] for case in cases] == [
        f"R{number:02d}" for number in range(2, 50)
    ]
    assert len({case["name"] for case in cases}) == len(cases)
    assert len({case["source_commit"] for case in cases}) == len(cases)
    assert {item["requirement_id"] for item in dataset["excluded_commits"]} == {
        "R01",
        "R50",
    }


def test_cfe_unit_commit_dataset_paths_and_queries_are_valid():
    dataset = load_fixture()
    irrelevant = set(dataset["known_irrelevant_files"])

    assert "src/reportWebVitals.js" in irrelevant
    for case in dataset["cases"]:
        assert len(case["query"].strip()) >= 8
        assert re.fullmatch(r"[0-9a-f]{40}", case["source_commit"])
        assert case["expected_files"]
        assert not irrelevant.intersection(case["expected_files"])
        for path in case["expected_files"]:
            value = PurePosixPath(path)
            assert not value.is_absolute()
            assert ".." not in value.parts
            assert value.suffix in INDEXABLE_SUFFIXES


def test_cfe_unit_fixture_is_compatible_with_repository_evaluator_schema():
    dataset = load_fixture()

    for case in dataset["cases"]:
        assert set(case) >= {"name", "query", "expected_files"}
