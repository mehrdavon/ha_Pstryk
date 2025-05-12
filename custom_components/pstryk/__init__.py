"""Pstryk Energy integration."""
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components import mqtt
from homeassistant.helpers.event import async_track_point_in_time
from datetime import timedelta
from homeassistant.util import dt as dt_util
import json

from .mqtt_publisher import PstrykMqttPublisher
from .services import async_setup_services, async_unload_services
from .const import (
    DOMAIN, 
    CONF_MQTT_ENABLED, 
    CONF_MQTT_TOPIC_BUY, 
    CONF_MQTT_TOPIC_SELL,
    DEFAULT_MQTT_TOPIC_BUY, 
    DEFAULT_MQTT_TOPIC_SELL
)

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up hass.data structure and services."""
    hass.data.setdefault(DOMAIN, {})
    
    # Set up services
    await async_setup_services(hass)
    
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Store API key and forward to sensor platform."""
    hass.data[DOMAIN].setdefault(entry.entry_id, {})["api_key"] = entry.data.get("api_key")
    
    # Register update listener for option changes - only if not already registered
    if not entry.update_listeners:
        entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    
    await hass.config_entries.async_forward_entry_setup(entry, "sensor")
    _LOGGER.debug("Pstryk entry setup: %s", entry.entry_id)
    
    # Setup MQTT publisher if enabled
    mqtt_enabled = entry.options.get(CONF_MQTT_ENABLED, False)
    mqtt_topic_buy = entry.options.get(CONF_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_BUY)
    mqtt_topic_sell = entry.options.get(CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_SELL)
    
    if mqtt_enabled:
        # Check if MQTT is available
        if not hass.services.has_service("mqtt", "publish"):
            _LOGGER.error("MQTT integration is not enabled. Cannot setup EVCC bridge.")
            # Display persistent notification to user
            hass.components.persistent_notification.create(
                "MQTT integration is not enabled. EVCC MQTT Bridge for Pstryk Energy "
                "cannot function. Please configure MQTT integration in Home Assistant.",
                title="Pstryk Energy MQTT Error",
                notification_id=f"{DOMAIN}_mqtt_error_{entry.entry_id}"
            )
            # Still return True to allow the rest of the integration to work
            return True
            
        # Create and store the MQTT publisher
        mqtt_publisher = PstrykMqttPublisher(
            hass, 
            entry.entry_id, 
            mqtt_topic_buy,
            mqtt_topic_sell
        )
        hass.data[DOMAIN][f"{entry.entry_id}_mqtt"] = mqtt_publisher
        
        # We need to wait until sensors are fully setup
        async def start_mqtt_publisher(_):
            """Start the MQTT publisher after a short delay to ensure coordinators are ready."""
            await mqtt_publisher.async_initialize()
            await mqtt_publisher.schedule_periodic_updates(interval_minutes=5)
            
            # Also start automatic force_retain to ensure messages stay in MQTT
            await setup_automatic_retain(
                hass, 
                entry.entry_id, 
                mqtt_topic_buy,
                mqtt_topic_sell
            )
        
        # Schedule the initialization to happen shortly after setup is complete
        hass.async_create_task(start_mqtt_publisher(None))
        
        _LOGGER.info("EVCC MQTT Bridge enabled for Pstryk Energy, publishing to %s and %s", 
                    mqtt_topic_buy, mqtt_topic_sell)
        
    return True

