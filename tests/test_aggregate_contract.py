from challenge_registry import aggregate_ready


def test_aggregate_depends_only_on_checked_children() -> None:
    signal = {
        "children": [11, 12, 13],
        "required_count": 3,
        "authority": "server_checked_locations",
    }
    assert not aggregate_ready(signal, {11, 12})
    assert aggregate_ready(signal, {11, 12, 13})
    assert not aggregate_ready(signal, {99})
