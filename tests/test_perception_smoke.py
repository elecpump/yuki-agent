from yuki.perception.main import build_perception


class FakeBus:
    pass


def test_build_perception_is_callable():
    bus = FakeBus()
    result = build_perception(bus)
    assert result is None
