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
from .const import API_URL, API_TIMEOUT, BUY_ENDPOINT, SELL_ENDPOINT, DOMAIN

_LOGGER = logging.getLogger(__name__)

class ExponentialBackoffRetry:
    """Implementacja wykładniczego opóźnienia przy ponawianiu prób."""

    def __init__(self, max_retries=3, base_delay=20.0):
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
            
    def __init__(self, hass, api_key, price_type):
        """Initialize the coordinator."""
        self.hass = hass
        self.api_key = api_key
        self.price_type = price_type
        self._unsub_hourly = None
        self._unsub_midnight = None
        # Inicjalizacja mechanizmu ponowień z 3 próbami i dłuższym odstępem
        self.retry_mechanism = ExponentialBackoffRetry(max_retries=3, base_delay=20.0)
        
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
                    _LOGGER.error("API authentication failed for %s - invalid API key", self.price_type)
                    raise UpdateFailed("API authentication failed - invalid API key")
                elif resp.status == 403:
                    _LOGGER.error("API access forbidden for %s - permissions issue", self.price_type)
                    raise UpdateFailed("API access forbidden - check permissions")
                elif resp.status == 404:
                    _LOGGER.error("API endpoint not found for %s - check URL", self.price_type)
                    raise UpdateFailed("API endpoint not found")
                elif resp.status == 429:
                    _LOGGER.error("API rate limit exceeded for %s", self.price_type)
                    raise UpdateFailed("API rate limit exceeded - try again later")
                elif resp.status != 200:
                    error_text = await resp.text()
                    _LOGGER.error("API error %s for %s: %s", resp.status, self.price_type, error_text)
                    raise UpdateFailed(f"API error {resp.status}: {error_text[:100]}")
                
                return await resp.json()

    async def _async_update_data(self):
        """Fetch 48h of frames and extract current + today's list."""
        _LOGGER.debug("Starting %s price update", self.price_type)
        
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
            
            _LOGGER.debug("Successfully fetched %s price data: current=%s, today_prices=%d", 
                         self.price_type, current_price, len(prices_today))

            return {
                "prices_today": prices_today,
                "prices": prices,
                "current": current_price,
                "is_cached": False,  # Dane bezpośrednio z API
            }

        except aiohttp.ClientError as err:
            _LOGGER.error("Network error fetching %s data: %s", self.price_type, str(err))
            if previous_data:
                _LOGGER.warning("Using cached data from previous update due to API failure")
                return previous_data
            raise UpdateFailed(f"Network error: {err}")
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching %s data: %s", self.price_type, str(err))
            if previous_data:
                _LOGGER.warning("Using cached data from previous update due to API failure")
                return previous_data
            raise UpdateFailed(f"Error: {err}")

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
                     self.price_type, next_run.isoformat())
                     
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
                     self.price_type, next_mid.isoformat())
                     
        self._unsub_midnight = async_track_point_in_time(
            self.hass, self._handle_midnight_update, dt_util.as_utc(next_mid)
        )

    async def _handle_midnight_update(self, _):
        """Handle midnight update."""
        _LOGGER.debug("Running scheduled midnight update for %s", self.price_type)
        await self.async_request_refresh()
        self.schedule_midnight_update()
