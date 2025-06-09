"""Sensor platform for Pstryk Energy integration."""
import logging
import asyncio
from datetime import datetime, timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from .update_coordinator import PstrykDataUpdateCoordinator
from .const import (
    DOMAIN, 
    CONF_MQTT_48H_MODE,
    CONF_RETRY_ATTEMPTS,
    CONF_RETRY_DELAY,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_DELAY
)
from homeassistant.helpers.translation import async_get_translations

_LOGGER = logging.getLogger(__name__)

# Store translations globally to avoid reloading for each sensor
_TRANSLATIONS_CACHE = {}

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up the two Pstryk sensors via the coordinator."""
    api_key = hass.data[DOMAIN][entry.entry_id]["api_key"]
    buy_top = entry.options.get("buy_top", entry.data.get("buy_top", 5))
    sell_top = entry.options.get("sell_top", entry.data.get("sell_top", 5))
    buy_worst = entry.options.get("buy_worst", entry.data.get("buy_worst", 5))
    sell_worst = entry.options.get("sell_worst", entry.data.get("sell_worst", 5))
    mqtt_48h_mode = entry.options.get(CONF_MQTT_48H_MODE, False)
    retry_attempts = entry.options.get(CONF_RETRY_ATTEMPTS, DEFAULT_RETRY_ATTEMPTS)
    retry_delay = entry.options.get(CONF_RETRY_DELAY, DEFAULT_RETRY_DELAY)

    _LOGGER.debug("Setting up Pstryk sensors with buy_top=%d, sell_top=%d, buy_worst=%d, sell_worst=%d, mqtt_48h_mode=%s, retry_attempts=%d, retry_delay=%ds", 
                 buy_top, sell_top, buy_worst, sell_worst, mqtt_48h_mode, retry_attempts, retry_delay)

    # Load translations once for all sensors
    global _TRANSLATIONS_CACHE
    if not _TRANSLATIONS_CACHE:
        try:
            _TRANSLATIONS_CACHE = await async_get_translations(
                hass, hass.config.language, DOMAIN, ["entity", "debug"]
            )
        except Exception as ex:
            _LOGGER.warning("Failed to load translations: %s", ex)
            _TRANSLATIONS_CACHE = {}

    # Cleanup old coordinators if they exist
    for price_type in ("buy", "sell"):
        key = f"{entry.entry_id}_{price_type}"
        coordinator = hass.data[DOMAIN].get(key)
        if coordinator:
            _LOGGER.debug("Cleaning up existing %s coordinator", price_type)
            # Cancel scheduled updates
            if hasattr(coordinator, '_unsub_hourly') and coordinator._unsub_hourly:
                coordinator._unsub_hourly()
            if hasattr(coordinator, '_unsub_midnight') and coordinator._unsub_midnight:
                coordinator._unsub_midnight()
            if hasattr(coordinator, '_unsub_afternoon') and coordinator._unsub_afternoon:
                coordinator._unsub_afternoon()
            # Remove from hass data
            hass.data[DOMAIN].pop(key, None)

    entities = []
    coordinators = []
    
    # Create coordinators first
    for price_type in ("buy", "sell"):
        key = f"{entry.entry_id}_{price_type}"
        coordinator = PstrykDataUpdateCoordinator(
            hass, 
            api_key, 
            price_type, 
            mqtt_48h_mode,
            retry_attempts,
            retry_delay
        )
        coordinators.append((coordinator, price_type, key))
        
    # Initialize coordinators in parallel to save time
    initial_refresh_tasks = []
    for coordinator, _, _ in coordinators:
        # Check if we're in the setup process or reloading
        try:
            # Newer Home Assistant versions
            from homeassistant.config_entries import ConfigEntryState
            is_setup = entry.state == ConfigEntryState.SETUP_IN_PROGRESS
        except ImportError:
            # Older Home Assistant versions - try another approach
            is_setup = not hass.data[DOMAIN].get(f"{entry.entry_id}_initialized", False)
            
        if is_setup:
            initial_refresh_tasks.append(coordinator.async_config_entry_first_refresh())
        else:
            initial_refresh_tasks.append(coordinator.async_refresh())
            
    refresh_results = await asyncio.gather(*initial_refresh_tasks, return_exceptions=True)
    
    # Mark as initialized after first setup
    hass.data[DOMAIN][f"{entry.entry_id}_initialized"] = True
    
    # Process coordinators and set up sensors
    for i, (coordinator, price_type, key) in enumerate(coordinators):
        # Check if initial refresh succeeded
        if isinstance(refresh_results[i], Exception):
            _LOGGER.error("Failed to initialize %s coordinator: %s", 
                         price_type, str(refresh_results[i]))
            # Still add coordinator and set up sensors even if initial load failed
        
        # Schedule updates
        coordinator.schedule_hourly_update()
        coordinator.schedule_midnight_update()
        
        # Schedule afternoon update if 48h mode is enabled
        if mqtt_48h_mode:
            coordinator.schedule_afternoon_update()
            
        hass.data[DOMAIN][key] = coordinator

        # Create only one sensor per price type that combines both current price and table data
        top = buy_top if price_type == "buy" else sell_top
        worst = buy_worst if price_type == "buy" else sell_worst
        entities.append(PstrykPriceSensor(coordinator, price_type, top, worst, entry.entry_id))

    async_add_entities(entities, True)


class PstrykPriceSensor(CoordinatorEntity, SensorEntity):
    """Combined price sensor with table data attributes."""
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: PstrykDataUpdateCoordinator, price_type: str, top_count: int, worst_count: int, entry_id: str):
        super().__init__(coordinator)
        self.price_type = price_type
        self.top_count = top_count
        self.worst_count = worst_count
        self.entry_id = entry_id
        self._attr_device_class = "monetary"
        self._cached_sorted_prices = None
        self._last_data_hash = None
        
    async def async_added_to_hass(self):
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()

    @property
    def name(self) -> str:
        return f"Pstryk Current {self.price_type.title()} Price"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.price_type}_price"
    
    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, "pstryk_energy")},
            "name": "Pstryk Energy",
            "manufacturer": "Pstryk",
            "model": "Energy Price Monitor",
            "sw_version": "1.6.2",
        }

    def _get_current_price(self):
        """Get current price based on current time."""
        if not self.coordinator.data or not self.coordinator.data.get("prices"):
            return None
            
        now_utc = dt_util.utcnow()
        for price_entry in self.coordinator.data.get("prices", []):
            try:
                if "start" not in price_entry:
                    continue
                    
                price_datetime = dt_util.parse_datetime(price_entry["start"])
                if not price_datetime:
                    continue
                    
                # Konwersja do UTC dla porównania
                price_datetime_utc = dt_util.as_utc(price_datetime)
                price_end_utc = price_datetime_utc + timedelta(hours=1)
                
                if price_datetime_utc <= now_utc < price_end_utc:
                    return price_entry.get("price")
            except Exception as e:
                _LOGGER.error("Error determining current price: %s", str(e))
                
        return None
    
    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        
        # Próbujemy znaleźć aktualną cenę na podstawie czasu
        current_price = self._get_current_price()
        
        # Jeśli nie znaleźliśmy, używamy wartości z koordynatora
        if current_price is None:
            current_price = self.coordinator.data.get("current")
            
        return current_price

    @property
    def native_unit_of_measurement(self) -> str:
        return "PLN/kWh"
    
    def _get_next_hour_price(self) -> dict:
        """Get price data for the next hour."""
        if not self.coordinator.data:
            return None
            
        now = dt_util.as_local(dt_util.utcnow())
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        
        # Use translations for debug messages
        debug_msg = _TRANSLATIONS_CACHE.get(
            "debug.looking_for_next_hour", 
            "Looking for price for next hour: {next_hour}"
        ).format(next_hour=next_hour.strftime("%Y-%m-%d %H:%M:%S"))
        _LOGGER.debug(debug_msg)
        
        # Check if we're looking for the next day's hour (midnight)
        is_looking_for_next_day = next_hour.day != now.day
        
        # First check in prices_today
        price_found = None
        if self.coordinator.data.get("prices_today"):
            for price_data in self.coordinator.data.get("prices_today", []):
                if "start" not in price_data:
                    continue
                    
                try:
                    price_datetime = dt_util.parse_datetime(price_data["start"])
                    if not price_datetime:
                        continue
                        
                    price_datetime = dt_util.as_local(price_datetime)
                    
                    if price_datetime.hour == next_hour.hour and price_datetime.day == next_hour.day:
                        price_found = price_data.get("price")
                        _LOGGER.debug("Found price for %s in today's list: %s", next_hour.strftime("%Y-%m-%d %H:%M:%S"), price_found)
                        return price_found
                except Exception as e:
                    error_msg = _TRANSLATIONS_CACHE.get(
                        "debug.error_processing_date", 
                        "Error processing date: {error}"
                    ).format(error=str(e))
                    _LOGGER.error(error_msg)
        
        # Always check the full list as a fallback, regardless of day
        if self.coordinator.data.get("prices"):
            _LOGGER.debug("Looking for price in full 48h list as fallback")
            
            for price_data in self.coordinator.data.get("prices", []):
                if "start" not in price_data:
                    continue
                    
                try:
                    price_datetime = dt_util.parse_datetime(price_data["start"])
                    if not price_datetime:
                        continue
                        
                    price_datetime = dt_util.as_local(price_datetime)
                    
                    # Check if this matches the hour and day we're looking for
                    if price_datetime.hour == next_hour.hour and price_datetime.day == next_hour.day:
                        price_found = price_data.get("price")
                        _LOGGER.debug("Found price for %s in full 48h list: %s", next_hour.strftime("%Y-%m-%d %H:%M:%S"), price_found)
                        return price_found
                except Exception as e:
                    full_list_error_msg = _TRANSLATIONS_CACHE.get(
                        "debug.error_processing_full_list", 
                        "Error processing date for full list: {error}"
                    ).format(error=str(e))
                    _LOGGER.error(full_list_error_msg)
        
        # If no price found for next hour
        if is_looking_for_next_day:
            midnight_msg = _TRANSLATIONS_CACHE.get(
                "debug.no_price_midnight", 
                "No price found for next day midnight. Data probably not loaded yet."
            )
            _LOGGER.info(midnight_msg)
        else:
            no_price_msg = _TRANSLATIONS_CACHE.get(
                "debug.no_price_next_hour", 
                "No price found for next hour: {next_hour}"
            ).format(next_hour=next_hour.strftime("%Y-%m-%d %H:%M:%S"))
            _LOGGER.warning(no_price_msg)
                
        return None
    
    def _get_cached_sorted_prices(self, today):
        """Get cached sorted prices or compute if data changed."""
        # Create a simple hash of the data to detect changes
        data_hash = hash(tuple((p["start"], p["price"]) for p in today))
        
        if self._last_data_hash != data_hash or self._cached_sorted_prices is None:
            _LOGGER.debug("Price data changed, recalculating sorted prices")
            
            # Sortowanie dla najlepszych cen
            sorted_best = sorted(
                today,
                key=lambda x: x["price"],
                reverse=(self.price_type == "sell"),
            )
            
            # Sortowanie dla najgorszych cen (odwrotna kolejność sortowania)
            sorted_worst = sorted(
                today,
                key=lambda x: x["price"],
                reverse=(self.price_type != "sell"),
            )
            
            self._cached_sorted_prices = {
                "best": sorted_best[: self.top_count],
                "worst": sorted_worst[: self.worst_count]
            }
            self._last_data_hash = data_hash
        
        return self._cached_sorted_prices
    
    def _is_likely_placeholder_data(self, prices_for_day):
        """Check if prices for a day are likely placeholders.
        
        Returns True if:
        - There are no prices
        - ALL prices have exactly the same value (suggesting API returned default values)
        - There are too many consecutive hours with the same value (e.g., 10+ hours)
        """
        if not prices_for_day:
            return True
            
        # Get all price values
        price_values = [p.get("price") for p in prices_for_day if p.get("price") is not None]
        
        if not price_values:
            return True
        
        # If we have less than 20 prices for a day, it's incomplete data
        if len(price_values) < 20:
            _LOGGER.debug(f"Only {len(price_values)} prices for the day, likely incomplete data")
            return True
            
        # Check if ALL values are identical
        unique_values = set(price_values)
        if len(unique_values) == 1:
            _LOGGER.debug(f"All {len(price_values)} prices have the same value ({price_values[0]}), likely placeholders")
            return True
        
        # Additional check: if more than 90% of values are the same, it's suspicious
        most_common_value = max(set(price_values), key=price_values.count)
        count_most_common = price_values.count(most_common_value)
        if count_most_common / len(price_values) > 0.9:
            _LOGGER.debug(f"{count_most_common}/{len(price_values)} prices have value {most_common_value}, likely placeholders")
            return True
            
        return False
    
    def _count_consecutive_same_values(self, prices):
        """Count maximum consecutive hours with the same price."""
        if not prices:
            return 0
            
        # Sort by time to ensure consecutive checking
        sorted_prices = sorted(prices, key=lambda x: x.get("start", ""))
        
        max_consecutive = 1
        current_consecutive = 1
        last_value = None
        
        for price in sorted_prices:
            value = price.get("price")
            if value is not None:
                if value == last_value:
                    current_consecutive += 1
                    max_consecutive = max(max_consecutive, current_consecutive)
                else:
                    current_consecutive = 1
                last_value = value
                    
        return max_consecutive
    
    def _get_mqtt_price_count(self):
        """Get the actual count of prices that would be published to MQTT."""
        if not self.coordinator.data:
            return 0
            
        if not self.coordinator.mqtt_48h_mode:
            # If not in 48h mode, we only publish today's prices
            prices_today = self.coordinator.data.get("prices_today", [])
            return len(prices_today)
        else:
            # In 48h mode, we need to count valid prices
            all_prices = self.coordinator.data.get("prices", [])
            
            # Just count today's prices as they're always valid
            now = dt_util.as_local(dt_util.utcnow())
            today_str = now.strftime("%Y-%m-%d")
            today_prices = [p for p in all_prices if p.get("start", "").startswith(today_str)]
            
            # For tomorrow, check if data looks valid
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            tomorrow_prices = [p for p in all_prices if p.get("start", "").startswith(tomorrow_str)]
            
            # Count today's prices always
            valid_count = len(today_prices)
            
            # Add tomorrow's prices only if they look like real data
            if tomorrow_prices and not self._is_likely_placeholder_data(tomorrow_prices):
                valid_count += len(tomorrow_prices)
            
            return valid_count
        
    @property
    def extra_state_attributes(self) -> dict:
        """Include the price table attributes in the current price sensor."""
        now = dt_util.as_local(dt_util.utcnow())
        
        # Get translated attribute names from cache
        next_hour_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.next_hour", 
            "Next hour"
        )
        
        using_cached_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.using_cached_data", 
            "Using cached data"
        )
        
        all_prices_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.all_prices", 
            "All prices"
        )
        
        best_prices_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.best_prices", 
            "Best prices"
        )
        
        worst_prices_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.worst_prices", 
            "Worst prices"
        )
        
        best_count_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.best_count", 
            "Best count"
        )
        
        worst_count_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.worst_count", 
            "Worst count"
        )
        
        price_count_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.price_count", 
            "Price count"
        )
        
        last_updated_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.last_updated", 
            "Last updated"
        )
        
        avg_price_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.avg_price", 
            "Average price today"
        )
        
        avg_price_remaining_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.avg_price_remaining",
            "Average price remaining"
        )
        
        avg_price_full_day_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.avg_price_full_day",
            "Average price full day"
        )
        
        tomorrow_available_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.tomorrow_available", 
            "Tomorrow prices available"
        )
        
        mqtt_price_count_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.mqtt_price_count", 
            "MQTT price count"
        )
        
        if self.coordinator.data is None:
            return {
                f"{avg_price_key} /0": None,
                f"{avg_price_key} /24": None,
                next_hour_key: None,
                all_prices_key: [],
                best_prices_key: [],
                worst_prices_key: [],
                best_count_key: self.top_count,
                worst_count_key: self.worst_count,
                price_count_key: 0,
                using_cached_key: False,
                tomorrow_available_key: False,
                mqtt_price_count_key: 0
            }
            
        next_hour_data = self._get_next_hour_price()
        today = self.coordinator.data.get("prices_today", [])
        is_cached = self.coordinator.data.get("is_cached", False)
        
        # Calculate average price for remaining hours today (from current hour)
        avg_price_remaining = None
        remaining_hours_count = 0
        avg_price_full_day = None
        
        if today:
            # Full day average (all 24 hours)
            total_price_full = sum(p.get("price", 0) for p in today if p.get("price") is not None)
            valid_prices_count_full = sum(1 for p in today if p.get("price") is not None)
            if valid_prices_count_full > 0:
                avg_price_full_day = round(total_price_full / valid_prices_count_full, 2)
            
            # Remaining hours average (from current hour onwards)
            current_hour = now.strftime("%Y-%m-%dT%H:")
            remaining_prices = []
            
            for p in today:
                if p.get("price") is not None and p.get("start", "") >= current_hour:
                    remaining_prices.append(p.get("price"))
            
            remaining_hours_count = len(remaining_prices)
            if remaining_hours_count > 0:
                avg_price_remaining = round(sum(remaining_prices) / remaining_hours_count, 2)
        
        # Create keys with hour count in user's preferred format
        avg_price_remaining_with_hours = f"{avg_price_key} /{remaining_hours_count}"
        avg_price_full_day_with_hours = f"{avg_price_key} /24"
        
        # Check if tomorrow's prices are available (more robust check)
        all_prices = self.coordinator.data.get("prices", [])
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow_prices = []
        
        # Only check for tomorrow prices if we have a reasonable amount of data
        if len(all_prices) > 0:
            tomorrow_prices = [p for p in all_prices if p.get("start", "").startswith(tomorrow)]
        
        # Log what we found for debugging
        if tomorrow_prices:
            unique_values = set(p.get("price") for p in tomorrow_prices if p.get("price") is not None)
            consecutive = self._count_consecutive_same_values(tomorrow_prices)
            _LOGGER.debug(
                f"Tomorrow has {len(tomorrow_prices)} prices, "
                f"{len(unique_values)} unique values, "
                f"max {consecutive} consecutive same values"
            )
        
        # Tomorrow is available only if:
        # 1. We have at least 20 hours of data for tomorrow
        # 2. The data doesn't look like placeholders
        tomorrow_available = (
            len(tomorrow_prices) >= 20 and 
            not self._is_likely_placeholder_data(tomorrow_prices)
        )
        
        # Get cached sorted prices
        sorted_prices = self._get_cached_sorted_prices(today) if today else {"best": [], "worst": []}
        
        # Get actual MQTT price count
        mqtt_price_count = self._get_mqtt_price_count()
        
        return {
            avg_price_remaining_with_hours: avg_price_remaining,
            avg_price_full_day_with_hours: avg_price_full_day,
            next_hour_key: next_hour_data,
            all_prices_key: today,
            best_prices_key: sorted_prices["best"],
            worst_prices_key: sorted_prices["worst"],
            best_count_key: self.top_count,
            worst_count_key: self.worst_count,
            price_count_key: len(today),
            last_updated_key: now.strftime("%Y-%m-%d %H:%M:%S"),
            using_cached_key: is_cached,
            tomorrow_available_key: tomorrow_available,
            mqtt_price_count_key: mqtt_price_count,
            "mqtt_48h_mode": self.coordinator.mqtt_48h_mode
        }
        
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success and self.coordinator.data is not None
