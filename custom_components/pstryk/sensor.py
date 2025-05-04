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
from .const import DOMAIN
from homeassistant.helpers.translation import async_get_translations

_LOGGER = logging.getLogger(__name__)

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

    _LOGGER.debug("Setting up Pstryk sensors with buy_top=%d, sell_top=%d, buy_worst=%d, sell_worst=%d", 
                 buy_top, sell_top, buy_worst, sell_worst)

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
            # Remove from hass data
            hass.data[DOMAIN].pop(key, None)

    entities = []
    coordinators = []
    
    # Create coordinators first
    for price_type in ("buy", "sell"):
        key = f"{entry.entry_id}_{price_type}"
        coordinator = PstrykDataUpdateCoordinator(hass, api_key, price_type)
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
        hass.data[DOMAIN][key] = coordinator

        # Create only one sensor per price type that combines both current price and table data
        top = buy_top if price_type == "buy" else sell_top
        worst = buy_worst if price_type == "buy" else sell_worst
        entities.append(PstrykPriceSensor(coordinator, price_type, top, worst))

    async_add_entities(entities, True)


class PstrykPriceSensor(CoordinatorEntity, SensorEntity):
    """Combined price sensor with table data attributes."""
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: PstrykDataUpdateCoordinator, price_type: str, top_count: int, worst_count: int):
        super().__init__(coordinator)
        self.price_type = price_type
        self.top_count = top_count
        self.worst_count = worst_count
        self._attr_device_class = "monetary"
        self._translations = {}
        
    async def async_added_to_hass(self):
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()
        
        # Load translations
        self._translations = await self._load_translations()

    async def _load_translations(self):
        """Load translations for the current language."""
        translations = {}
        try:
            translations = await async_get_translations(
                self.hass, self.hass.config.language, DOMAIN, ["entity", "debug"]
            )
        except Exception as ex:
            _LOGGER.warning("Failed to load translations: %s", ex)
        return translations

    @property
    def name(self) -> str:
        return f"Pstryk Current {self.price_type.title()} Price"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.price_type}_price"

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("current")

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
        debug_msg = self._translations.get(
            "debug.looking_for_next_hour", 
            "Looking for price for next hour: {next_hour}"
        ).format(next_hour=next_hour.isoformat())
        _LOGGER.debug(debug_msg)
        
        # Check if we're looking for the next day's hour (midnight)
        is_looking_for_next_day = next_hour.day != now.day
        
        # First check in prices_today
        if not is_looking_for_next_day or self.coordinator.data.get("prices_today"):
            for price_data in self.coordinator.data.get("prices_today", []):
                if "start" not in price_data:
                    continue
                    
                try:
                    price_datetime = dt_util.parse_datetime(price_data["start"])
                    if not price_datetime:
                        continue
                        
                    price_datetime = dt_util.as_local(price_datetime)
                    
                    if price_datetime.hour == next_hour.hour and price_datetime.day == next_hour.day:
                        return price_data.get("price")
                except Exception as e:
                    error_msg = self._translations.get(
                        "debug.error_processing_date", 
                        "Error processing date: {error}"
                    ).format(error=str(e))
                    _LOGGER.error(error_msg)
        
        # If looking for midnight hour (next day), also check prices (full 48h list)
        if is_looking_for_next_day and self.coordinator.data.get("prices"):
            next_day_msg = self._translations.get(
                "debug.looking_for_next_day", 
                "Looking for next day price in full price list (48h)"
            )
            _LOGGER.debug(next_day_msg)
            
            for price_data in self.coordinator.data.get("prices", []):
                if "start" not in price_data:
                    continue
                    
                try:
                    price_datetime = dt_util.parse_datetime(price_data["start"])
                    if not price_datetime:
                        continue
                        
                    price_datetime = dt_util.as_local(price_datetime)
                    
                    # Check if this is 00:00 of the next day
                    if price_datetime.hour == 0 and price_datetime.day == next_hour.day:
                        return price_data.get("price")
                except Exception as e:
                    full_list_error_msg = self._translations.get(
                        "debug.error_processing_full_list", 
                        "Error processing date for full list: {error}"
                    ).format(error=str(e))
                    _LOGGER.error(full_list_error_msg)
        
        # If no price found for next hour
        if is_looking_for_next_day:
            midnight_msg = self._translations.get(
                "debug.no_price_midnight", 
                "No price found for next day midnight. Data probably not loaded yet."
            )
            _LOGGER.info(midnight_msg)
        else:
            no_price_msg = self._translations.get(
                "debug.no_price_next_hour", 
                "No price found for next hour: {next_hour}"
            ).format(next_hour=next_hour.isoformat())
            _LOGGER.warning(no_price_msg)
            
        return None
        
    @property
    def extra_state_attributes(self) -> dict:
        """Include the price table attributes in the current price sensor."""
        now = dt_util.as_local(dt_util.utcnow())
        
        # Get translated attribute name
        next_hour_key = self._translations.get(
            "entity.sensor.next_hour", 
            "Next hour"
        )
        
        if self.coordinator.data is None:
            return {
                next_hour_key: None,
                "all_prices": [],
                "best_prices": [],
                "worst_prices": [],
                "top_count": self.top_count,
                "worst_count": self.worst_count,
                "last_updated": now.isoformat(),
                "price_count": 0,
                "data_available": False
            }
            
        next_hour_data = self._get_next_hour_price()
        today = self.coordinator.data.get("prices_today", [])
        
        # Sortowanie dla najlepszych cen
        sorted_best_prices = sorted(
            today,
            key=lambda x: x["price"],
            reverse=(self.price_type == "sell"),
        )
        
        # Sortowanie dla najgorszych cen (odwrotna kolejność sortowania)
        sorted_worst_prices = sorted(
            today,
            key=lambda x: x["price"],
            reverse=(self.price_type != "sell"),
        )
        
        return {
            next_hour_key: next_hour_data,
            "all_prices": today,
            "best_prices": sorted_best_prices[: self.top_count],
            "worst_prices": sorted_worst_prices[: self.worst_count],
            "top_count": self.top_count,
            "worst_count": self.worst_count,
            "price_count": len(today),
            "last_updated": now.isoformat(),
            "data_available": True
        }
        
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success and self.coordinator.data is not None
