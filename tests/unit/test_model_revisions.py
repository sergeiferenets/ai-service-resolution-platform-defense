"""Имена репозиториев не подменяют immutable revisions."""
import dataclasses
from pathlib import Path

import pytest
import yaml

from backend.config import model_revision
from backend.orchestrator.artifacts import build_artifacts


@pytest.mark.parametrize("field", ["llm_model_revision", "vision_model_revision", "embedding_model_revision"])
def test_each_revision_changes_artifact_metadata(field):
    values = dict(code_commit="code", rules_version="rules", prompt_versions={"context": "prompt"},
                  llm_model_revision="a" * 40, vision_model_revision="b" * 40,
                  embedding_model_revision="c" * 40)
    old = build_artifacts(**values)
    values[field] = "d" * 40
    new = build_artifacts(**values)
    assert old.id != new.id
    assert dataclasses.asdict(old)[field] != dataclasses.asdict(new)[field]


@pytest.mark.parametrize("value", ["main", "Qwen/Qwen3-8B-AWQ", "abc", ""])
def test_repository_or_branch_is_not_revision(value, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_REVISION", value)
    if value:
        with pytest.raises(ValueError): model_revision("LLM_MODEL_REVISION")
    else:
        assert model_revision("LLM_MODEL_REVISION") is None
    with pytest.raises(ValueError):
        build_artifacts(code_commit="x", rules_version="r", prompt_versions={},
                        llm_model_revision=value, vision_model_revision="b" * 40)


def test_deployment_and_loader_pin_same_revision():
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "infra/docker-compose.yml").read_text(encoding="utf-8"))
    for service, prefix in [("vllm-llm", "LLM"), ("vllm-vision", "VISION"), ("vllm-embeddings", "EMBED")]:
        command = compose["services"][service]["command"]
        assert any(c.startswith("--revision=${" + prefix + "_MODEL_REVISION:?") for c in command)
        assert any(c.startswith("--tokenizer-revision=${" + prefix + "_MODEL_REVISION:?") for c in command)
    script = (root / "infra/bootstrap.sh").read_text(encoding="utf-8")
    assert 'download "$repo" --revision "$revision"' in script
    assert 'snapshot_download(repo_id=sys.argv[1], revision=sys.argv[2])' in script
