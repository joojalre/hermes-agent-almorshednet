"""SSH working-directory syntax belongs to the remote shell, not the host OS."""

import pytest

from tui_gateway import server


@pytest.mark.parametrize(
    ("path", "accepted"),
    [
        ("/home/remote/project", True),
        ("~", True),
        ("~/project", True),
        ("project", False),
        ("project/subdir", False),
        ("", False),
    ],
)
def test_remote_cwd_shape_uses_remote_shell_syntax(path, accepted):
    assert server._is_remote_cwd_shape(path) is accepted


def test_bound_ssh_profile_keeps_remote_paths_and_local_profile_checks_host(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    ssh_home = tmp_path / "ssh-profile"
    local_home = tmp_path / "local-profile"
    ssh_home.mkdir()
    local_home.mkdir()
    (ssh_home / "config.yaml").write_text("terminal:\n  backend: ssh\n", encoding="utf-8")
    (local_home / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")

    remote = "/remote-fixture-only/nonexistent/project"
    assert server._bound_terminal_backend(ssh_home) == "ssh"
    assert server._workspace_cwd(ssh_home, remote) == remote
    assert server._workspace_cwd(ssh_home, "~/project") == "~/project"
    with pytest.raises(ValueError, match="must be absolute or ~-relative"):
        server._workspace_cwd(ssh_home, "relative/project")

    existing = tmp_path / "existing-host-project"
    existing.mkdir()
    missing = tmp_path / "missing-host-project"
    assert not missing.exists()
    assert server._is_remote_cwd_shape(str(existing))
    assert server._bound_terminal_backend(local_home) == "local"
    assert server._workspace_cwd(local_home, str(existing)) == str(existing)
    with pytest.raises(ValueError, match="working directory does not exist"):
        server._workspace_cwd(local_home, str(missing))
