from camera_stream.demand import TopicDemand


def test_specific_topic_demand_tracks_subscribe_and_unsubscribe() -> None:
    demand = TopicDemand(["front", "side"])

    assert demand.apply(b"\x01front/color") == {"front"}
    assert demand.count("front") == 1
    assert demand.count("side") == 0
    assert demand.apply(b"\x00front/color") == {"front"}
    assert demand.count("front") == 0


def test_empty_prefix_demands_every_camera_and_duplicate_events_balance() -> None:
    demand = TopicDemand(["front", "side"])

    assert demand.apply(b"\x01") == {"front", "side"}
    assert demand.apply(b"\x01front/color") == {"front"}
    assert demand.count("front") == 2
    assert demand.count("side") == 1
    assert demand.apply(b"\x00") == {"front", "side"}
    assert demand.count("front") == 1
    assert demand.count("side") == 0
