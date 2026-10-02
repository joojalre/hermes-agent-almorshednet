"""The external action must never install Hermes into the plugin checkout."""
import os
from pathlib import Path
import subprocess

import pytest
import hermes_yaml as yaml


@pytest.mark.platforms("posix")
def test_external_validator_checkout_uses_requested_ref_and_preserves_caller(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    action = yaml.safe_load((repo / ".github/actions/plugin-validate/action.yml").read_text())
    source_step = next(step for step in action["runs"]["steps"] if step.get("id") == "source")
    ref = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    caller = tmp_path / "plugin"
    caller.mkdir()
    manifest = caller / "pyproject.toml"
    manifest.write_text('[project]\nname = "caller-plugin"\nversion = "1.0.0"\n')
    before = manifest.read_bytes()
    runner = tmp_path / "runner"
    runner.mkdir()
    output = tmp_path / "output"
    env_file = tmp_path / "env"
    env = {**os.environ, "RUNNER_TEMP": str(runner), "RUNNER_OS": "Linux",
           "GITHUB_OUTPUT": str(output), "GITHUB_ENV": str(env_file), "_HERMES_REF": ref,
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{repo.as_uri()}.insteadOf",
           "GIT_CONFIG_VALUE_0": "https://github.com/NousResearch/hermes-agent.git"}
    subprocess.run(["bash", "-c", source_step["run"]], cwd=caller, env=env, check=True,
                   capture_output=True, text=True, timeout=60)
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    source = Path(outputs["source"])
    assert source.is_relative_to(runner)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip() == ref
    assert manifest.read_bytes() == before
    assert sorted(path.name for path in caller.iterdir()) == ["pyproject.toml"]
    assert "python-version" in outputs  # the real setup-pm pin reader ran
    assert not (source / ".build/validator").exists()  # preparation is not installation


@pytest.mark.platforms("posix")
def test_default_validates_the_immutable_action_checkout_without_fetching(tmp_path):
    """No explicit ref: the action validates the checkout it was pinned to, never moving main."""
    repo = Path(__file__).resolve().parents[2]
    action = yaml.safe_load((repo / ".github/actions/plugin-validate/action.yml").read_text())
    source_step = next(step for step in action["runs"]["steps"] if step.get("id") == "source")
    # A space in the path exercises the quoted source expansion, not just its spelling.
    pinned = tmp_path / "pinned checkout"
    action_path = pinned / ".github" / "actions" / "plugin-validate"
    action_path.mkdir(parents=True)
    toolchain = pinned / "scripts" / "ci" / "setup_toolchain.py"
    toolchain.parent.mkdir(parents=True)
    toolchain.write_text(
        "import os\nopen(os.environ['GITHUB_OUTPUT'], 'a').write('prepared-from=' + __file__ + '\\n')\n")
    no_git = tmp_path / "no-git"
    no_git.mkdir()
    (no_git / "git").write_text("#!/bin/sh\necho unexpected git call >&2\nexit 97\n")
    (no_git / "git").chmod(0o755)
    runner = tmp_path / "runner"
    runner.mkdir()
    output = tmp_path / "output"
    env = {**os.environ, "RUNNER_TEMP": str(runner), "RUNNER_OS": "Linux", "GITHUB_OUTPUT": str(output),
           "_HERMES_REF": "", "_ACTION_PATH": str(action_path),
           "PATH": os.pathsep.join((str(no_git), os.environ.get("PATH", "")))}
    subprocess.run(["bash", "-c", source_step["run"]], cwd=tmp_path, env=env, check=True,
                   capture_output=True, text=True, timeout=60)
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert Path(outputs["source"]).resolve() == pinned.resolve()
    assert Path(outputs["prepared-from"]).resolve() == toolchain.resolve()
    assert Path(outputs["build"]).is_relative_to(runner)
