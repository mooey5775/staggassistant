"""Support for Fellow Stagg EKG Pro binary sensors."""
import logging
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up StaggLink binary sensors from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    binary_sensors = [
        StaggBinarySensor(coordinator, entry, "on_base", "On Base", None, "mdi:kettle-outline"),
        StaggBinarySensor(coordinator, entry, "no_water", "No Water", BinarySensorDeviceClass.PROBLEM, "mdi:water-off"),
        StaggBinarySensor(coordinator, entry, "water_ready", "Water Ready", None, "mdi:kettle-steam"),
    ]

    async_add_entities(binary_sensors, True)

class StaggBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of a Stagg EKG Pro binary sensor."""

    def __init__(self, coordinator, entry, key, name_suffix, device_class, icon):
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._key = key
        self._attr_name = f"{entry.data.get('name', 'Stagg Kettle')} {name_suffix}"
        self._attr_unique_id = f"stagg_{entry.data['ip_address'].replace('.', '_')}_{key}"
        self._attr_device_class = device_class
        self._attr_icon = icon
        self._attr_has_entity_name = False

        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"stagglink_{entry.data['ip_address'].replace('.', '_')}")},
            "name": entry.data.get("name", "Stagg Kettle"),
            "manufacturer": "Fellow",
            "model": "Stagg EKG Pro",
        }

    @property
    def is_on(self):
        """Return true if the condition is met."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self._key)
