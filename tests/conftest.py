from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import git
import pytest

from maxxing.config import Settings
from maxxing.evidence.models import EvidenceBundle

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIO_FIXTURES = sorted(p for p in FIXTURES.glob("*.json") if not p.name.endswith(".expected.json"))


def load_fixture(path: Path) -> tuple[EvidenceBundle, dict]:
    bundle = EvidenceBundle.model_validate_json(path.read_text())
    expected = json.loads(path.with_name(path.stem + ".expected.json").read_text())
    return bundle, expected


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        repo_path=tmp_path / "no-repo",
        llm_fake=True,
        db_path=tmp_path / "incidents.db",
        lb_url="http://127.0.0.1:9",  # nothing listens here
        health_timeout_s=0.2,
    )


class SyntheticRepo:
    """Tiny GitPython helper for building commit histories in tmp_path."""

    def __init__(self, path: Path):
        self.path = path
        self.repo = git.Repo.init(path, initial_branch="main")
        self._n = 0

    def write(self, rel: str, text: str) -> None:
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg: str, author: str = "Arjun", email: str = "arjun@vip.dev") -> str:
        self._n += 1
        self.repo.git.add(A=True)
        date = datetime(2026, 10, 25, 12, self._n, tzinfo=UTC).isoformat()
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
               "GIT_COMMITTER_NAME": "ci", "GIT_COMMITTER_EMAIL": "ci@vip.dev"}
        with self.repo.git.custom_environment(**env):
            self.repo.git.commit("-q", "--no-verify", "-m", msg, f"--author={author} <{email}>")
        return self.repo.head.commit.hexsha


@pytest.fixture
def synth_repo(tmp_path: Path) -> SyntheticRepo:
    return SyntheticRepo(tmp_path / "repo")
