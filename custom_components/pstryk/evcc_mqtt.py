# evcc_mqtt.py
import logging
import paho.mqtt.client as mqtt
import json
from homeassistant.util import dt as dt_util
from .const import ATTR_MQTT_HOST, ATTR_MQTT_PORT, ATTR_MQTT_USER, ATTR_MQTT_PASS, ATTR_MQTT_TOPIC

_LOGGER = logging.getLogger(__name__)

class EVCCMQTTBridge:
    def __init__(self, hass, config):
        self.hass = hass
        self.config = config
        self.client = None
        self.connected = False

    async def connect(self):
        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                self.connected = True
                _LOGGER.info("Connected to MQTT broker")
            else:
                self.connected = False
                _LOGGER.error("MQTT connection failed: %s", rc)

        self.client = mqtt.Client()
        self.client.on_connect = on_connect
        
        if self.config.get(ATTR_MQTT_USER):
            self.client.username_pw_set(
                self.config[ATTR_MQTT_USER],
                self.config.get(ATTR_MQTT_PASS, "")
            )

        try:
            await self.hass.async_add_executor_job(
                self.client.connect,
                self.config[ATTR_MQTT_HOST],
                self.config.get(ATTR_MQTT_PORT, 1883),
                60
            )
            self.client.loop_start()
        except Exception as e:
            _LOGGER.error("MQTT connection error: %s", str(e))
            self.connected = False

    async def publish(self, data):
        if not self.connected:
            await self.connect()

        base_topic = self.config.get(ATTR_MQTT_TOPIC, "evcc/pstryk")
        
        try:
            self.client.publish(
                f"{base_topic}/grid",
                json.dumps(data["grid"]),
                retain=True
            )
            self.client.publish(
                f"{base_topic}/feedin",
                json.dumps(data["feedin"]),
                retain=True
            )
            _LOGGER.debug("Published EVCC data to MQTT")
        except Exception as e:
            _LOGGER.error("Error publishing to MQTT: %s", str(e))
            self.connected = False
