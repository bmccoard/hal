from hal.sayings import HAL_SAYINGS, startup_saying


def test_startup_saying_uses_the_central_catalog(monkeypatch) -> None:
    monkeypatch.setattr("hal.sayings.secrets.choice", lambda items: items[1])

    assert startup_saying() == HAL_SAYINGS[1]
    assert len(HAL_SAYINGS) == 3
