def test_shallow_copy_preserves_repeated_ordered_multidict_values():
    from copy import copy

    from boltons.dictutils import OrderedMultiDict

    original = OrderedMultiDict([("channel", "alpha"), ("channel", "beta"), ("priority", "high")])

    observed = copy(original)

    assert observed.getlist("channel") == ["alpha", "beta"]
    assert observed.getlist("priority") == ["high"]
