"""DataUpdateCoordinator for Fellow Stagg EKG Pro integration."""
import json
import logging
import re
import struct
import async_timeout
from datetime import timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# Firmware 1.2.26+ no longer prints CLI output. State is served as JSON at /temp
# and the settings as a 17-byte binary record at this API path. Record layout
# credit: https://github.com/bramboe/stagg-ekg-plus-ha (MIT).
NATIVE_SETTINGS_PATH = "api?i=0,p=0,d=0,t=3,s=1"
SETTINGS_RECORD_SIZE = 17

# Field mask bits (bytes 0-1 of the settings record)
MASK_ALTITUDE = 0x0001
MASK_SCHEDULE = 0x0004
MASK_SCHEDULE_ON = 0x0008
MASK_CLOCK = 0x0010
MASK_CLOCK_MODE = 0x0020
MASK_HOLD = 0x0040
MASK_CHIME = 0x0080
MASK_UNITS_C = 0x0200
MASK_BOIL = 0x0400
MASK_BOIL_ON = 0x0800
MASK_LANGUAGE = 0x1000


def parse_float(value):
    """Convert a value to float, treating 'nan' and NaN as missing."""
    if value is None or value == 'nan':
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # NaN is the only value that is not equal to itself
    return None if result != result else result


def decode_record_temperature(raw):
    """Temperatures are half-degrees Celsius with bit 15 set, else whole Fahrenheit."""
    if raw & 0x8000:
        return (raw & 0x7fff) / 2
    return round((raw - 32) / 1.8, 1)


def decode_settings_record(raw):
    """Decode the 17-byte settings record into the legacy prtsettings fields."""
    if len(raw) != SETTINGS_RECORD_SIZE:
        raise ValueError(f"Expected a {SETTINGS_RECORD_SIZE}-byte settings record, got {len(raw)}")
    mask, altitude, _target, sch_tempr = struct.unpack_from("<HHHH", raw)

    settings = {"units": "C" if mask & MASK_UNITS_C else "F"}
    if mask & MASK_ALTITUDE:
        # Meters with bit 15 set, else feet
        meters = (altitude & 0x7fff) if altitude & 0x8000 else altitude * 0.3048
        settings["altitude"] = int(round(meters))
    if mask & MASK_SCHEDULE:
        settings["schedon"] = 1 if mask & MASK_SCHEDULE_ON else 0
        settings["schtempr_c"] = decode_record_temperature(sch_tempr)
        settings["schtime"] = f"{raw[9]:02d}:{raw[8]:02d}"
    if mask & MASK_CLOCK:
        settings["clock"] = f"{raw[11]:02d}:{raw[10]:02d}"
    if mask & MASK_CLOCK_MODE:
        settings["clockmode"] = raw[12]
    if mask & MASK_HOLD:
        settings["hold"] = raw[13]
    if mask & MASK_CHIME:
        settings["chime"] = raw[14]
    if mask & MASK_LANGUAGE:
        settings["language"] = raw[15]
    if mask & MASK_BOIL:
        settings["boil"] = 1 if mask & MASK_BOIL_ON else 0
    return settings


