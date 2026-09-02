def test_structured_log_formatting_does_not_add_fields_to_input_mapping():
    import logging

    from pythonjsonlogger import jsonlogger

    message = {"event": "fresh-evaluation"}
    record = logging.LogRecord(
        name="oracle",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
        sinfo="bounded stack detail",
    )

    jsonlogger.JsonFormatter().format(record)

    assert message == {"event": "fresh-evaluation"}
