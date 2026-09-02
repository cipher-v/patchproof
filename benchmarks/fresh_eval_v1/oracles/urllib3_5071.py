def test_explicit_zero_port_survives_url_normalization():
    from urllib3.util import parse_url

    observed = parse_url("http://service.invalid:0/resource")

    assert observed.port == 0
    assert observed.netloc == "service.invalid:0"
