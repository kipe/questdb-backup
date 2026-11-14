"""Health status without binding a port."""

from questdb_backup.health import HealthState


def test_health_is_ok_until_a_run_fails() -> None:
    state = HealthState()
    assert state.status_code() == 200
    state.mark(True)
    assert state.status_code() == 200
    state.mark(False)
    assert state.status_code() == 503