class StaggLinkCoordinator(DataUpdateCoordinator):
    """Central instance to fetch data from the kettle CLI API."""

    def __init__(self, hass, ip, update_interval_seconds):
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="StaggLink Kettle",
            # Convert integer seconds into a timedelta object
            update_interval=timedelta(seconds=update_interval_seconds),
        )
        self.ip = ip
        self.session = async_get_clientsession(hass)
        # Firmware 1.2.26+ doesn't report once vs. repeat, so remember the last mode we set
        self.schedule_repeat = None

    async def _async_update_data(self):
        """Fetch data from the kettle, preferring the native API of newer firmware."""
        try:
            async with async_timeout.timeout(20):
                data = await self._read_native()
                if data is None:
                    data = await self._read_legacy()
                return data
        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"Connection error: {err}")

    async def _read_native(self):
        """Read state and settings from firmware 1.2.26+, or return None on older firmware."""
        response = await self.session.get(f"http://{self.ip}/temp")
        if response.status != 200:
            return None
        text = await response.text()
        try:
            # The kettle emits a bare `nan` (invalid JSON) when lifted off the base
            status = json.loads(re.sub(r':\s*nan\b', ':NaN', text))
        except ValueError:
            return None
        if not isinstance(status, dict) or "state" not in status:
            return None

        response_settings = await self.session.get(f"http://{self.ip}/{NATIVE_SETTINGS_PATH}")
        response_settings.raise_for_status()
        try:
            settings = decode_settings_record(await response_settings.read())
        except ValueError as err:
            raise UpdateFailed(f"Parsing error: {err}")

        schedon = settings.get("schedon", 0)
        if schedon == 0:
            schedule_mode = "off"
        elif self.schedule_repeat:
            schedule_mode = "repeat"
        else:
            schedule_mode = "once"

        hold = settings.get("hold", 0)
        chime = settings.get("chime", 0)
        return {
            "temp": parse_float(status.get("temp")),
            "target": parse_float(status.get("temp_set")),
            "mode": status["state"],
            "hold_time_minutes": settings.get("hold", 30),
            "hold_enabled": 1 if hold > 0 else 0,
            "pre_boil_enabled": settings.get("boil", 0),
            "chime_enabled": 1 if chime > 0 else 0,
            "chime_volume": chime,
            "altitude_meters": settings.get("altitude", 0),
            "language": settings.get("language", 0),
            "units": settings["units"],
            "clock_mode": settings.get("clockmode", 0),
            "clock_time": settings.get("clock"),
            "schedule_enabled": schedon,
            "schedule_mode": schedule_mode,
            "schedule_time": settings.get("schtime"),
            # Hide temperature if schedule is off
            "schedule_temperature": settings.get("schtempr_c") if schedon else None,
        }

    async def _read_legacy(self):
        """Read state and settings from the text CLI of firmware older than 1.2.26."""
        state_url = f"http://{self.ip}/cli?cmd=state"
        settings_url = f"http://{self.ip}/cli?cmd=prtsettings"

        # 1. Fetch current device state
        response = await self.session.get(state_url)
        response.raise_for_status()
        state_text = await response.text()

        # 2. Fetch system settings dump
        response_settings = await self.session.get(settings_url)
        response_settings.raise_for_status()
        settings_text = await response_settings.text()

        temp_match = re.search(r'tempr=([0-9.]+|nan)', state_text)
        target_match = re.search(r'temprT=([0-9.]+|nan)', state_text)
        mode_match = re.search(r'mode=([a-zA-Z0-9_]+)', state_text)

        if not temp_match or not target_match or not mode_match:
            raise UpdateFailed(f"Parsing error. Response: {state_text}")

        def get_setting_value(name, default=None):
            match = re.search(rf'\b{name}\s*[:=]\s*([a-zA-Z0-9_.-]+)', settings_text, re.IGNORECASE)
            if match:
                val = match.group(1).strip()
                try:
                    if '.' in val:
                        return float(val)
                    return int(val)
                except ValueError:
                    return val
            return default

        def get_time_value(name, default=None):
            """Parse HH:MM values (colons not matched by get_setting_value)."""
            match = re.search(rf'\b{name}\s*=\s*([0-9]+:[0-9]+)', settings_text, re.IGNORECASE)
            return match.group(1) if match else default

        # 3. Fetch current clock time from prtclock
        clock_time = None
        try:
            response_clock = await self.session.get(f"http://{self.ip}/cli?cmd=prtclock")
            response_clock.raise_for_status()
            clock_text = await response_clock.text()
            clock_match = re.search(r'clock=([0-9]+:[0-9]+)', clock_text)
            if clock_match:
                h, m = clock_match.group(1).split(":")
                clock_time = f"{int(h):02d}:{int(m):02d}"
        except Exception:
            pass

        # Schedule temperature: "schtempr=N F (X C ...)" or "schtempr=N C (X C ...)"
        sch_tempr_c = None
        sch_match = re.search(r'schtempr\s*=\s*.*?\s*(-?[0-9.]+)\s*C', settings_text)
        if sch_match:
            try:
                sch_tempr_c = float(sch_match.group(1))
            except ValueError:
                pass

        schedon = get_setting_value("schedon", 0)
        repeat_sched = get_setting_value("Repeat_sched", 0)
        if schedon == 0:
            schedule_mode = "off"
            sch_tempr_c = None # Hide temperature if schedule is off
        elif repeat_sched:
            schedule_mode = "repeat"
        else:
            schedule_mode = "once"

        return {
            "temp": parse_float(temp_match.group(1)),
            "target": parse_float(target_match.group(1)),
            "mode": mode_match.group(1),
            "hold_time_minutes": get_setting_value("hold", 30),
            "hold_enabled": 1 if get_setting_value("hold", 0) > 0 else 0,
            "pre_boil_enabled": get_setting_value("boil", 0),
            "chime_enabled": 1 if get_setting_value("chime", 0) > 0 else 0,
            "chime_volume": get_setting_value("chime", 0),
            "altitude_meters": get_setting_value("altitude", 0),
            "language": get_setting_value("language", 0),
            "units": "C" if get_setting_value("units", 1) == 1 else "F",
            "clock_mode": get_setting_value("clockmode", 0),
            "clock_time": clock_time,
            "schedule_enabled": schedon,
            "schedule_mode": schedule_mode,
            "schedule_time": get_time_value("schtime"),
            "schedule_temperature": sch_tempr_c,
        }
