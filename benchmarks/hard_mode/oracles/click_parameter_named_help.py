from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import click
from click.testing import CliRunner


def test_parameter_named_help_does_not_break_value_parsing() -> None:
    command = click.Command(
        "demo",
        params=[click.Argument(["help"])],
        callback=lambda help: click.echo(help),
    )

    result = CliRunner().invoke(command, ["kept-value"])

    assert (result.exit_code, result.output) == (0, "kept-value\n")
