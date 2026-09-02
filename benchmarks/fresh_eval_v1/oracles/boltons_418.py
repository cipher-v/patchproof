def test_singular_word_with_double_s_is_not_truncated():
    from boltons.strutils import singularize

    assert singularize("compass") == "compass"
