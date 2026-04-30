"""Test example for Ukraine Alarm integration improvements."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.ukraine_alarm.coordinator import (
    UkraineAlarmDataUpdateCoordinator,
)


@pytest.fixture
def mock_config_entry():
    """Create a mock config entry."""
    entry = MagicMock()
    entry.data = {"region": "5", "name": "Kyiv"}
    return entry


@pytest.fixture
def mock_session():
    """Create a mock aiohttp session."""
    return MagicMock()


@pytest.fixture
async def coordinator(hass: HomeAssistant, mock_config_entry, mock_session):
    """Create a coordinator instance."""
    coordinator = UkraineAlarmDataUpdateCoordinator(
        hass, mock_config_entry, mock_session
    )
    return coordinator


async def test_alert_start_tracking(coordinator):
    """Test that alert start time is tracked correctly."""
    # Mock API response with active air alert
    mock_response = [
        {
            "activeAlerts": [
                {"type": "AIR", "lastUpdate": "2026-04-30T14:30:00.000Z"}
            ]
        }
    ]

    with patch.object(
        coordinator.uasiren, "get_alerts", return_value=mock_response
    ):
        # First update - alert starts
        data = await coordinator._async_update_data()

        assert data["AIR"]["state"] is True
        assert "started_at" in data["AIR"]
        assert "duration" in data["AIR"]
        assert data["AIR"]["duration"] >= 0


async def test_alert_duration_increases(coordinator):
    """Test that alert duration increases over time."""
    mock_response = [{"activeAlerts": [{"type": "AIR"}]}]

    with patch.object(
        coordinator.uasiren, "get_alerts", return_value=mock_response
    ):
        # First update
        data1 = await coordinator._async_update_data()
        duration1 = data1["AIR"]["duration"]

        # Wait a bit (simulated)
        with patch("homeassistant.util.dt.utcnow") as mock_now:
            mock_now.return_value = dt_util.utcnow() + timedelta(seconds=10)

            # Second update
            data2 = await coordinator._async_update_data()
            duration2 = data2["AIR"]["duration"]

            # Duration should increase
            assert duration2 > duration1


async def test_alert_end_tracking(coordinator):
    """Test that alert end time and duration are tracked."""
    # Start with active alert
    mock_response_active = [{"activeAlerts": [{"type": "AIR"}]}]

    with patch.object(
        coordinator.uasiren, "get_alerts", return_value=mock_response_active
    ):
        data1 = await coordinator._async_update_data()
        assert data1["AIR"]["state"] is True

    # Alert ends
    mock_response_inactive = [{"activeAlerts": []}]

    with patch.object(
        coordinator.uasiren, "get_alerts", return_value=mock_response_inactive
    ):
        data2 = await coordinator._async_update_data()

        assert data2["AIR"]["state"] is False
        assert "last_started_at" in data2["AIR"]
        assert "last_ended_at" in data2["AIR"]
        assert "last_duration" in data2["AIR"]
        assert data2["AIR"]["last_duration"] > 0


async def test_multiple_alert_types(coordinator):
    """Test handling multiple alert types simultaneously."""
    mock_response = [
        {
            "activeAlerts": [
                {"type": "AIR"},
                {"type": "ARTILLERY"},
            ]
        }
    ]

    with patch.object(
        coordinator.uasiren, "get_alerts", return_value=mock_response
    ):
        data = await coordinator._async_update_data()

        assert data["AIR"]["state"] is True
        assert data["ARTILLERY"]["state"] is True
        assert data["CHEMICAL"]["state"] is False
        assert data["_metadata"]["active_alerts_count"] == 2
        assert data["_metadata"]["has_active_alerts"] is True


async def test_duration_formatting(coordinator):
    """Test duration formatting."""
    # Test seconds
    assert coordinator._format_duration(45) == "45s"

    # Test minutes
    assert coordinator._format_duration(90) == "1m 30s"

    # Test hours
    assert coordinator._format_duration(7200) == "2h 0m"
    assert coordinator._format_duration(7890) == "2h 11m"


async def test_empty_api_response(coordinator):
    """Test handling of empty API response."""
    mock_response = []

    with patch.object(
        coordinator.uasiren, "get_alerts", return_value=mock_response
    ):
        # Should not raise exception
        data = await coordinator._async_update_data()

        # Should return all alerts as inactive
        assert data["AIR"]["state"] is False
        assert data["_metadata"]["active_alerts_count"] == 0


async def test_invalid_api_response(coordinator):
    """Test handling of invalid API response."""
    mock_response = [{"invalid": "data"}]

    with patch.object(
        coordinator.uasiren, "get_alerts", return_value=mock_response
    ):
        # Should raise UpdateFailed
        from homeassistant.helpers.update_coordinator import UpdateFailed

        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()


async def test_rate_limit_error(coordinator):
    """Test handling of rate limit error."""
    from aiohttp import ClientResponseError
    from homeassistant.helpers.update_coordinator import UpdateFailed

    mock_error = ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=429,
        message="Rate limit exceeded",
    )

    with patch.object(
        coordinator.uasiren, "get_alerts", side_effect=mock_error
    ):
        with pytest.raises(UpdateFailed) as exc_info:
            await coordinator._async_update_data()

        assert "429" in str(exc_info.value)


async def test_metadata_tracking(coordinator):
    """Test that metadata is correctly tracked."""
    mock_response = [
        {
            "activeAlerts": [
                {"type": "AIR"},
                {"type": "ARTILLERY"},
            ]
        }
    ]

    with patch.object(
        coordinator.uasiren, "get_alerts", return_value=mock_response
    ):
        data = await coordinator._async_update_data()

        metadata = data["_metadata"]
        assert metadata["region_id"] == "5"
        assert "last_update" in metadata
        assert metadata["active_alerts_count"] == 2
        assert metadata["has_active_alerts"] is True


async def test_alert_restart_after_end(coordinator):
    """Test that alert can restart after ending."""
    mock_response_active = [{"activeAlerts": [{"type": "AIR"}]}]
    mock_response_inactive = [{"activeAlerts": []}]

    # First alert
    with patch.object(
        coordinator.uasiren, "get_alerts", return_value=mock_response_active
    ):
        data1 = await coordinator._async_update_data()
        first_start = data1["AIR"]["started_at"]

    # Alert ends
    with patch.object(
        coordinator.uasiren, "get_alerts", return_value=mock_response_inactive
    ):
        await coordinator._async_update_data()

    # Wait a bit
    with patch("homeassistant.util.dt.utcnow") as mock_now:
        mock_now.return_value = dt_util.utcnow() + timedelta(minutes=5)

        # Alert starts again
        with patch.object(
            coordinator.uasiren, "get_alerts", return_value=mock_response_active
        ):
            data3 = await coordinator._async_update_data()
            second_start = data3["AIR"]["started_at"]

            # Should be a new alert with new start time
            assert data3["AIR"]["state"] is True
            assert second_start != first_start
            assert "last_started_at" in data3["AIR"]


# Integration test example
async def test_binary_sensor_attributes(hass: HomeAssistant):
    """Test that binary sensor has correct attributes."""
    from custom_components.ukraine_alarm.binary_sensor import UkraineAlarmSensor
    from homeassistant.components.binary_sensor import BinarySensorEntityDescription

    # Create mock coordinator with data
    coordinator = MagicMock()
    coordinator.data = {
        "AIR": {
            "state": True,
            "started_at": "2026-04-30T14:30:00.000Z",
            "duration": 900,
            "duration_formatted": "15m 0s",
            "last_update": "2026-04-30T14:45:00.000Z",
        },
        "_metadata": {
            "region_id": "5",
            "last_update": "2026-04-30T14:45:00.000Z",
            "active_alerts_count": 1,
            "has_active_alerts": True,
        },
    }
    coordinator.last_update_success = True

    description = BinarySensorEntityDescription(
        key="AIR",
        translation_key="air",
    )

    sensor = UkraineAlarmSensor(
        name="Kyiv",
        unique_id="5",
        description=description,
        coordinator=coordinator,
    )

    # Test state
    assert sensor.is_on is True

    # Test attributes
    attrs = sensor.extra_state_attributes
    assert attrs["status"] == "active"
    assert attrs["started_at"] == "2026-04-30T14:30:00.000Z"
    assert attrs["duration_seconds"] == 900
    assert attrs["duration"] == "15m 0s"
    assert attrs["region_id"] == "5"
