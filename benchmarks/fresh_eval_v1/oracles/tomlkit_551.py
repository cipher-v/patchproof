def test_multiline_value_keeps_initial_line_break_after_round_trip():
    import tomlkit

    original = "\nalpha\nbeta"
    rendered = tomlkit.string(original, multiline=True).as_string()
    observed = str(tomlkit.parse(f"payload = {rendered}\n")["payload"])

    assert observed == original
