from src.app.container import Container


def test_the_container_registers_every_agent_in_the_settings():
    container = Container()

    assert set(container.registry()) == {a.name for a in container.settings().agents}
