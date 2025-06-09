"""Diagnostics support for Pstryk Energy."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, 
    CONF_MQTT_ENABLED, 
    CONF_MQTT_48H_MODE, 
    CONF_MQTT_TOPIC_BUY, 
    CONF_MQTT_TOPIC_SELL,
    CONF_RETRY_ATTEMPTS,
    CONF_RETRY_DELAY,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_DELAY
)

_LOGGER = logging.getLogger(__name__)

async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    diagnostics_data = {
        "entry": {
            "title": entry.title,
            "entry_id": entry.entry_id,
            "version": entry.version,
            "options": {
                "buy_top": entry.options.get("buy_top", entry.data.get("buy_top", 5)),
                "sell_top": entry.options.get("sell_top", entry.data.get("sell_top", 5)),
                "buy_worst": entry.options.get("buy_worst", entry.data.get("buy_worst", 5)),
                "sell_worst": entry.options.get("sell_worst", entry.data.get("sell_worst", 5)),
                "mqtt_enabled": entry.options.get(CONF_MQTT_ENABLED, False),
                "mqtt_48h_mode": entry.options.get(CONF_MQTT_48H_MODE, False),
                "mqtt_topic_buy": entry.options.get(CONF_MQTT_TOPIC_BUY, "Not set"),
                "mqtt_topic_sell": entry.options.get(CONF_MQTT_TOPIC_SELL, "Not set"),
                "retry_attempts": entry.options.get(CONF_RETRY_ATTEMPTS, DEFAULT_RETRY_ATTEMPTS),
                "retry_delay": entry.options.get(CONF_RETRY_DELAY, DEFAULT_RETRY_DELAY),
            },
        },
        "coordinators": {},
        "mqtt_status": {},
    }

    # Check MQTT status
    mqtt_publisher = hass.data[DOMAIN].get(f"{entry.entry_id}_mqtt")
    if mqtt_publisher:
        diagnostics_data["mqtt_status"] = {
            "publisher_initialized": mqtt_publisher._initialized,
            "last_published": mqtt_publisher.last_published.strftime("%Y-%m-%d %H:%M:%S") if mqtt_publisher.last_published else None,
            "topic_buy": mqtt_publisher.mqtt_topic_buy,
            "topic_sell": mqtt_publisher.mqtt_topic_sell,
        }
    else:
        diagnostics_data["mqtt_status"]["enabled"] = False

    # Check coordinators
    now = dt_util.now()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    
    for price_type in ("buy", "sell"):
        key = f"{entry.entry_id}_{price_type}"
        coordinator = hass.data[DOMAIN].get(key)
        if coordinator:
            coordinator_data = {
                "last_update_success": coordinator.last_update_success,
                "data_available": coordinator.data is not None,
                "mqtt_48h_mode": getattr(coordinator, 'mqtt_48h_mode', False),
            }
            
            # Check for retry configuration
            if hasattr(coordinator, 'retry_mechanism'):
                coordinator_data["retry_config"] = {
                    "max_retries": coordinator.retry_mechanism.max_retries,
                    "base_delay": coordinator.retry_mechanism.base_delay,
                }
            
            # Check for various update attributes
            if hasattr(coordinator, 'last_update') and coordinator.last_update:
                coordinator_data["last_update"] = dt_util.as_local(coordinator.last_update).strftime("%Y-%m-%d %H:%M:%S")
            elif hasattr(coordinator, 'last_updated') and coordinator.last_updated:
                coordinator_data["last_update"] = dt_util.as_local(coordinator.last_updated).strftime("%Y-%m-%d %H:%M:%S")
            else:
                coordinator_data["last_update"] = None
                
            # Add price count if data available
            if coordinator.data:
                coordinator_data["prices_count"] = len(coordinator.data.get("prices", []))
                coordinator_data["prices_today_count"] = len(coordinator.data.get("prices_today", []))
                coordinator_data["current_price"] = coordinator.data.get("current")
                coordinator_data["is_cached"] = coordinator.data.get("is_cached", False)
                
                # Check tomorrow prices availability
                all_prices = coordinator.data.get("prices", [])
                tomorrow_prices = [p for p in all_prices if p.get("start", "").startswith(tomorrow)]
                coordinator_data["tomorrow_prices_count"] = len(tomorrow_prices)
                coordinator_data["tomorrow_prices_available"] = len(tomorrow_prices) >= 20
                
                # MQTT price count - what would actually be published
                if coordinator.mqtt_48h_mode:
                    coordinator_data["mqtt_price_count"] = len(all_prices)
                else:
                    coordinator_data["mqtt_price_count"] = len(coordinator.data.get("prices_today", []))
                
            diagnostics_data["coordinators"][price_type] = coordinator_data

    # Check if MQTT integration is available
    diagnostics_data["mqtt_integration_available"] = hass.services.has_service("mqtt", "publish")

    # Add missing import
    from datetime import timedelta

    return diagnostics_data
