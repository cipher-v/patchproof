"""Independent regression oracle for Textualize/rich#3938."""

from io import StringIO

from rich.console import Console


def test_soft_wrapped_background_style_does_not_span_line_terminators() -> None:
    stream = StringIO()
    console = Console(
        file=stream,
        width=20,
        color_system="standard",
        force_terminal=True,
        no_color=False,
    )

    console.print("alpha\nbeta", style="on red", soft_wrap=True, markup=False, end="")

    assert stream.getvalue() == "\x1b[41malpha\x1b[0m\n\x1b[41mbeta\x1b[0m"
