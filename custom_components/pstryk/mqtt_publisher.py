"""MQTT Publisher for Pstryk Energy integration."""
import logging
import json
from datetime import datetime, timedelta
import asyncio
from homeassistant.helpers.entity import Entity
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.components import mqtt
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    DEFAULT_MQTT_TOPIC_BUY,
    DEFAULT_MQTT_TOPIC_SELL
)

_LOGGER = logging.getLogger(__name__)

class PstrykMqttPublisher:
    """Class to handle publishing Pstryk energy prices to MQTT for EVCC."""

    def __init__(
        self, 
        hass: HomeAssistant, 
        entry_id: str, 
        mqtt_topic_buy: str = DEFAULT_MQTT_TOPIC_BUY,
        mqtt_topic_sell: str = DEFAULT_MQTT_TOPIC_SELL
    ):
        """Initialize the MQTT publisher."""
        self.hass = hass
        self.entry_id = entry_id
        self.mqtt_topic_buy = mqtt_topic_buy
        self.mqtt_topic_sell = mqtt_topic_sell
        self._publish_task = None
        self._initialized = False
        self._translations = {}
        self._unsub_timer = None
        self._last_published = None

    async def async_initialize(self):
        """Initialize the publisher and load translations."""
        if self._initialized:
            return True
            
        # Load translations
        try:
            self._translations = await async_get_translations(
                self.hass, self.hass.config.language, DOMAIN, ["mqtt"]
            )
        except Exception as ex:
            _LOGGER.warning("Failed to load translations for MQTT publisher: %s", ex)
            
        self._initialized = True
        return True
    
    def _is_day_data_valid(self, day_prices):
        """Check if prices for a specific day look like real data.
        
        Returns False if the data appears to be placeholders.
        """
        if not day_prices:
            return False
            
        price_values = [p.get("price") for p in day_prices if p.get("price") is not None]
        
        if not price_values:
            return False
            
        # Need at least 20 hours for a valid day
        if len(price_values) < 20:
            return False
            
        # If all values are identical, it's likely placeholder data
        unique_values = set(price_values)
        if len(unique_values) == 1:
            return False
            
        # If more than 90% of values are the same, probably placeholders
        most_common = max(set(price_values), key=price_values.count)
        if price_values.count(most_common) / len(price_values) > 0.9:
            return False
            
        return True

    def _format_prices_for_evcc(self, prices_data, price_type):
        """Format prices in EVCC expected format.
        
        EVCC expects a list of objects with the following structure:
        [
            {
                "start": "2024-05-07T00:00:00Z", // ISO timestamp in UTC
                "end": "2024-05-07T01:00:00Z",   // ISO timestamp in UTC (next hour)
                "value": 0.1234                  // Price in PLN/kWh
            }
        ]
        """
        if not prices_data or "prices" not in prices_data:
            return []
            
        formatted_prices = []
        
        # First, check if we're in 48h mode
        mqtt_48h_mode = self.hass.data[DOMAIN].get(f"{self.entry_id}_mqtt_48h_mode", False)
        
        # Get current date for comparison
        now = dt_util.as_local(dt_util.utcnow())
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        
        # Get the appropriate price list based on mode
        if mqtt_48h_mode:
            price_list = prices_data.get("prices", [])
            
            # In 48h mode, we need to check if tomorrow's data is valid
            # Separate today and tomorrow prices
            today_prices = [p for p in price_list if p.get("start", "").startswith(today_str)]
            tomorrow_prices = [p for p in price_list if p.get("start", "").startswith(tomorrow_str)]
            
            # Always include today's prices
            prices_to_process = today_prices.copy()
            
            # Only include tomorrow if the data looks valid
            if tomorrow_prices and self._is_day_data_valid(tomorrow_prices):
                prices_to_process.extend(tomorrow_prices)
                _LOGGER.info(f"Including {len(tomorrow_prices)} tomorrow prices in MQTT publish")
            else:
                if tomorrow_prices:
                    _LOGGER.info(f"Excluding {len(tomorrow_prices)} tomorrow prices from MQTT - appear to be placeholders")
                else:
                    _LOGGER.debug("No tomorrow prices available for MQTT publish")
        else:
            # In normal mode, use only today's prices
            prices_to_process = prices_data.get("prices_today", [])
            
        # Process the selected prices
        for price_entry in prices_to_process:
            try:
                if "start" not in price_entry or "price" not in price_entry:
                    continue
                    
                # Validate price is a number
                try:
                    price_value = float(price_entry["price"])
                except (TypeError, ValueError):
                    _LOGGER.warning("Invalid price value: %s", price_entry.get("price"))
                    continue
                    
                # Parse local time and convert to UTC ISO format
                local_dt = dt_util.parse_datetime(price_entry["start"])
                if not local_dt:
                    continue
                    
                utc_dt = dt_util.as_utc(local_dt)
                
                # Calculate end time (1 hour later)
                end_dt = utc_dt + timedelta(hours=1)
                
                # Format times in ISO format with Z suffix for UTC
                start_str = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                
                # EVCC w trybie custom pokazuje ceny jako "gr" (grosze), 
                price_value = price_value / 1
                
                formatted_prices.append({
                    "start": start_str,
                    "end": end_str,
                    "value": price_value
                })
            except Exception as e:
                _LOGGER.error("Error formatting price for EVCC: %s", str(e))
                
        _LOGGER.debug(f"Formatted {len(formatted_prices)} prices for MQTT from {len(prices_to_process)} input prices")
        return formatted_prices

    async def publish_prices(self):
        """Publish prices to MQTT using common function."""
        from .mqtt_common import publish_mqtt_prices
        
        success = await publish_mqtt_prices(
            self.hass, 
            self.entry_id, 
            self.mqtt_topic_buy, 
            self.mqtt_topic_sell
        )
        
        if success:
            self._last_published = dt_util.now()
            
        return success

    async def schedule_periodic_updates(self, interval_minutes=60):
        """Schedule periodic updates to MQTT (default: 60 minutes)."""
        from .mqtt_common import setup_periodic_mqtt_publish
        
        # Use common function for periodic publishing
        await setup_periodic_mqtt_publish(
            self.hass,
            self.entry_id,
            self.mqtt_topic_buy,
            self.mqtt_topic_sell,
            interval_minutes
        )
        
        return True

    def unsubscribe(self):
        """Unsubscribe from all events."""
        # Cleanup is handled by common function in mqtt_common.py
        _LOGGER.debug("MQTT publisher cleanup requested")
            
    @property
    def last_published(self):
        """Return the timestamp of the last successful publish."""
        return self._last_published
