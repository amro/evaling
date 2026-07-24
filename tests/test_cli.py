from click.testing import CliRunner

from evaling import __version__
from evaling.cli import main


def test_version_flag_reports_package_version():
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
    assert "evaling" in result.output


def test_bare_invocation_shows_help():
    result = CliRunner().invoke(main, [])
    # click's no_args_is_help exits 2: no subcommand given is a usage signal
    assert result.exit_code == 2
    assert "Usage:" in result.output


def test_version_matches_package_metadata():
    from importlib.metadata import version

    assert version("evaling") == __version__
