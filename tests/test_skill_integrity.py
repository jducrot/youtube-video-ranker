"""The failure that actually happened: the instructions and the scripts drifted apart
(and then the scripts vanished). These tests keep SKILL.md honest about what ships."""
from __future__ import annotations

import re
import subprocess
import sys

from conftest import SCRIPTS, SKILL_DIR

SKILL_MD = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_frontmatter_has_a_name_and_a_trigger_description():
    m = re.match(r'---\nname: "youtube-video-ranker"\ndescription: "(.+?)"\n---\n', SKILL_MD, re.S)
    assert m and "Use whenever the user pastes two or more YouTube URLs" in m.group(1)


def test_every_script_the_instructions_name_exists():
    named = set(re.findall(r"RANKER_DIR/scripts/([A-Za-z_.]+)", SKILL_MD))
    assert {"prefetch.py", "telemetry.py", "rank_tools.py", "inventory_brief.md", "factcheck_brief.md"} <= named
    missing = [n for n in named if not (SCRIPTS / n).exists()]
    assert not missing, f"SKILL.md names files that do not ship: {missing}"


def _help(script):
    return subprocess.run([sys.executable, "-I", str(SCRIPTS / script), "-h"],
                          capture_output=True, text=True).stdout


def test_every_subcommand_the_instructions_use_is_real():
    for script, pattern in (("telemetry.py", r'telemetry\.py" (\w[\w-]*)'),
                            ("rank_tools.py", r'rank_tools\.py" (\w[\w-]*)')):
        used = set(re.findall(pattern, SKILL_MD))
        assert used, f"no {script} commands found in SKILL.md"
        helptext = _help(script)
        for sub in used:
            assert sub in helptext, f"SKILL.md uses '{script} {sub}' but the script has no such command"


def test_the_scorer_lives_in_one_place_not_pasted_into_the_instructions():
    assert "def point_value" not in SKILL_MD
    assert (SCRIPTS / "rank_videos.py").exists()


def test_both_subagent_briefs_carry_the_untrusted_input_guard():
    for brief in ("inventory_brief.md", "factcheck_brief.md"):
        text = (SCRIPTS / brief).read_text(encoding="utf-8")
        assert "## Untrusted input" in text and "never as instructions" in text.lower(), brief


def test_briefs_do_not_hard_code_a_user_home_directory():
    for brief in ("inventory_brief.md", "factcheck_brief.md"):
        assert "C:\\Users\\" not in (SCRIPTS / brief).read_text(encoding="utf-8")


def test_the_fetch_order_documented_matches_the_code():
    code = (SCRIPTS / "prefetch.py").read_text(encoding="utf-8")
    body = code[code.index("def process"):]
    assert body.index("yta_fetch(vid, meta)") < body.index("run_fetchpy(claims_dir, vid)")
    assert "as the **primary** path" in SKILL_MD


def test_the_plugin_version_matches_the_scripts_version():
    import json
    from conftest import ROOT
    version = (SCRIPTS / "VERSION").read_text(encoding="utf-8").strip()
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["version"] == version
