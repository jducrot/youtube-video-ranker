"""Shared fixtures: load the skill's scripts without installing anything, and
pull the scorer out of SKILL.md (it is embedded there so the skill stays a
single portable file; the tests treat SKILL.md as the one source of truth)."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "skill"
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
def telemetry_path() -> Path:
    return SCRIPTS / "telemetry.py"


@pytest.fixture(scope="session")
def scorer_path(tmp_path_factory) -> Path:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"```python\n(#!/usr/bin/env python3\n\"\"\"Score and rank.*?)\n```",
                  text, re.S)
    assert m, "scorer code block not found in SKILL.md"
    p = tmp_path_factory.mktemp("scorer") / "rank_videos.py"
    p.write_text(m.group(1), encoding="utf-8")
    return p


@pytest.fixture(scope="session")
def run_scorer(scorer_path):
    def _run(data: dict) -> dict:
        out = subprocess.run([sys.executable, "-I", str(scorer_path)],
                             input=json.dumps(data), capture_output=True,
                             text=True, check=True)
        return json.loads(out.stdout)
    return _run


def make_points(*specs):
    """specs: (id, core, verdict) -> list of point dicts."""
    return [{"id": i, "core": c, "verdict": v} for i, c, v in specs]