async def setup_automatic_retain(hass, entry_id, buy_topic, sell_topic, retain_hours=168):
    """Set up automatic republishing of MQTT messages to ensure they remain retained."""
    _LOGGER.info("Setting up automatic MQTT message retention for buy topic %s and sell topic %s",
                buy_topic, sell_topic)
    
    # Get buy and sell coordinators
    buy_coordinator = hass.data[DOMAIN].get(f"{entry_id}_buy")
    sell_coordinator = hass.data[DOMAIN].get(f"{entry_id}_sell")
    
    if not buy_coordinator or not buy_coordinator.data or not sell_coordinator or not sell_coordinator.data:
        _LOGGER.error("No data available for entry %s, will retry later", entry_id)
        # Schedule a retry in 30 seconds
        now = dt_util.now()
        next_run = now + timedelta(seconds=30)
        hass.async_create_task(
            async_track_point_in_time(
                hass, 
                lambda _: setup_automatic_retain(hass, entry_id, buy_topic, sell_topic, retain_hours),
                dt_util.as_utc(next_run)
            )
        )
        return
    
    # Get or create MQTT publisher
    mqtt_publisher = hass.data[DOMAIN].get(f"{entry_id}_mqtt")
    if not mqtt_publisher:
        from .mqtt_publisher import PstrykMqttPublisher
        mqtt_publisher = PstrykMqttPublisher(hass, entry_id, buy_topic, sell_topic)
        await mqtt_publisher.async_initialize()
    
    # Format prices
    buy_prices = mqtt_publisher._format_prices_for_evcc(buy_coordinator.data, "buy")
    sell_prices = mqtt_publisher._format_prices_for_evcc(sell_coordinator.data, "sell")
    
    if not buy_prices or not sell_prices:
        _LOGGER.error("No price data available to publish for automatic retain")
        return
    
    # Sort prices and create payloads
    buy_prices.sort(key=lambda x: x["start"])
    sell_prices.sort(key=lambda x: x["start"])
    
    buy_payload = json.dumps(buy_prices)
    sell_payload = json.dumps(sell_prices)
    
    # Publish immediately with retain flag
    await hass.services.async_call(
        "mqtt", 
        "publish",
        {
            "topic": buy_topic,
            "payload": buy_payload,
            "qos": 1,
            "retain": True
        },
        blocking=True
    )
    
    await hass.services.async_call(
        "mqtt", 
        "publish",
        {
            "topic": sell_topic,
            "payload": sell_payload,
            "qos": 1,
            "retain": True
        },
        blocking=True
    )
    
    _LOGGER.info("Published initial retained messages for buy and sell topics")
    
    # Set up recurring republish every 5 minutes to ensure retention
    now = dt_util.now()
    retain_key = f"{entry_id}_auto_retain"
    
    # Cancel any existing task
    if retain_key in hass.data[DOMAIN] and callable(hass.data[DOMAIN][retain_key]):
        hass.data[DOMAIN][retain_key]()
        hass.data[DOMAIN].pop(retain_key, None)
    
    # Function to republish data periodically
    async def republish_retain_periodic(now=None):
        """Republish MQTT message with retain flag periodically."""
        try:
            # Get fresh data
            if buy_coordinator.data and sell_coordinator.data:
                buy_prices = mqtt_publisher._format_prices_for_evcc(buy_coordinator.data, "buy")
                sell_prices = mqtt_publisher._format_prices_for_evcc(sell_coordinator.data, "sell")
                
                if buy_prices and sell_prices:
                    buy_prices.sort(key=lambda x: x["start"])
                    sell_prices.sort(key=lambda x: x["start"])
                    
                    buy_payload = json.dumps(buy_prices)
                    sell_payload = json.dumps(sell_prices)
                    
                    # Publish with retain flag
                    await hass.services.async_call(
                        "mqtt", 
                        "publish",
                        {
                            "topic": buy_topic,
                            "payload": buy_payload,
                            "qos": 1,
                            "retain": True
                        },
                        blocking=True
                    )
                    
                    await hass.services.async_call(
                        "mqtt", 
                        "publish",
                        {
                            "topic": sell_topic,
                            "payload": sell_payload,
                            "qos": 1,
                            "retain": True
                        },
                        blocking=True
                    )
                    
                    _LOGGER.debug("Auto-republished retained messages to buy and sell topics")
            
            # Schedule next run
            next_run = dt_util.now() + timedelta(minutes=5)
            hass.data[DOMAIN][retain_key] = async_track_point_in_time(
                hass, republish_retain_periodic, dt_util.as_utc(next_run)
            )
            
        except Exception as e:
            _LOGGER.error("Error in automatic MQTT retain process: %s", str(e))
            # Try to reschedule anyway
            next_run = dt_util.now() + timedelta(minutes=5)
            hass.data[DOMAIN][retain_key] = async_track_point_in_time(
                hass, republish_retain_periodic, dt_util.as_utc(next_run)
            )
    
    # Schedule first run
    next_run = now + timedelta(minutes=5)
    hass.data[DOMAIN][retain_key] = async_track_point_in_time(
        hass, republish_retain_periodic, dt_util.as_utc(next_run)
    )
    
    _LOGGER.info(
        "Automatic MQTT message retention activated for buy and sell topics (every 5 minutes)"
    )

async def _cleanup_coordinators(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up coordinators and cancel scheduled tasks."""
    # Clean up auto retain task
    retain_key = f"{entry.entry_id}_auto_retain"
    if retain_key in hass.data[DOMAIN] and callable(hass.data[DOMAIN][retain_key]):
        hass.data[DOMAIN][retain_key]()
        hass.data[DOMAIN].pop(retain_key, None)
    
    # Clean up MQTT publisher if exists
    mqtt_publisher = hass.data[DOMAIN].get(f"{entry.entry_id}_mqtt")
    if mqtt_publisher:
        _LOGGER.debug("Cleaning up MQTT publisher for entry %s", entry.entry_id)
        mqtt_publisher.unsubscribe()
        hass.data[DOMAIN].pop(f"{entry.entry_id}_mqtt", None)
    
    # Clean up coordinators
    for price_type in ("buy", "sell"):
        key = f"{entry.entry_id}_{price_type}"
        coordinator = hass.data[DOMAIN].get(key)
        if coordinator:
            _LOGGER.debug("Cleaning up %s coordinator for entry %s", price_type, entry.entry_id)
            # Cancel scheduled updates
            if hasattr(coordinator, '_unsub_hourly') and coordinator._unsub_hourly:
                coordinator._unsub_hourly()
                coordinator._unsub_hourly = None
            if hasattr(coordinator, '_unsub_midnight') and coordinator._unsub_midnight:
                coordinator._unsub_midnight()
                coordinator._unsub_midnight = None
            # Remove from hass data
            hass.data[DOMAIN].pop(key, None)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload sensor platform and clear data."""
    # First cancel coordinators' scheduled updates
    await _cleanup_coordinators(hass, entry)
    
    # Then unload the platform
    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, "sensor")
    
    # Finally clean up data
    if unload_ok:
        if entry.entry_id in hass.data[DOMAIN]:
            hass.data[DOMAIN].pop(entry.entry_id)
            
        # Clean up any remaining components
        for key in list(hass.data[DOMAIN].keys()):
            if key.startswith(f"{entry.entry_id}_"):
                hass.data[DOMAIN].pop(key, None)
                
        # If this is the last entry, unload services
        entries = hass.config_entries.async_entries(DOMAIN)
        if len(entries) <= 1:  # This is the last or only entry
            await async_unload_services(hass)
                
    return unload_ok

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options change."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
