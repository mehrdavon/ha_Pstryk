"""Pstryk energy cost data coordinator."""
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
    ENERGY_COST_ENDPOINT,
    ENERGY_USAGE_ENDPOINT,
    API_TIMEOUT,
    CONF_RETRY_ATTEMPTS,
    CONF_RETRY_DELAY,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_DELAY
)
from .update_coordinator import ExponentialBackoffRetry

_LOGGER = logging.getLogger(__name__)

class PstrykCostDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Pstryk energy cost data."""
    
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
                name=f"{DOMAIN}_cost",
                update_interval=timedelta(hours=1),
            )
            
            # Schedule hourly updates
            self.schedule_hourly_update()
            # Schedule midnight updates
            self.schedule_midnight_update()

        
    async def _async_update_data(self):
            """Fetch energy cost data from API."""
            _LOGGER.debug("Starting energy cost and usage data fetch")
            
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
                daily_cost_url = f"{API_URL}{ENERGY_COST_ENDPOINT.format(
                    resolution='day', 
                    start=format_time(yesterday_start), 
                    end=format_time(day_after_tomorrow)
                )}"
                daily_usage_url = f"{API_URL}{ENERGY_USAGE_ENDPOINT.format(
                    resolution='day', 
                    start=format_time(yesterday_start), 
                    end=format_time(day_after_tomorrow)
                )}"
                
                _LOGGER.debug(f"Fetching daily data from {yesterday_start} to {day_after_tomorrow}")
                daily_cost_data = await self._fetch_data(daily_cost_url)
                daily_usage_data = await self._fetch_data(daily_usage_url)
                
                if daily_cost_data and daily_usage_data:
                    data["daily"] = self._process_daily_data_simple(daily_cost_data, daily_usage_data)
                
                # Fetch monthly data
                monthly_cost_url = f"{API_URL}{ENERGY_COST_ENDPOINT.format(
                    resolution='month', 
                    start=format_time(month_start), 
                    end=format_time(next_month_start)
                )}"
                monthly_usage_url = f"{API_URL}{ENERGY_USAGE_ENDPOINT.format(
                    resolution='month', 
                    start=format_time(month_start), 
                    end=format_time(next_month_start)
                )}"
                
                _LOGGER.debug(f"Fetching monthly data for {month_start.strftime('%B %Y')}")
                monthly_cost_data = await self._fetch_data(monthly_cost_url)
                monthly_usage_data = await self._fetch_data(monthly_usage_url)
                
                if monthly_cost_data and monthly_usage_data:
                    data["monthly"] = self._process_monthly_data_simple(monthly_cost_data, monthly_usage_data)
                    
                # Fetch yearly data using month resolution
                yearly_cost_url = f"{API_URL}{ENERGY_COST_ENDPOINT.format(
                    resolution='month', 
                    start=format_time(year_start), 
                    end=format_time(next_year_start)
                )}"
                yearly_usage_url = f"{API_URL}{ENERGY_USAGE_ENDPOINT.format(
                    resolution='month', 
                    start=format_time(year_start), 
                    end=format_time(next_year_start)
                )}"
                
                _LOGGER.debug(f"Fetching yearly data for {year_start.year}")
                yearly_cost_data = await self._fetch_data(yearly_cost_url)
                yearly_usage_data = await self._fetch_data(yearly_usage_url)
                
                if yearly_cost_data and yearly_usage_data:
                    data["yearly"] = self._process_yearly_data_simple(yearly_cost_data, yearly_usage_data)
                    
                _LOGGER.debug("Successfully fetched energy cost and usage data")
                return data
                
            except Exception as err:
                _LOGGER.error("Error fetching energy cost data: %s", err)
                raise UpdateFailed(f"Error fetching energy cost data: {err}")

    
    def _process_monthly_data_simple(self, cost_data, usage_data):
            """Simple monthly data processor - just take the first frame since we requested current month only."""
            _LOGGER.info("Processing monthly data - simple version")
            
            result = {
                "frame": {},
                "total_balance": 0,
                "total_sold": 0,
                "total_cost": 0,
                "fae_usage": 0,
                "rae_usage": 0
            }
            
            # Get cost data from first frame (should be current month)
            if cost_data and cost_data.get("frames") and cost_data["frames"]:
                frame = cost_data["frames"][0]
                result["frame"] = frame
                result["total_balance"] = frame.get("energy_balance_value", 0)
                result["total_sold"] = frame.get("energy_sold_value", 0)
                result["total_cost"] = abs(frame.get("fae_cost", 0))
                _LOGGER.info(f"Monthly cost data: balance={result['total_balance']}, "
                            f"sold={result['total_sold']}, cost={result['total_cost']}")
            
            # Get usage data from first frame (should be current month)
            if usage_data and usage_data.get("frames") and usage_data["frames"]:
                frame = usage_data["frames"][0]
                result["fae_usage"] = frame.get("fae_usage", 0)
                result["rae_usage"] = frame.get("rae", 0)
                _LOGGER.info(f"Monthly usage data: fae={result['fae_usage']}, rae={result['rae_usage']}")
            
            return result


    def _process_daily_data_simple(self, cost_data, usage_data):
            """Simple daily data processor - directly use API values without complex logic."""
            _LOGGER.info("=== SIMPLE DAILY DATA PROCESSOR ===")
            
            result = {
                "frame": {},
                "total_balance": 0,
                "total_sold": 0,
                "total_cost": 0,
                "fae_usage": 0,
                "rae_usage": 0
            }
            
            live_date = None
            
            # Find the live usage frame (current day)
            if usage_data and usage_data.get("frames"):
                _LOGGER.info(f"Processing {len(usage_data['frames'])} usage frames")
                
                for i, frame in enumerate(usage_data["frames"]):
                    _LOGGER.info(f"Frame {i}: start={frame.get('start')}, "
                               f"is_live={frame.get('is_live', False)}, "
                               f"fae_usage={frame.get('fae_usage')}, "
                               f"rae={frame.get('rae')}")
                    
                    # Use the frame marked as is_live
                    if frame.get("is_live", False):
                        result["fae_usage"] = frame.get("fae_usage", 0)
                        result["rae_usage"] = frame.get("rae", 0)
                        _LOGGER.info(f"*** FOUND LIVE FRAME: fae_usage={result['fae_usage']}, rae={result['rae_usage']} ***")
                        
                        # Store the live frame's date info for cost matching
                        live_start = frame.get("start")
                        if live_start:
                            # Extract the date part for matching with cost data
                            live_date = live_start.split("T")[0]
                            _LOGGER.info(f"Live frame date: {live_date}")
                        break
            
            # Find the corresponding cost frame for the same day
            if cost_data and cost_data.get("frames") and live_date:
                _LOGGER.info(f"Processing {len(cost_data['frames'])} cost frames, looking for date: {live_date}")
                
                # Look for the cost frame that matches the live usage frame's date
                for frame in cost_data["frames"]:
                    frame_start = frame.get("start", "")
                    frame_date = frame_start.split("T")[0] if frame_start else ""
                    
                    _LOGGER.info(f"Checking cost frame: start={frame_start}, date={frame_date}, "
                               f"balance={frame.get('energy_balance_value', 0)}, "
                               f"cost={frame.get('fae_cost', 0)}")
                    
                    # Match the date with the live frame's date
                    if frame_date == live_date:
                        result["frame"] = frame
                        result["total_balance"] = frame.get("energy_balance_value", 0)
                        result["total_sold"] = frame.get("energy_sold_value", 0)
                        result["total_cost"] = abs(frame.get("fae_cost", 0))
                        _LOGGER.info(f"*** MATCHED cost frame for date {live_date}: balance={result['total_balance']}, "
                                   f"cost={result['total_cost']}, sold={result['total_sold']} ***")
                        break
                else:
                    _LOGGER.warning(f"No cost frame found matching live date {live_date}")
            elif not live_date:
                _LOGGER.warning("No live frame found in usage data, cannot match cost frame")
            
            _LOGGER.info(f"=== FINAL RESULT: fae_usage={result['fae_usage']}, "
                        f"rae_usage={result['rae_usage']}, "
                        f"balance={result['total_balance']}, "
                        f"cost={result['total_cost']}, "
                        f"sold={result['total_sold']} ===")
            return result


    

    def _process_yearly_data_simple(self, cost_data, usage_data):
            """Simple yearly data processor - sum all months for the year."""
            _LOGGER.info("Processing yearly data - simple version")
            
            # Initialize totals
            total_balance = 0
            total_sold = 0
            total_cost = 0
            fae_usage = 0
            rae_usage = 0
            
            # Sum up all months from cost data
            if cost_data and cost_data.get("frames"):
                for frame in cost_data["frames"]:
                    total_balance += frame.get("energy_balance_value", 0)
                    total_sold += frame.get("energy_sold_value", 0)
                    total_cost += abs(frame.get("fae_cost", 0))
                _LOGGER.info(f"Yearly cost totals: balance={total_balance}, "
                            f"sold={total_sold}, cost={total_cost}")
                    
            # Sum up all months from usage data
            if usage_data and usage_data.get("frames"):
                for frame in usage_data["frames"]:
                    fae_usage += frame.get("fae_usage", 0)
                    rae_usage += frame.get("rae", 0)  # Note: API uses 'rae' not 'rae_usage'
                _LOGGER.info(f"Yearly usage totals: fae={fae_usage}, rae={rae_usage}")
                    
            return {
                "frame": {},  # No single frame for yearly data
                "total_balance": total_balance,
                "total_sold": total_sold,
                "total_cost": total_cost,
                "fae_usage": fae_usage,
                "rae_usage": rae_usage
            }

    
    def _extract_frame_values(self, cost_frame, usage_frame):
        """Extract and combine values from cost and usage frames."""
        result = {}
        
        # Get cost data
        if cost_frame:
            result["total_balance"] = cost_frame.get("energy_balance_value", 0)
            result["total_sold"] = cost_frame.get("energy_sold_value", 0)
            result["total_cost"] = abs(cost_frame.get("fae_cost", 0))
            result["frame"] = cost_frame
        else:
            result["total_balance"] = 0
            result["total_sold"] = 0
            result["total_cost"] = 0
            result["frame"] = {}
            
        # Get usage data
        if usage_frame:
            result["fae_usage"] = usage_frame.get("fae_usage", 0)
            result["rae_usage"] = usage_frame.get("rae", 0)  # Note: API uses 'rae' not 'rae_usage'
        else:
            result["fae_usage"] = 0
            result["rae_usage"] = 0
            
        return result
    
    async def _fetch_data(self, url):
            """Fetch data from the API using retry mechanism."""
            async def _make_api_request():
                """Make the actual API request."""
                _LOGGER.info(f"Fetching data from URL: {url}")
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
        
        _LOGGER.debug("Scheduling next midnight cost update at %s", 
                     next_mid.strftime("%Y-%m-%d %H:%M:%S"))
                     
        self._unsub_midnight = async_track_point_in_time(
            self.hass, self._handle_midnight_update, dt_util.as_utc(next_mid)
        )
        
    async def _handle_midnight_update(self, _):
        """Handle midnight update."""
        _LOGGER.debug("Running scheduled midnight cost update")
        await self.async_refresh()
        self.schedule_midnight_update()
        
    def schedule_hourly_update(self):
        """Schedule hourly updates."""
        if self._unsub_hourly:
            self._unsub_hourly()
            self._unsub_hourly = None
            
        now = dt_util.now()
        next_run = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, minutes=1))
        
        _LOGGER.debug("Scheduling next hourly cost update at %s", 
                     next_run.strftime("%Y-%m-%d %H:%M:%S"))
                     
        self._unsub_hourly = async_track_point_in_time(
            self.hass, self._handle_hourly_update, dt_util.as_utc(next_run)
        )
        
    async def _handle_hourly_update(self, now):
        """Handle the hourly update."""
        _LOGGER.debug("Triggering hourly cost update")
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
