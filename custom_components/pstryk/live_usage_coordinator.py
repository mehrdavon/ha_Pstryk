"""Live energy usage data coordinator for Pstryk Energy integration."""
import logging
from datetime import timedelta
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_track_point_in_time
import homeassistant.util.dt as dt_util
from .const import (
    DOMAIN,
    API_URL,
    LIVE_USAGE_ENDPOINT
)
from .api_client import PstrykAPIClient

_LOGGER = logging.getLogger(__name__)


class PstrykLiveUsageCoordinator(DataUpdateCoordinator):
    """Class to manage fetching live hourly energy usage data."""

    def __init__(self, hass: HomeAssistant, api_client: PstrykAPIClient):
        """Initialize."""
        self.api_client = api_client
        self._unsub_timer = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_live_usage",
            update_interval=timedelta(minutes=5),  # Update every 5 minutes
        )

    async def _async_update_data(self):
        """Fetch live hourly energy usage data from API."""
        _LOGGER.debug("Starting live energy usage data fetch")

        try:
            now = dt_util.utcnow()

            # Fetch current hour data (last 2 hours to ensure we get the live frame)
            start = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            end = now + timedelta(hours=1)

            format_time = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            url = f"{API_URL}{LIVE_USAGE_ENDPOINT.format(
                start=format_time(start),
                end=format_time(end)
            )}"

            _LOGGER.debug(f"Fetching live usage from {start} to {end}")

            usage_data = await self.api_client.fetch(url)

            if not usage_data or not usage_data.get("frames"):
                _LOGGER.warning("No frames returned for live usage data")
                return None

            # Find the live frame
            live_frame = None
            for frame in usage_data.get("frames", []):
                if frame.get("is_live", False):
                    live_frame = frame
                    break

            if not live_frame:
                # If no live flag, use the most recent frame
                _LOGGER.debug("No live flag found, using most recent frame")
                live_frame = usage_data["frames"][-1] if usage_data["frames"] else None

            if not live_frame:
                return None

            result = {
                "fae_usage": live_frame.get("fae_usage", 0),  # Consumed this hour
                "rae_usage": live_frame.get("rae", 0),  # Produced this hour
                "start": live_frame.get("start"),
                "end": live_frame.get("end"),
                "is_live": live_frame.get("is_live", False),
            }

            _LOGGER.debug(f"Live usage: consumed={result['fae_usage']} kWh, produced={result['rae_usage']} kWh, live={result['is_live']}")

            return result

        except Exception as err:
            _LOGGER.error("Error fetching live energy usage data: %s", err, exc_info=True)
            raise UpdateFailed(f"Error fetching live energy usage data: {err}")

    def schedule_updates(self):
        """Schedule periodic updates every 5 minutes."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

        now = dt_util.now()
        # Schedule next update 5 minutes from now
        next_run = now + timedelta(minutes=5)

        _LOGGER.debug("Scheduling next live usage update at %s",
                     next_run.strftime("%Y-%m-%d %H:%M:%S"))

        self._unsub_timer = async_track_point_in_time(
            self.hass, self._handle_update, dt_util.as_utc(next_run)
        )

    async def _handle_update(self, _):
        """Handle periodic update."""
        _LOGGER.debug("Running scheduled live usage update")
        await self.async_request_refresh()
        self.schedule_updates()
