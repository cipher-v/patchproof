def test_headerless_column_stack_combines_each_row():
    import tablib

    left = tablib.Dataset()
    left.append((10, 20))
    left.append((30, 40))
    right = tablib.Dataset()
    right.append((50,))
    right.append((60,))

    try:
        combined = left.stack_cols(right)
    except TypeError as error:
        raise AssertionError(f"headerless column stacking raised unexpectedly: {error}") from error

    assert combined.headers is None
    assert list(combined) == [(10, 20, 50), (30, 40, 60)]
