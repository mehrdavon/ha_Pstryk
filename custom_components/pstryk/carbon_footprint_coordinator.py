"""Pstryk carbon footprint data coordinator."""
import logging
from datetime import timedelta
import async_timeout
import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_track_point_in_time
import homeassistant.util.dt as dt_util
from .const import (
    DOMAIN,
    API_URL,
    CARBON_FOOTPRINT_ENDPOINT,
    API_TIMEOUT,
    CONF_RETRY_ATTEMPTS,
    CONF_RETRY_DELAY,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_DELAY
)
from .update_coordinator import ExponentialBackoffRetry

_LOGGER = logging.getLogger(__name__)

class PstrykCarbonFootprintUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Pstryk carbon footprint data."""

    def __init__(self, hass: HomeAssistant, api_key: str, retry_attempts=None, retry_delay=None):
        """Initialize."""
        self.api_key = api_key
        self._unsub_hourly = None
        self._unsub_midnight = None

        # Get retry configuration from entry options
        if retry_attempts is None or retry_delay is None:
            # Try to find the config entry to get retry options
            for entry in hass.config_entries.async_entries(DOMAIN):
                if entry.data.get("api_key") == api_key:
                    retry_attempts = entry.options.get(CONF_RETRY_ATTEMPTS, DEFAULT_RETRY_ATTEMPTS)
                    retry_delay = entry.options.get(CONF_RETRY_DELAY, DEFAULT_RETRY_DELAY)
                    break
            else:
                # Use defaults if no matching entry found
                retry_attempts = DEFAULT_RETRY_ATTEMPTS
                retry_delay = DEFAULT_RETRY_DELAY

        # Initialize retry mechanism with configurable values
        self.retry_mechanism = ExponentialBackoffRetry(max_retries=retry_attempts, base_delay=retry_delay)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_carbon_footprint",
            update_interval=timedelta(hours=1),
        )

        # Schedule hourly updates
        self.schedule_hourly_update()
        # Schedule midnight updates
        self.schedule_midnight_update()


    async def _async_update_data(self):
        """Fetch carbon footprint data from API."""
        _LOGGER.debug("Starting carbon footprint data fetch")

        try:
            now = dt_util.utcnow()

            # Since we use for_tz=Europe/Warsaw in the API, we can use simple UTC times
            # The API will handle the timezone conversion for us

            # For daily data - just use UTC dates
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            tomorrow_start = today_start + timedelta(days=1)
            yesterday_start = today_start - timedelta(days=1)
            day_after_tomorrow = tomorrow_start + timedelta(days=1)

            # For monthly data - current month
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if now.month == 12:
                next_month_start = month_start.replace(year=now.year + 1, month=1)
            else:
                next_month_start = month_start.replace(month=now.month + 1)

            # For yearly data - current year
            year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            next_year_start = year_start.replace(year=now.year + 1)

            # Format times for API (just use UTC)
            format_time = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            # Fetch data for all resolutions
            data = {}

            # Fetch daily data with a 3-day window to ensure we get all data
            daily_url = f"{API_URL}{CARBON_FOOTPRINT_ENDPOINT.format(resolution='day', start=format_time(yesterday_start), end=format_time(day_after_tomorrow))}"

            _LOGGER.debug(f"Fetching daily carbon footprint data from {yesterday_start} to {day_after_tomorrow}")
            daily_data = await self._fetch_data(daily_url)

            if daily_data:
                data["daily"] = self._process_daily_data(daily_data)

            # Fetch monthly data
            monthly_url = f"{API_URL}{CARBON_FOOTPRINT_ENDPOINT.format(resolution='month', start=format_time(month_start), end=format_time(next_month_start))}"

            _LOGGER.debug(f"Fetching monthly carbon footprint data for {month_start.strftime('%B %Y')}")
            monthly_data = await self._fetch_data(monthly_url)

            if monthly_data:
                data["monthly"] = self._process_monthly_data(monthly_data)

            # Fetch yearly data using month resolution
            yearly_url = f"{API_URL}{CARBON_FOOTPRINT_ENDPOINT.format(resolution='month', start=format_time(year_start), end=format_time(next_year_start))}"

            _LOGGER.debug(f"Fetching yearly carbon footprint data for {year_start.year}")
            yearly_data = await self._fetch_data(yearly_url)

            if yearly_data:
                data["yearly"] = self._process_yearly_data(yearly_data)

            _LOGGER.debug("Successfully fetched carbon footprint data")
            return data

        except Exception as err:
            _LOGGER.error("Error fetching carbon footprint data: %s", err)
            raise UpdateFailed(f"Error fetching carbon footprint data: {err}")


    def _process_monthly_data(self, data):
        """Simple monthly data processor - just take the first frame since we requested current month only."""
        _LOGGER.info("Processing monthly carbon footprint data")

        result = {
            "frame": {},
            "total_carbon_footprint": 0
        }

        # Get data from first frame (should be current month)
        if data and data.get("frames") and data["frames"]:
            frame = data["frames"][0]
            result["frame"] = frame
            result["total_carbon_footprint"] = frame.get("carbon_footprint", 0)
            _LOGGER.info(f"Monthly carbon footprint: {result['total_carbon_footprint']} gCO2eq")

        return result


    def _process_daily_data(self, data):
        """Simple daily data processor - find the live frame or current day."""
        _LOGGER.info("=== SIMPLE DAILY CARBON FOOTPRINT PROCESSOR ===")

        result = {
            "frame": {},
            "total_carbon_footprint": 0
        }

        # Find the live frame (current day)
        if data and data.get("frames"):
            _LOGGER.info(f"Processing {len(data['frames'])} carbon footprint frames")

            for i, frame in enumerate(data["frames"]):
                _LOGGER.info(f"Frame {i}: start={frame.get('start')}, "
                           f"is_live={frame.get('is_live', False)}, "
                           f"carbon_footprint={frame.get('carbon_footprint')}")

                # Use the frame marked as is_live
                if frame.get("is_live", False):
                    result["frame"] = frame
                    result["total_carbon_footprint"] = frame.get("carbon_footprint", 0)
                    _LOGGER.info(f"*** FOUND LIVE CARBON FOOTPRINT FRAME: {result['total_carbon_footprint']} gCO2eq ***")
                    break

        _LOGGER.info(f"=== FINAL RESULT: carbon_footprint={result['total_carbon_footprint']} ===")
        return result


    def _process_yearly_data(self, data):
        """Simple yearly data processor - sum all months for the year."""
        _LOGGER.info("Processing yearly carbon footprint data")

        # Initialize totals
        total_carbon_footprint = 0

        # Sum up all months
        if data and data.get("frames"):
            for frame in data["frames"]:
                total_carbon_footprint += frame.get("carbon_footprint", 0)
            _LOGGER.info(f"Yearly carbon footprint total: {total_carbon_footprint} gCO2eq")

        return {
            "frame": {},  # No single frame for yearly data
            "total_carbon_footprint": total_carbon_footprint
        }


    async def _fetch_data(self, url):
        """Fetch data from the API using retry mechanism."""
        async def _make_api_request():
            """Make the actual API request."""
            _LOGGER.info(f"Fetching carbon footprint data from URL: {url}")
            async with aiohttp.ClientSession() as session:
                async with async_timeout.timeout(API_TIMEOUT):
                    resp = await session.get(
                        url,
                        headers={
                            "Authorization": self.api_key,
                            "Accept": "application/json"
                        }
                    )

                    if resp.status != 200:
                        error_text = await resp.text()
                        _LOGGER.error("API error %s for URL %s: %s", resp.status, url, error_text)
                        raise UpdateFailed(f"API error {resp.status}: {error_text}")

                    data = await resp.json()
                    _LOGGER.info(f"API response data: {data}")
                    return data

        try:
            # Load translations for retry mechanism
            await self.retry_mechanism.load_translations(self.hass)

            # Use retry mechanism to fetch data
            return await self.retry_mechanism.execute(_make_api_request)
        except Exception as e:
            _LOGGER.error("Error fetching from %s after retries: %s", url, e)
            return None


    def schedule_midnight_update(self):
        """Schedule midnight updates for daily reset."""
        if hasattr(self, '_unsub_midnight'):
            if self._unsub_midnight:
                self._unsub_midnight()
                self._unsub_midnight = None
        else:
            self._unsub_midnight = None

        now = dt_util.now()
        # Schedule update shortly after local midnight (which is when API data resets)
        # The API resets at 22:00 UTC (summer) or 23:00 UTC (winter) = 00:00 local Poland time
        next_mid = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)

        _LOGGER.debug("Scheduling next midnight carbon footprint update at %s",
                     next_mid.strftime("%Y-%m-%d %H:%M:%S"))

        self._unsub_midnight = async_track_point_in_time(
            self.hass, self._handle_midnight_update, dt_util.as_utc(next_mid)
        )

    async def _handle_midnight_update(self, _):
        """Handle midnight update."""
        _LOGGER.debug("Running scheduled midnight carbon footprint update")
        await self.async_refresh()
        self.schedule_midnight_update()

    def schedule_hourly_update(self):
        """Schedule hourly updates."""
        if self._unsub_hourly:
            self._unsub_hourly()
            self._unsub_hourly = None

        now = dt_util.now()
        next_run = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, minutes=1))

        _LOGGER.debug("Scheduling next hourly carbon footprint update at %s",
                     next_run.strftime("%Y-%m-%d %H:%M:%S"))

        self._unsub_hourly = async_track_point_in_time(
            self.hass, self._handle_hourly_update, dt_util.as_utc(next_run)
        )

    async def _handle_hourly_update(self, now):
        """Handle the hourly update."""
        _LOGGER.debug("Triggering hourly carbon footprint update")
        await self.async_refresh()

        # Schedule the next update
        self.schedule_hourly_update()

    async def async_shutdown(self):
        """Clean up on shutdown."""
        if self._unsub_hourly:
            self._unsub_hourly()
            self._unsub_hourly = None
        if self._unsub_midnight:
            self._unsub_midnight()
            self._unsub_midnight = None