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
        
        for price_entry in prices_data.get("prices", []):
            try:
                if "start" not in price_entry or "price" not in price_entry:
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
                price_value = price_entry["price"] / 1
                
                formatted_prices.append({
                    "start": start_str,
                    "end": end_str,
                    "value": price_value
                })
            except Exception as e:
                _LOGGER.error("Error formatting price for EVCC: %s", str(e))
                
        return formatted_prices

    async def publish_prices(self):
        """Publish prices to MQTT in EVCC format for both buy and sell."""
        try:
            if not self._initialized:
                await self.async_initialize()
                
            # Get both buy and sell coordinators
            buy_coordinator = self.hass.data[DOMAIN].get(f"{self.entry_id}_buy")
            sell_coordinator = self.hass.data[DOMAIN].get(f"{self.entry_id}_sell")
            
            if not buy_coordinator or not sell_coordinator:
                _LOGGER.error("Unable to find Pstryk coordinators for MQTT publishing")
                return False
                
            # Check if data is available
            if not buy_coordinator.data:
                _LOGGER.warning("No buy price data available for MQTT publishing")
                return False
                
            if not sell_coordinator.data:
                _LOGGER.warning("No sell price data available for MQTT publishing")
                return False
                
            # Format prices for EVCC
            buy_prices = self._format_prices_for_evcc(buy_coordinator.data, "buy")
            sell_prices = self._format_prices_for_evcc(sell_coordinator.data, "sell")
            
            if not buy_prices:
                _LOGGER.warning("No valid buy prices available to publish to MQTT")
                return False
                
            if not sell_prices:
                _LOGGER.warning("No valid sell prices available to publish to MQTT")
                return False
                
            # Sort prices by time to ensure chronological order
            buy_prices.sort(key=lambda x: x["start"])
            sell_prices.sort(key=lambda x: x["start"])
            
            # Convert to JSON
            buy_payload = json.dumps(buy_prices)
            sell_payload = json.dumps(sell_prices)
            
            # Log before publishing
            _LOGGER.debug(
                "Publishing buy prices to MQTT topic %s with RETAIN=TRUE, QoS=1, payload length: %d bytes", 
                self.mqtt_topic_buy, 
                len(buy_payload)
            )
            
            _LOGGER.debug(
                "Publishing sell prices to MQTT topic %s with RETAIN=TRUE, QoS=1, payload length: %d bytes", 
                self.mqtt_topic_sell, 
                len(sell_payload)
            )
            
            # Publish buy prices to MQTT with explicit retain flag
            await mqtt.async_publish(
                self.hass,
                self.mqtt_topic_buy,
                buy_payload,
                qos=1,
                retain=True
            )
            
            # Publish sell prices to MQTT with explicit retain flag
            await mqtt.async_publish(
                self.hass,
                self.mqtt_topic_sell,
                sell_payload,
                qos=1,
                retain=True
            )
            
            # Double-check with a direct publish to ensure they're retained
            await self.hass.services.async_call(
                "mqtt", 
                "publish",
                {
                    "topic": self.mqtt_topic_buy,
                    "payload": buy_payload,
                    "qos": 1,
                    "retain": True
                },
                blocking=True
            )
            
            await self.hass.services.async_call(
                "mqtt", 
                "publish",
                {
                    "topic": self.mqtt_topic_sell,
                    "payload": sell_payload,
                    "qos": 1,
                    "retain": True
                },
                blocking=True
            )
            
            now = dt_util.now()
            self._last_published = now
            
            _LOGGER.info(
                "Published %d buy prices to %s and %d sell prices to %s WITH RETAIN FLAG", 
                len(buy_prices), 
                self.mqtt_topic_buy,
                len(sell_prices),
                self.mqtt_topic_sell
            )
            
            return True
            
        except Exception as e:
            _LOGGER.error("Error publishing Pstryk prices to MQTT: %s", str(e))
            return False

    async def schedule_periodic_updates(self, interval_minutes=5):
        """Schedule periodic updates to MQTT."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
            
        # Initial publish
        await self.publish_prices()
        
        # Schedule periodic updates
        async def periodic_publish(_now=None):
            """Handle periodic publishing."""
            await self.publish_prices()
            
        # POPRAWKA: użycie bezpośrednio funkcji async_track_time_interval zamiast przez hass.helpers.event
        self._unsub_timer = async_track_time_interval(
            self.hass,
            periodic_publish, 
            timedelta(minutes=interval_minutes)
        )
        
        _LOGGER.debug(
            "Scheduled periodic MQTT publishing every %d minutes", 
            interval_minutes
        )
        
        return True

    def unsubscribe(self):
        """Unsubscribe from all events."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
            _LOGGER.debug("Unsubscribed from MQTT publishing timer")
            
    @property
    def last_published(self):
        """Return the timestamp of the last successful publish."""
        return self._last_published
