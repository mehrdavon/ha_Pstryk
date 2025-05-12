"""Config flow for Pstryk Energy integration."""
from homeassistant import config_entries
import voluptuous as vol
import aiohttp
import asyncio
import async_timeout
from datetime import timedelta
from homeassistant.util import dt as dt_util
from homeassistant.core import callback
from homeassistant.components import mqtt
from homeassistant.exceptions import HomeAssistantError
from .const import (
    DOMAIN, 
    API_URL, 
    API_TIMEOUT, 
    DEFAULT_MQTT_TOPIC_BUY, 
    DEFAULT_MQTT_TOPIC_SELL, 
    CONF_MQTT_ENABLED, 
    CONF_MQTT_TOPIC_BUY, 
    CONF_MQTT_TOPIC_SELL
)

class MQTTNotConfiguredError(HomeAssistantError):
    """Exception raised when MQTT is not configured."""
    pass

class PstrykConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pstryk Energy."""
    VERSION = 2

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            # Sprawdź poprawność API key
            api_key = user_input["api_key"]
            valid = await self._validate_api_key(api_key)
            
            if valid:
                # Check MQTT configuration if enabled
                mqtt_enabled = user_input.get(CONF_MQTT_ENABLED, False)
                if mqtt_enabled:
                    mqtt_configured = await self._check_mqtt_configuration()
                    if not mqtt_configured:
                        errors["base"] = "mqtt_not_configured"
                
                if not errors:
                    # Extract MQTT related configs
                    data = {
                        "api_key": user_input["api_key"],
                        "buy_top": user_input["buy_top"],
                        "sell_top": user_input["sell_top"],
                        "buy_worst": user_input["buy_worst"],
                        "sell_worst": user_input["sell_worst"],
                    }
                    
                    # Add MQTT configs to options
                    options = {
                        CONF_MQTT_ENABLED: user_input.get(CONF_MQTT_ENABLED, False),
                        CONF_MQTT_TOPIC_BUY: user_input.get(CONF_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_BUY),
                        CONF_MQTT_TOPIC_SELL: user_input.get(CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_SELL),
                    }
                    
                    return self.async_create_entry(
                        title="Pstryk Energy", 
                        data=data,
                        options=options
                    )
            else:
                errors["api_key"] = "invalid_api_key"

        # Check if MQTT integration is loaded
        mqtt_enabled = False
        try:
            mqtt_enabled = self.hass.services.has_service("mqtt", "publish")
        except Exception:
            mqtt_enabled = False
            
        # Base schema for required fields
        schema = {
            vol.Required("api_key"): str,
            vol.Required("buy_top", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
            vol.Required("sell_top", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
            vol.Required("buy_worst", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
            vol.Required("sell_worst", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
        }
        
        # Add MQTT fields if MQTT integration is loaded
        if mqtt_enabled:
            schema.update({
                vol.Required(CONF_MQTT_ENABLED, default=False): bool,
                vol.Optional(CONF_MQTT_TOPIC_BUY, default=DEFAULT_MQTT_TOPIC_BUY): str,
                vol.Optional(CONF_MQTT_TOPIC_SELL, default=DEFAULT_MQTT_TOPIC_SELL): str,
            })

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
            errors=errors
        )
    
    async def _validate_api_key(self, api_key):
        """Validate API key by calling a simple API endpoint."""
        # Używamy endpointu buy z krótkim oknem czasowym dla szybkiego sprawdzenia
        now = dt_util.utcnow()
        start_utc = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        endpoint = f"pricing/?resolution=hour&window_start={start_utc}&window_end={end_utc}"
        url = f"{API_URL}{endpoint}"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with async_timeout.timeout(API_TIMEOUT):
                    resp = await session.get(
                        url,
                        headers={"Authorization": api_key, "Accept": "application/json"}
                    )
                    return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False
    
    async def _check_mqtt_configuration(self):
        """Check if MQTT integration is properly configured."""
        try:
            # Sprawdź czy usługa MQTT publish jest dostępna
            # To powinno działać zarówno z dodatkiem MQTT jak i z integracją podstawową
            return self.hass.services.has_service("mqtt", "publish")
        except Exception:
            return False
    
    @staticmethod
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return PstrykOptionsFlowHandler(config_entry)


class PstrykOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Pstryk options."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        errors = {}
        
        if user_input is not None:
            # Check MQTT configuration if enabled
            mqtt_enabled = user_input.get(CONF_MQTT_ENABLED, False)
            if mqtt_enabled:
                mqtt_configured = await self._check_mqtt_configuration()
                if not mqtt_configured:
                    errors["base"] = "mqtt_not_configured"
            
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        # Check if MQTT integration is loaded
        mqtt_enabled = False
        try:
            mqtt_enabled = self.hass.services.has_service("mqtt", "publish")
        except Exception:
            mqtt_enabled = False
        
        # Base schema for required fields    
        options = {
            vol.Required("buy_top", default=self.config_entry.options.get(
                "buy_top", self.config_entry.data.get("buy_top", 5))): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=24)),
            vol.Required("sell_top", default=self.config_entry.options.get(
                "sell_top", self.config_entry.data.get("sell_top", 5))): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=24)),
            vol.Required("buy_worst", default=self.config_entry.options.get(
                "buy_worst", self.config_entry.data.get("buy_worst", 5))): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=24)),
            vol.Required("sell_worst", default=self.config_entry.options.get(
                "sell_worst", self.config_entry.data.get("sell_worst", 5))): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=24)),
        }
        
        # Add MQTT fields if MQTT integration is loaded
        if mqtt_enabled:
            options.update({
                vol.Required(CONF_MQTT_ENABLED, default=self.config_entry.options.get(
                    CONF_MQTT_ENABLED, False)): bool,
                vol.Optional(CONF_MQTT_TOPIC_BUY, default=self.config_entry.options.get(
                    CONF_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_BUY)): str,
                vol.Optional(CONF_MQTT_TOPIC_SELL, default=self.config_entry.options.get(
                    CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_SELL)): str,
            })

        return self.async_show_form(
            step_id="init", 
            data_schema=vol.Schema(options),
            errors=errors
        )
        
    async def _check_mqtt_configuration(self):
        """Check if MQTT integration is properly configured."""
        try:
            # Sprawdź czy usługa MQTT publish jest dostępna
            # To powinno działać zarówno z dodatkiem MQTT jak i z integracją podstawową
            return self.hass.services.has_service("mqtt", "publish")
        except Exception:
            return False
