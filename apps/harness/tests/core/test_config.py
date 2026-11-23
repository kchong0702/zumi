import pytest
from pydantic import ValidationError

from src.core.config import RunSettings


def test_a_run_lease_shorter_than_two_heartbeats_is_refused():
    with pytest.raises(ValidationError, match="at least twice heartbeat_s"):
        RunSettings(heartbeat_s=10, dead_after_s=15)


@pytest.mark.parametrize("field", ["sweep_s", "poll_s", "max_workers", "max_attempts"])
def test_run_settings_below_their_least_are_refused(field: str):
    with pytest.raises(ValidationError, match=field):
        RunSettings.model_validate({field: 0})
