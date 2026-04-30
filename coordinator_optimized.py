"""The ukraine_alarm component - Enhanced version with alert index optimization."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

import aiohttp
from aiohttp import ClientSession
from uasiren.client import Client

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_REGION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import ALERT_TYPES, DOMAIN

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(seconds=10)

type UkraineAlarmConfigEntry = ConfigEntry[UkraineAlarmDataUpdateCoordinator]


class UkraineAlarmDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching Ukraine Alarm API."""

    config_entry: UkraineAlarmConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: UkraineAlarmConfigEntry,
        session: ClientSession,
    ) -> None:
        """Initialize."""
        self.region_id = config_entry.data[CONF_REGION]
        self.uasiren = Client(session)
        # Зберігаємо історію тривог для відстеження часу початку
        self._alert_history: dict[str, dict[str, Any]] = {}
        # Зберігаємо останній індекс для оптимізації
        self._last_alert_index: int | None = None

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data via library."""
        _LOGGER.debug(
            "Fetching alerts for region %s (interval: %s)",
            self.region_id,
            self.update_interval,
        )

        # Спочатку перевіряємо чи змінився індекс тривог (легкий запит)
        try:
            status = await self.uasiren.get_last_alert_index()
            current_index = status.get("lastActionIndex")
            
            # Якщо індекс не змінився, повертаємо попередні дані
            if (
                self._last_alert_index is not None
                and current_index == self._last_alert_index
                and self.data
            ):
                _LOGGER.debug(
                    "Alert index unchanged (%s), skipping full update",
                    current_index,
                )
                # Оновлюємо тривалість для активних тривог
                return self._update_durations()
            
            self._last_alert_index = current_index
            _LOGGER.debug("Alert index changed to %s, fetching full data", current_index)
            
        except (aiohttp.ClientError, TimeoutError) as error:
            _LOGGER.debug(
                "Could not fetch alert index, proceeding with full update: %s", error
            )

        # Отримуємо повні дані про тривоги
        try:
            res = await self.uasiren.get_alerts(self.region_id)
        except aiohttp.ClientResponseError as error:
            if error.status == 429:
                _LOGGER.warning(
                    "Rate limit reached for region %s (50 req/min limit). "
                    "Consider increasing update interval.",
                    self.region_id,
                )
            raise UpdateFailed(
                f"Error fetching alerts from API (status {error.status}): {error}"
            ) from error
        except (aiohttp.ClientError, TimeoutError) as error:
            raise UpdateFailed(f"Error fetching alerts from API: {error}") from error

        # Валідація відповіді
        if not res or not isinstance(res, list) or len(res) == 0:
            _LOGGER.warning(
                "Empty or invalid response from API for region %s", self.region_id
            )
            # Повертаємо попередні дані якщо вони є
            if self.data:
                return self.data
            return self._create_empty_data()

        if "activeAlerts" not in res[0]:
            _LOGGER.error(
                "Invalid API response structure for region %s: missing 'activeAlerts'",
                self.region_id,
            )
            raise UpdateFailed("Invalid API response structure")

        active_alerts = res[0]["activeAlerts"]
        _LOGGER.debug(
            "Received %d active alert(s) for region %s",
            len(active_alerts),
            self.region_id,
        )

        current_time = dt_util.utcnow()
        current_data = {}

        # Обробка активних тривог
        active_alert_types = set()
        for alert in active_alerts:
            alert_type = alert.get("type", "UNKNOWN")
            active_alert_types.add(alert_type)

            # Якщо це нова тривога, зберігаємо час початку
            if alert_type not in self._alert_history or not self._alert_history[
                alert_type
            ].get("active", False):
                self._alert_history[alert_type] = {
                    "active": True,
                    "started_at": current_time,
                    "last_update": current_time,
                    "alert_data": alert,
                }
                _LOGGER.info(
                    "New alert started for region %s: %s", self.region_id, alert_type
                )
            else:
                # Оновлюємо час останнього оновлення
                self._alert_history[alert_type]["last_update"] = current_time
                self._alert_history[alert_type]["alert_data"] = alert

        # Формуємо дані для кожного типу тривоги
        for alert_type in ALERT_TYPES:
            is_active = alert_type in active_alert_types

            if is_active and alert_type in self._alert_history:
                history = self._alert_history[alert_type]
                started_at = history["started_at"]
                duration = (current_time - started_at).total_seconds()

                current_data[alert_type] = {
                    "state": True,
                    "started_at": started_at.isoformat(),
                    "duration": int(duration),
                    "duration_formatted": self._format_duration(duration),
                    "last_update": history["last_update"].isoformat(),
                }
            else:
                # Тривога неактивна
                if alert_type in self._alert_history and self._alert_history[
                    alert_type
                ].get("active", False):
                    # Тривога щойно закінчилась
                    history = self._alert_history[alert_type]
                    ended_at = current_time
                    total_duration = (ended_at - history["started_at"]).total_seconds()

                    _LOGGER.info(
                        "Alert ended for region %s: %s (duration: %s)",
                        self.region_id,
                        alert_type,
                        self._format_duration(total_duration),
                    )

                    # Зберігаємо інформацію про завершену тривогу
                    self._alert_history[alert_type] = {
                        "active": False,
                        "started_at": history["started_at"],
                        "ended_at": ended_at,
                        "duration": int(total_duration),
                        "last_update": ended_at,
                    }

                # Повертаємо дані про неактивну тривогу
                if alert_type in self._alert_history:
                    history = self._alert_history[alert_type]
                    current_data[alert_type] = {
                        "state": False,
                        "last_started_at": history.get("started_at", "").isoformat()
                        if isinstance(history.get("started_at"), datetime)
                        else None,
                        "last_ended_at": history.get("ended_at", "").isoformat()
                        if isinstance(history.get("ended_at"), datetime)
                        else None,
                        "last_duration": history.get("duration"),
                        "last_duration_formatted": self._format_duration(
                            history.get("duration", 0)
                        )
                        if history.get("duration")
                        else None,
                    }
                else:
                    current_data[alert_type] = {
                        "state": False,
                        "last_started_at": None,
                        "last_ended_at": None,
                        "last_duration": None,
                        "last_duration_formatted": None,
                    }

        # Додаємо загальну інформацію
        current_data["_metadata"] = {
            "region_id": self.region_id,
            "last_update": current_time.isoformat(),
            "active_alerts_count": len(active_alert_types),
            "has_active_alerts": len(active_alert_types) > 0,
            "alert_index": self._last_alert_index,
        }

        return current_data

    def _update_durations(self) -> dict[str, Any]:
        """Update durations for active alerts without fetching new data."""
        if not self.data:
            return self._create_empty_data()

        current_time = dt_util.utcnow()
        updated_data = dict(self.data)

        # Оновлюємо тривалість для активних тривог
        for alert_type in ALERT_TYPES:
            if (
                alert_type in self._alert_history
                and self._alert_history[alert_type].get("active")
            ):
                history = self._alert_history[alert_type]
                started_at = history["started_at"]
                duration = (current_time - started_at).total_seconds()

                updated_data[alert_type] = {
                    "state": True,
                    "started_at": started_at.isoformat(),
                    "duration": int(duration),
                    "duration_formatted": self._format_duration(duration),
                    "last_update": history["last_update"].isoformat(),
                }

        # Оновлюємо метадані
        if "_metadata" in updated_data:
            updated_data["_metadata"]["last_update"] = current_time.isoformat()

        return updated_data

    def _create_empty_data(self) -> dict[str, Any]:
        """Create empty data structure."""
        data = {}
        for alert_type in ALERT_TYPES:
            data[alert_type] = {
                "state": False,
                "last_started_at": None,
                "last_ended_at": None,
                "last_duration": None,
                "last_duration_formatted": None,
            }
        data["_metadata"] = {
            "region_id": self.region_id,
            "last_update": dt_util.utcnow().isoformat(),
            "active_alerts_count": 0,
            "has_active_alerts": False,
            "alert_index": self._last_alert_index,
        }
        return data

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format duration in human-readable format."""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            minutes = int(seconds / 60)
            secs = int(seconds % 60)
            return f"{minutes}m {secs}s"
        else:
            hours = int(seconds / 3600)
            minutes = int((seconds % 3600) / 60)
            return f"{hours}h {minutes}m"
