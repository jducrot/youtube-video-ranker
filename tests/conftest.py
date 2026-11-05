"""Shared fixtures: load the skill's scripts without installing anything."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "skills" / "youtube-video-ranker"
SCRIPTS = SKILL_DIR / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def prefetch():
    return _load("rank_prefetch", SCRIPTS / "prefetch.py")


@pytest.fixture(scope="session")
def rank_tools():
    return _load("rank_tools_mod", SCRIPTS / "rank_tools.py")


@pytest.fixture(scope="session")
def telemetry_path() -> Path:
    return SCRIPTS / "telemetry.py"


@pytest.fixture(scope="session")
def scorer_path() -> Path:
    return SCRIPTS / "rank_videos.py"


@pytest.fixture(scope="session")
def run_scorer(scorer_path):
    """Run the scorer; returns the parsed output. Raises if it exits non-zero."""
    def _run(data: dict) -> dict:
        out = subprocess.run([sys.executable, "-I", str(scorer_path)],
                             input=json.dumps(data), capture_output=True,
                             text=True, check=True)
        return json.loads(out.stdout)
    return _run


@pytest.fixture(scope="session")
def run_scorer_raw(scorer_path):
    """Run the scorer; returns the CompletedProcess without raising."""
    def _run(data) -> subprocess.CompletedProcess:
        payload = data if isinstance(data, str) else json.dumps(data)
        return subprocess.run([sys.executable, "-I", str(scorer_path)],
                              input=payload, capture_output=True, text=True)
    return _run


def make_points(*specs):
    """specs: (id, core, verdict) -> list of point dicts."""
    return [{"id": i, "core": c, "verdict": v} for i, c, v in specs]
