"""Data update coordinator for Pstryk Energy integration."""
import logging
from datetime import timedelta
import asyncio
import aiohttp
import async_timeout
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util
from homeassistant.helpers.translation import async_get_translations
from .const import (
    API_URL, 
    API_TIMEOUT, 
    BUY_ENDPOINT, 
    SELL_ENDPOINT, 
    DOMAIN, 
    CONF_MQTT_48H_MODE,
    CONF_RETRY_ATTEMPTS,
    CONF_RETRY_DELAY,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_DELAY
)

_LOGGER = logging.getLogger(__name__)

class ExponentialBackoffRetry:
    """Implementacja wykładniczego opóźnienia przy ponawianiu prób."""

    def __init__(self, max_retries=DEFAULT_RETRY_ATTEMPTS, base_delay=DEFAULT_RETRY_DELAY):
        """Inicjalizacja mechanizmu ponowień.
        
        Args:
            max_retries: Maksymalna liczba prób
            base_delay: Podstawowe opóźnienie w sekundach (zwiększane wykładniczo)
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._translations = {}
        
    async def load_translations(self, hass):
        """Załaduj tłumaczenia dla aktualnego języka."""
        try:
            self._translations = await async_get_translations(
                hass, hass.config.language, DOMAIN, ["debug"]
            )
        except Exception as ex:
            _LOGGER.warning("Failed to load translations for retry mechanism: %s", ex)
        
    async def execute(self, func, *args, price_type=None, **kwargs):
        """Wykonaj funkcję z ponawianiem prób.
        
        Args:
            func: Funkcja asynchroniczna do wykonania
            args, kwargs: Argumenty funkcji
            price_type: Typ ceny (do logów)
            
        Returns:
            Wynik funkcji
            
        Raises:
            UpdateFailed: Po wyczerpaniu wszystkich prób
        """
        last_exception = None
        for retry in range(self.max_retries):
            try:
                return await func(*args, **kwargs)
            except Exception as err:
                last_exception = err
                # Nie czekamy po ostatniej próbie
                if retry < self.max_retries - 1:
                    delay = self.base_delay * (2 ** retry)
                    
                    # Użyj przetłumaczonego komunikatu jeśli dostępny
                    retry_msg = self._translations.get(
                        "debug.retry_attempt", 
                        "Retry {retry}/{max_retries} after error: {error} (delay: {delay}s)"
                    ).format(
                        retry=retry + 1, 
                        max_retries=self.max_retries, 
                        error=str(err), 
                        delay=round(delay, 1)
                    )
                    
                    _LOGGER.debug(retry_msg)
                    await asyncio.sleep(delay)
        
        # Jeśli wszystkie próby zawiodły i mamy timeout
        if isinstance(last_exception, asyncio.TimeoutError) and price_type:
            timeout_msg = self._translations.get(
                "debug.timeout_after_retries", 
                "Timeout fetching {price_type} data from API after {retries} retries"
            ).format(price_type=price_type, retries=self.max_retries)
            
            _LOGGER.error(timeout_msg)
            
            api_timeout_msg = self._translations.get(
                "debug.api_timeout_message", 
                "API timeout after {timeout} seconds (tried {retries} times)"
            ).format(timeout=API_TIMEOUT, retries=self.max_retries)
            
            raise UpdateFailed(api_timeout_msg)
        
        # Dla innych typów błędów
        raise last_exception

def convert_price(value):
    """Convert price string to float."""
    try:
        return round(float(str(value).replace(",", ".").strip()), 2)
    except (ValueError, TypeError) as e:
        _LOGGER.warning("Price conversion error: %s", e)
        return None

class PstrykDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch both current price and today's table."""
    
    def __del__(self):
        """Properly clean up when object is deleted."""
        if hasattr(self, '_unsub_hourly') and self._unsub_hourly:
            self._unsub_hourly()
        if hasattr(self, '_unsub_midnight') and self._unsub_midnight:
            self._unsub_midnight()
        if hasattr(self, '_unsub_afternoon') and self._unsub_afternoon:
            self._unsub_afternoon()
            
    def __init__(self, hass, api_key, price_type, mqtt_48h_mode=False, retry_attempts=None, retry_delay=None):
        """Initialize the coordinator."""
        self.hass = hass
        self.api_key = api_key
        self.price_type = price_type
        self.mqtt_48h_mode = mqtt_48h_mode
        self._unsub_hourly = None
        self._unsub_midnight = None
        self._unsub_afternoon = None
        # Inicjalizacja tłumaczeń
        self._translations = {}
        # Track if we had tomorrow prices in last update
        self._had_tomorrow_prices = False
        
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
        
        # Inicjalizacja mechanizmu ponowień z konfigurowalnymi wartościami
        self.retry_mechanism = ExponentialBackoffRetry(max_retries=retry_attempts, base_delay=retry_delay)
        
        # Set a default update interval as a fallback (1 hour)
        # This ensures data is refreshed even if scheduled updates fail
        update_interval = timedelta(hours=1)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{price_type}",
            update_interval=update_interval,  # Add fallback interval
        )

    async def _make_api_request(self, url):
        """Make API request with proper error handling."""
        async with aiohttp.ClientSession() as session:
            async with async_timeout.timeout(API_TIMEOUT):
                resp = await session.get(
                    url,
                    headers={"Authorization": self.api_key, "Accept": "application/json"}
                )
                
                # Obsługa różnych kodów błędu
                if resp.status == 401:
                    error_msg = self._translations.get(
                        "debug.api_error_401", 
                        "API authentication failed for {price_type} - invalid API key"
                    ).format(price_type=self.price_type)
                    _LOGGER.error(error_msg)
                    raise UpdateFailed(self._translations.get(
                        "debug.api_error_401_user", 
                        "API authentication failed - invalid API key"
                    ))
                elif resp.status == 403:
                    error_msg = self._translations.get(
                        "debug.api_error_403", 
                        "API access forbidden for {price_type} - permissions issue"
                    ).format(price_type=self.price_type)
                    _LOGGER.error(error_msg)
                    raise UpdateFailed(self._translations.get(
                        "debug.api_error_403_user", 
                        "API access forbidden - check permissions"
                    ))
                elif resp.status == 404:
                    error_msg = self._translations.get(
                        "debug.api_error_404", 
                        "API endpoint not found for {price_type} - check URL"
                    ).format(price_type=self.price_type)
                    _LOGGER.error(error_msg)
                    raise UpdateFailed(self._translations.get(
                        "debug.api_error_404_user", 
                        "API endpoint not found"
                    ))
                elif resp.status == 429:
                    error_msg = self._translations.get(
                        "debug.api_error_429", 
                        "API rate limit exceeded for {price_type}"
                    ).format(price_type=self.price_type)
                    _LOGGER.error(error_msg)
                    raise UpdateFailed(self._translations.get(
                        "debug.api_error_429_user", 
                        "API rate limit exceeded - try again later"
                    ))
                elif resp.status == 502:
                    error_msg = self._translations.get(
                        "debug.api_error_502", 
                        "API Gateway error (502) for {price_type} - server may be down"
                    ).format(price_type=self.price_type)
                    _LOGGER.error(error_msg)
                    raise UpdateFailed(self._translations.get(
                        "debug.api_error_502_user", 
                        "API Gateway error (502) - server may be down"
                    ))
                elif 500 <= resp.status < 600:
                    error_msg = self._translations.get(
                        "debug.api_error_5xx", 
                        "API server error ({status}) for {price_type} - server issue"
                    ).format(status=resp.status, price_type=self.price_type)
                    _LOGGER.error(error_msg)
                    raise UpdateFailed(self._translations.get(
                        "debug.api_error_5xx_user", 
                        "API server error ({status}) - server issue"
                    ).format(status=resp.status))
                elif resp.status != 200:
                    error_text = await resp.text()
                    # Pokaż tylko pierwsze 50 znaków błędu dla krótszego logu
                    short_error = error_text[:50] + ("..." if len(error_text) > 50 else "")
                    error_msg = self._translations.get(
                        "debug.api_error_generic", 
                        "API error {status} for {price_type}: {error}"
                    ).format(status=resp.status, price_type=self.price_type, error=short_error)
                    _LOGGER.error(error_msg)
                    raise UpdateFailed(self._translations.get(
                        "debug.api_error_generic_user", 
                        "API error {status}: {error}"
                    ).format(status=resp.status, error=short_error))
                
                return await resp.json()

    async def _check_and_publish_mqtt(self, new_data):
        """Check if we should publish to MQTT after update."""
        if not self.mqtt_48h_mode:
            return
            
        now = dt_util.now()
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        
        # Check if tomorrow prices are available in new data
        all_prices = new_data.get("prices", [])
        tomorrow_prices = [p for p in all_prices if p["start"].startswith(tomorrow)]
        has_tomorrow_prices = len(tomorrow_prices) > 0
        
        # If we didn't have tomorrow prices before, but now we do, publish to MQTT
        if not self._had_tomorrow_prices and has_tomorrow_prices:
            _LOGGER.info("Tomorrow prices detected for %s, triggering MQTT publish", self.price_type)
            
            # Find our config entry
            entry_id = None
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                if entry.data.get("api_key") == self.api_key:
                    entry_id = entry.entry_id
                    break
                    
            if entry_id:
                # Check if both coordinators are initialized before publishing
                buy_coordinator = self.hass.data[DOMAIN].get(f"{entry_id}_buy")
                sell_coordinator = self.hass.data[DOMAIN].get(f"{entry_id}_sell")
                
                if not buy_coordinator or not sell_coordinator:
                    _LOGGER.debug("Coordinators not yet initialized, skipping MQTT publish for now")
                    # Don't update _had_tomorrow_prices so we'll try again on next update
                    return
                
                # Get MQTT topics from config
                from .const import CONF_MQTT_TOPIC_BUY, CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_SELL
                entry = self.hass.config_entries.async_get_entry(entry_id)
                mqtt_topic_buy = entry.options.get(CONF_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_BUY)
                mqtt_topic_sell = entry.options.get(CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_SELL)
                
                # Wait a moment for both coordinators to update
                await asyncio.sleep(5)
                
                # Publish to MQTT
                from .mqtt_common import publish_mqtt_prices
                success = await publish_mqtt_prices(self.hass, entry_id, mqtt_topic_buy, mqtt_topic_sell)
                
                if success:
                    _LOGGER.info("Successfully published 48h prices to MQTT after detecting tomorrow prices")
                else:
                    _LOGGER.error("Failed to publish to MQTT after detecting tomorrow prices")
        
        # Update state for next check
        self._had_tomorrow_prices = has_tomorrow_prices

    async def _async_update_data(self):
        """Fetch 48h of frames and extract current + today's list."""
        _LOGGER.debug("Starting %s price update (48h mode: %s)", self.price_type, self.mqtt_48h_mode)
        
        # Store the previous data for fallback
        previous_data = None
        if hasattr(self, 'data') and self.data:
            previous_data = self.data.copy() if self.data else None
            if previous_data:
                # Oznacz jako dane z cache, jeśli będziemy ich używać
                previous_data["is_cached"] = True
        
        today_local = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
        window_end_local = today_local + timedelta(days=2)
        start_utc = dt_util.as_utc(today_local).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = dt_util.as_utc(window_end_local).strftime("%Y-%m-%dT%H:%M:%SZ")

        endpoint_tpl = BUY_ENDPOINT if self.price_type == "buy" else SELL_ENDPOINT
        endpoint = endpoint_tpl.format(start=start_utc, end=end_utc)
        url = f"{API_URL}{endpoint}"
        
        _LOGGER.debug("Requesting %s data from %s", self.price_type, url)

        try:
            # Załaduj tłumaczenia dla mechanizmu ponowień
            await self.retry_mechanism.load_translations(self.hass)
            
            # Załaduj tłumaczenia dla koordynatora
            try:
                self._translations = await async_get_translations(
                    self.hass, self.hass.config.language, DOMAIN, ["debug"]
                )
            except Exception as ex:
                _LOGGER.warning("Failed to load translations for coordinator: %s", ex)
            
            # Użyj mechanizmu ponowień z parametrem price_type
            # Nie potrzebujemy łapać asyncio.TimeoutError tutaj, ponieważ
            # jest już obsługiwany w execute() z odpowiednimi tłumaczeniami
            data = await self.retry_mechanism.execute(
                self._make_api_request, 
                url, 
                price_type=self.price_type
            )

            frames = data.get("frames", [])
            if not frames:
                _LOGGER.warning("No frames returned for %s prices", self.price_type)
                
            now_utc = dt_util.utcnow()
            prices = []
            current_price = None

            for f in frames:
                val = convert_price(f.get("price_gross"))
                if val is None:
                    continue
                start = dt_util.parse_datetime(f["start"])
                end = dt_util.parse_datetime(f["end"])
                
                # Weryfikacja poprawności dat
                if not start or not end:
                    _LOGGER.warning("Invalid datetime format in frames for %s", self.price_type)
                    continue
                    
                local_start = dt_util.as_local(start).strftime("%Y-%m-%dT%H:%M:%S")
                prices.append({"start": local_start, "price": val})
                if start <= now_utc < end:
                    current_price = val

            # only today's entries
            today_str = today_local.strftime("%Y-%m-%d")
            prices_today = [p for p in prices if p["start"].startswith(today_str)]
            
            _LOGGER.debug("Successfully fetched %s price data: current=%s, today_prices=%d, total_prices=%d", 
                         self.price_type, current_price, len(prices_today), len(prices))

            new_data = {
                "prices_today": prices_today,
                "prices": prices,
                "current": current_price,
                "is_cached": False,  # Dane bezpośrednio z API
            }
            
            # Check if we should publish to MQTT (only for first coordinator that detects new tomorrow prices)
            if self.mqtt_48h_mode:
                await self._check_and_publish_mqtt(new_data)
            
            return new_data

        except aiohttp.ClientError as err:
            error_msg = self._translations.get(
                "debug.network_error", 
                "Network error fetching {price_type} data: {error}"
            ).format(price_type=self.price_type, error=str(err))
            _LOGGER.error(error_msg)
            
            if previous_data:
                cache_msg = self._translations.get(
                    "debug.using_cache", 
                    "Using cached data from previous update due to API failure"
                )
                _LOGGER.warning(cache_msg)
                return previous_data
                
            raise UpdateFailed(self._translations.get(
                "debug.network_error_user", 
                "Network error: {error}"
            ).format(error=err))
            
        except Exception as err:
            error_msg = self._translations.get(
                "debug.unexpected_error", 
                "Unexpected error fetching {price_type} data: {error}"
            ).format(price_type=self.price_type, error=str(err))
            _LOGGER.exception(error_msg)
            
            if previous_data:
                cache_msg = self._translations.get(
                    "debug.using_cache", 
                    "Using cached data from previous update due to API failure"
                )
                _LOGGER.warning(cache_msg)
                return previous_data
                
            raise UpdateFailed(self._translations.get(
                "debug.unexpected_error_user", 
                "Error: {error}"
            ).format(error=err))

    def schedule_hourly_update(self):
        """Schedule next refresh 1 min after each full hour."""
        if self._unsub_hourly:
            self._unsub_hourly()
            self._unsub_hourly = None
            
        now = dt_util.now()
        # Keep original timing: 1 minute past the hour
        next_run = (now.replace(minute=0, second=0, microsecond=0)
                    + timedelta(hours=1, minutes=1))
        
        _LOGGER.debug("Scheduling next hourly update for %s at %s", 
                     self.price_type, next_run.strftime("%Y-%m-%d %H:%M:%S"))
                     
        self._unsub_hourly = async_track_point_in_time(
            self.hass, self._handle_hourly_update, dt_util.as_utc(next_run)
        )

    async def _handle_hourly_update(self, _):
        """Handle hourly update."""
        _LOGGER.debug("Running scheduled hourly update for %s", self.price_type)
        await self.async_request_refresh()
        self.schedule_hourly_update()

    def schedule_midnight_update(self):
        """Schedule next refresh 1 min after local midnight."""
        if self._unsub_midnight:
            self._unsub_midnight()
            self._unsub_midnight = None
            
        now = dt_util.now()
        # Keep original timing: 1 minute past midnight
        next_mid = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
        
        _LOGGER.debug("Scheduling next midnight update for %s at %s", 
                     self.price_type, next_mid.strftime("%Y-%m-%d %H:%M:%S"))
                     
        self._unsub_midnight = async_track_point_in_time(
            self.hass, self._handle_midnight_update, dt_util.as_utc(next_mid)
        )

    async def _handle_midnight_update(self, _):
        """Handle midnight update."""
        _LOGGER.debug("Running scheduled midnight update for %s", self.price_type)
        await self.async_request_refresh()
        self.schedule_midnight_update()
        
    def schedule_afternoon_update(self):
        """Schedule frequent updates between 14:00-15:00 for 48h mode."""
        if self._unsub_afternoon:
            self._unsub_afternoon()
            self._unsub_afternoon = None
            
        if not self.mqtt_48h_mode:
            _LOGGER.debug("Afternoon updates not scheduled for %s - 48h mode is disabled", self.price_type)
            return
            
        now = dt_util.now()
        
        # Determine next check time
        # If we're before 14:00, start at 14:00
        if now.hour < 14:
            next_check = now.replace(hour=14, minute=0, second=0, microsecond=0)
        # If we're between 14:00-15:00, find next 15-minute slot
        elif now.hour == 14:
            # Calculate minutes to next 15-minute mark
            current_minutes = now.minute
            if current_minutes < 15:
                next_minutes = 15
            elif current_minutes < 30:
                next_minutes = 30
            elif current_minutes < 45:
                next_minutes = 45
            else:
                # Move to 15:00
                next_check = now.replace(hour=15, minute=0, second=0, microsecond=0)
                next_minutes = None
                
            if next_minutes is not None:
                next_check = now.replace(minute=next_minutes, second=0, microsecond=0)
        # If we're at 15:00 or later, schedule for tomorrow 14:00
        else:
            next_check = (now + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
        
        # Make sure next_check is in the future
        if next_check <= now:
            # This shouldn't happen, but just in case
            next_check = next_check + timedelta(minutes=15)
        
        _LOGGER.info("Scheduling afternoon update check for %s at %s (48h mode, checking every 15min between 14:00-15:00)", 
                     self.price_type, next_check.strftime("%Y-%m-%d %H:%M:%S"))
                     
        self._unsub_afternoon = async_track_point_in_time(
            self.hass, self._handle_afternoon_update, dt_util.as_utc(next_check)
        )

    async def _handle_afternoon_update(self, _):
        """Handle afternoon update for 48h mode."""
        now = dt_util.now()
        _LOGGER.debug("Running scheduled afternoon update check for %s at %s", 
                     self.price_type, now.strftime("%H:%M"))
        
        # Perform the update
        await self.async_request_refresh()
        
        # Schedule next check only if we're still in the 14:00-15:00 window
        if now.hour < 15:
            self.schedule_afternoon_update()
        else:
            # We've finished the afternoon window, schedule for tomorrow
            _LOGGER.info("Finished afternoon update window for %s, next cycle tomorrow", self.price_type)
            self.schedule_afternoon_update()
        
    def unschedule_all_updates(self):
        """Unschedule all updates."""
        if self._unsub_hourly:
            self._unsub_hourly()
            self._unsub_hourly = None
        if self._unsub_midnight:
            self._unsub_midnight()
            self._unsub_midnight = None
        if self._unsub_afternoon:
            self._unsub_afternoon()
            self._unsub_afternoon = None
