"""Independent regression oracle for Kludex/starlette#3317."""

from starlette.datastructures import URL


def test_clearing_port_on_authorityless_url_is_safe() -> None:
    original = URL("file:///tmp/report.txt")

    try:
        replaced = original.replace(port=None)
        observed = str(replaced)
    except Exception as error:  # The assertion records the old public failure.
        observed = type(error).__name__

    assert observed == "file:///tmp/report.txt"
