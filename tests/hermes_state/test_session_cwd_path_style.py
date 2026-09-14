from hermes_state_sessions import _cwd_prefix_clause


def test_double_slash_posix_prefix_does_not_gain_windows_separator_match():
    _clause, params = _cwd_prefix_clause("//srv/Repo")

    assert params == ["//srv/Repo", "//srv/Repo/%"]


def test_wsl_unc_prefix_keeps_windows_separator_compatibility():
    _clause, params = _cwd_prefix_clause("//wsl.localhost/Ubuntu/home/alex")

    assert params[-1].endswith(r"\\%")
