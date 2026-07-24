"""Command-line entry point for evaling."""

import click

from evaling import __version__


@click.group(no_args_is_help=True)
@click.version_option(version=__version__, prog_name="evaling")
def main() -> None:
    """Compare prompt variants and models, easily."""
