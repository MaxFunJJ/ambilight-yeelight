from __future__ import annotations

import asyncio
import logging
from itertools import repeat

import voluptuous as vol

import homeassistant.helpers.config_validation as cv

from homeassistant.components.switch import (
    # DOMAIN, 
    SwitchEntity, 
    ENTITY_ID_FORMAT,
    PLATFORM_SCHEMA)

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_TRANSITION,
    ATTR_RGB_COLOR)
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.light import VALID_TRANSITION, is_on

from homeassistant.const import (
    CONF_LIGHTS,
    SERVICE_TURN_ON,
    SERVICE_TURN_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    ATTR_ENTITY_ID)
    
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from requests.auth import HTTPDigestAuth
from requests.adapters import HTTPAdapter

from haphilipsjs import PhilipsTV

from collections.abc import Callable

import string
from yeelight import *
# from datetime import timedelta

import math

_LOGGER = logging.getLogger(__name__)

# MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=1)

CONF_TV_ADDRESS, DEFAULT_TV_ADDRESS = "tv_address", "127.0.0.1"
CONF_API_VERSION, DEFAULT_API_VERSION = "api_version", 6
CONF_USERNAME, DEFAULT_USER = "username", "user"
CONF_PASSWORD, DEFAULT_PASS = "password", "pass"
CONF_NAME, DEFAULT_NAME = "name", "Ambilights+Yeelight"
CONF_AMBI_REGION, DEFAULT_AMBI_REGION = "ambi_region", "top"
CONF_LIGHTS_RGB = "lights_rgb"
CONF_LIGHTS_CT = "lights_ct"
CONF_YEELIGHTS, DEFAULT_YEELIGHTS = "yeelights", "127.0.0.1"
CONF_ICON, DEFAULT_ICON = "icon", "mdi:television-ambient-light"
CONF_MIN_BRIGHTNESS, DEFAULT_MIN_BRIGHTNESS = "min_brightness", 1
CONF_MAX_BRIGHTNESS, DEFAULT_MAX_BRIGHTNESS = "max_brightness", 100

BASE_URL = 'https://{0}:1926/6/{1}' # for older philps tv's, try changing this to 'http://{0}:1925/1/{1}'
TIMEOUT = 5.0 # get/post request timeout with tv
CONNFAILCOUNT = 5 # number of get/post attempts
DEFAULT_RGB_COLOR = [255,255,255] # default colour for bulb when dimmed in game mode (and incase of failure) 


## Future develop
# The following line tracks entity states
# async_track_state_change(self.hass, list(entities), sensor_state_listener)

RESOURCE_SCHEMA = vol.Any(
    {
        vol.Optional(CONF_YEELIGHTS): cv.string,
        vol.Optional(CONF_LIGHTS_RGB): cv.entity_ids,
        vol.Optional(CONF_LIGHTS_CT): cv.entity_ids,
        vol.Optional(CONF_AMBI_REGION, default=DEFAULT_AMBI_REGION): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_ICON, default=DEFAULT_ICON): cv.icon,
        vol.Optional(CONF_MIN_BRIGHTNESS, default=DEFAULT_MIN_BRIGHTNESS): cv.positive_int,
        vol.Optional(CONF_MAX_BRIGHTNESS, default=DEFAULT_MAX_BRIGHTNESS): cv.positive_int
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        # vol.Required(CONF_PLATFORM): "philips_ambilight+yeelight",
        vol.Required(CONF_TV_ADDRESS): cv.string,
        vol.Optional(CONF_API_VERSION, default=DEFAULT_API_VERSION): cv.string,
        vol.Required(CONF_USERNAME, default=DEFAULT_USER): cv.string,
        vol.Required(CONF_PASSWORD, default=DEFAULT_PASS): cv.string,
        vol.Required(CONF_LIGHTS): vol.Schema({cv.string: RESOURCE_SCHEMA}),
        # vol.Required(CONF_LIGHTS): vol.Schema({cv.ensure_list: RESOURCE_SCHEMA}),
    }
)

async def async_setup_platform(
            hass: HomeAssistant, 
            config, 
            async_add_entities: AddEntitiesCallback,
            discovery_info: DiscoveryInfoType | None = None
        ) -> None:
    # philips_ambi_lighting = hass.data.get(DOMAIN)
    tvip = config.get(CONF_TV_ADDRESS)
    user = config.get(CONF_USERNAME)
    password = config.get(CONF_PASSWORD)
    resources = config.get(CONF_LIGHTS)
    api_version = config.get(CONF_API_VERSION)

    tv_coordinator = AmbiHue(hass, tvip, api_version, user, password)

    dev: list[SwitchEntity] = []
    for entry, data in resources.items():
        name = data.get(CONF_NAME)
        option = data.get(CONF_AMBI_REGION)
        icon = data.get(CONF_ICON)
        lights_yeelight_ips = data.get(CONF_YEELIGHTS)
        lights_rgb = data.get(CONF_LIGHTS_RGB, [])
        lights_ct = data.get(CONF_LIGHTS_CT, [])
        min_brightness = data.get(CONF_MIN_BRIGHTNESS)
        max_brightness = data.get(CONF_MAX_BRIGHTNESS)

        if lights_yeelight_ips is not None:
            dev.append(
                AmbiHueYeeSwitch(
                    hass, tv_coordinator, name, lights_yeelight_ips, option, icon, min_brightness, max_brightness
                )
            )

        if lights_rgb is not None:
            dev.append(
                AmbiHueRgbLightSwitch(
                    hass, tv_coordinator, name, lights_rgb, option, icon, min_brightness, max_brightness
                )
            )
        if lights_ct is not None:
            dev.append(
                AmbiHueCtLightSwitch(
                    hass, tv_coordinator, name, lights_ct, option, icon, min_brightness, max_brightness
                )
            )

    async_add_entities(dev, True)

class AmbiHueYeeSwitch(SwitchEntity):

    def __init__(self, hass: HomeAssistant, tv_coordinator: AmbiHue, name, bulbips: string, option, icon, min_brightness, max_brightness) -> None:
        self._hass = hass
        self._name = name
        self._position = option
        self._icon = icon
        self._is_on = False
        self._connfail = 0
        self._available = False
        self._ambihue: AmbiHue = tv_coordinator

        self._bulbips = bulbips.split(', ')
        self._bulbs: list[Bulb] = []
        for address in self._bulbips:
            self._bulbs.append(Bulb(address))

        self._brightness_pct = 30 # initial brightness
        self._min_brightness_pct = min_brightness
        self._max_brightness_pct = max_brightness
        self._brightness = int((self._brightness_pct / 100) * 254) # initial brightness
        self._min_brightness = int((self._min_brightness_pct / 100) * 254)
        self._max_brightness = int((self._max_brightness_pct / 100) * 254)

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return self._icon

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def available(self):
        return self._available

    async def async_turn_on(self, **kwargs):
        await self.async_turn_on_bulbs_and_music()
        await self.async_update()
        if self._is_on:
            self._follow = True
            self._ambihue.add_listener(self)
            _LOGGER.debug('AmbiYeelight turned on')
    
    async def async_turn_on_bulbs_and_music(self):
        await asyncio.gather(*(self.async_turn_on_bulb_and_music(bulb) for bulb in self._bulbs), return_exceptions=True)
    
    async def async_turn_on_bulb_and_music(self, bulb: Bulb):
        powerstate, musicmode = await self.async_getState(bulb)
        if not powerstate:
            bulb.turn_on()
        if not musicmode:
            bulb.start_music()

    async def async_turn_on_bulbs(self):
        await asyncio.gather(*(self.async_turn_on_bulb(bulb) for bulb in self._bulbs), return_exceptions=True)

    async def async_turn_on_bulb(self, bulb: Bulb):
        powerstate, musicmode = await self.async_getState(bulb)
        if not powerstate:
            bulb.turn_on()

    async def async_turn_off_bulbs(self):
        await asyncio.gather(*(self.async_turn_off_bulb(bulb) for bulb in self._bulbs), return_exceptions=True)
    
    async def async_turn_off_bulb(self, bulb: Bulb):
        powerstate, musicmode = await self.async_getState(bulb)
        if powerstate:
            bulb.turn_off()
    
    async def async_start_music_bulbs(self):
        await asyncio.gather(*(self.async_start_music_bulb(bulb) for bulb in self._bulbs), return_exceptions=True)
    
    async def async_start_music_bulb(self, bulb: Bulb):
        powerstate, musicmode = await self.async_getState(bulb)
        if not musicmode:
            bulb.start_music()

    async def async_stop_music_bulbs(self):
        await asyncio.gather(*(self.async_stop_music_bulb(bulb) for bulb in self._bulbs), return_exceptions=True)
    
    async def async_stop_music_bulb(self, bulb: Bulb):
        powerstate, musicmode = await self.async_getState(bulb)
        if musicmode:
            bulb.stop_music()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._ambihue.remove_listener(self)
        self._follow = False
        self._is_on = False
        await self.async_stop_music_bulbs() # disables (more intensive) music mode afterward
        await self.async_turn_off_bulbs()
        _LOGGER.debug('AmbiYeelight turned off')

    async def async_getState(self, bulb: Bulb):
        power_on = False
        musicmode = False
        try:
            properties = bulb.get_properties()
            if properties:
                powerstate = properties['power']
                musicmode = bulb.music_mode
                self._available = True
            else:
                powerstate = 'off'
            if powerstate == 'on':
                power_on = True
        except Exception as e:
            _LOGGER.error('The following error occured while getting the yeelight state: ' + str(e))
        return power_on, musicmode

    async def async_update(self) -> None:
        for bulb in self._bulbs:
            powerstate, musicmode = await self.async_getState(bulb)
            if powerstate and musicmode:
                self._is_on = True
            else:
                self._is_on = False
                return

    async def async_is_update_needed(self, r, g, b, brightness):
        if brightness != self._brightness:
            return True
        if r != self._r:
            return True
        if g != self._g:
            return True
        if b != self._b:
            return True
        return False

    async def async_set_bulb(self, r, g, b, brightness, bulb: Bulb, ambiSetting):
        try:
            if brightness < self._min_brightness:
                brightness = self._min_brightness
            if self._max_brightness_pct < 100:
                brightness = brightness / 100 * self._max_brightness_pct
            if brightness > self._max_brightness:
                brightness = self._max_brightness

            if r == None and g == None and b == None: # incase of a failure somewhere
                _LOGGER.error('RGB values are None.')
                r,g,b = DEFAULT_RGB_COLOR[0], DEFAULT_RGB_COLOR[1], DEFAULT_RGB_COLOR[2]
                bulb.set_brightness(1)
            
            if r == 0 and g == 0 and b == 0: # dim bulb in game mode
                if 'menuSetting' in ambiSetting and ambiSetting['menuSetting'] == "GAME":
                    r,g,b = DEFAULT_RGB_COLOR[0], DEFAULT_RGB_COLOR[1], DEFAULT_RGB_COLOR[2]
                    bulb.set_brightness(1)
            else:
                if ambiSetting['styleName'] == "FOLLOW_VIDEO":
                    transitions = [RGBTransition(r,g,b,duration=300,brightness=brightness)] # this transition can be customised (see: https://yeelight.readthedocs.io/en/latest/yeelight.html#yeelight.Flow)
                else:
                    transitions = [RGBTransition(r,g,b,duration=200,brightness=brightness)]
                flow = Flow(
                    count=1,
                    action=Flow.actions.stay,
                    transitions=transitions)
                bulb.start_flow(flow)
                self._brightness = brightness
                self._r, self._g, self._b = r, g, b
                return True
        except Exception as e:
            _LOGGER.error('Failed to set the bulb color values with error (going to try to start the music mode again):' + str(e))
            await self.async_turn_on_bulb_and_music(bulb)
            return False

    async def async_update_bulbs(self):
        try:
            r, g, b = await self._ambihue.async_get_rgb(self._ambihue._layer, self._position)
            brightness = await self._ambihue.async_get_brightness(r, g, b)
            if not self.async_is_update_needed(r, g, b, brightness):
                return True
            ambiSetting = self._ambihue._api.ambilight_current_configuration
            await asyncio.gather(*(self.async_set_bulb(r, g, b, brightness, bulb, ambiSetting) for bulb in self._bulbs), return_exceptions=True)
        except:
            _LOGGER.error('Failed async_update_bulbs: ' + str(e))
            return False

class AmbiHueRgbLightSwitch(SwitchEntity):

    def __init__(self, hass: HomeAssistant, tv_coordinator: AmbiHue, name, lights_rgb: string, option, icon, min_brightness, max_brightness) -> None:
        self._hass = hass
        self._name = name
        self._position = option
        self._icon = icon
        self._is_on = False
        self._connfail = 0
        self._available = False
        self._ambihue: AmbiHue = tv_coordinator

        self._lights_types = dict(zip(lights_rgb, repeat("rgb")))
        self._lights = list(self._lights_types.keys())

        self._brightness_pct = 30 # initial brightness
        self._min_brightness_pct = min_brightness
        self._max_brightness_pct = max_brightness
        self._brightness = int((self._brightness_pct / 100) * 254) # initial brightness
        self._min_brightness = int((self._min_brightness_pct / 100) * 254)
        self._max_brightness = int((self._max_brightness_pct / 100) * 254)

        self._r = 0
        self._g = 0
        self._b = 0

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return self._icon

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def available(self):
        return self._available

    async def async_turn_on(self, **kwargs):
        # await self._ambihue.async_update()
        await self.async_turn_on_bulbs()
        await self.async_update()
        if self._is_on:
            self._follow = True
            self._ambihue.add_listener(self)
            _LOGGER.debug('Ambi RGB Light turned on')

    async def async_turn_on_bulbs(self):
        tasks = []
        for light in self._lights:
            if is_on(self.hass, light):
                self._is_on = True
                continue
            service_data = {ATTR_ENTITY_ID: light, ATTR_TRANSITION: 1}
            if self._brightness is not None:
                service_data[ATTR_BRIGHTNESS] = int((self._brightness / 100) * 254)
            tasks.append(
            self.hass.services.async_call(
                    LIGHT_DOMAIN, SERVICE_TURN_ON, service_data
                )
            )
        if tasks:
            await asyncio.wait(tasks, timeout=2)
            # await self.async_update()
            

    async def async_turn_on_bulb(self, light):
        if is_on(self.hass, light):
            self._is_on = True
            return
        service_data = {ATTR_ENTITY_ID: light, ATTR_TRANSITION: 1}
        if self._brightness is not None:
            service_data[ATTR_BRIGHTNESS] = int((self._brightness / 100) * 254)
        await self.hass.services.async_call(
                LIGHT_DOMAIN, SERVICE_TURN_ON, service_data
            )
        # await self.async_update()

    async def async_turn_off_bulbs(self):
        tasks = []
        for light in self._lights:
            if not is_on(self.hass, light):
                continue
            service_data = {ATTR_ENTITY_ID: light, ATTR_TRANSITION: 1}
            tasks.append(
            self.hass.services.async_call(
                    LIGHT_DOMAIN, SERVICE_TURN_OFF, service_data
                )
            )
        if tasks:
            await asyncio.wait(tasks, timeout=2)
            await self.async_update()
    
    async def async_turn_off_bulb(self, light):
        if not is_on(self.hass, light):
            self._is_on = False
            return
        service_data = {ATTR_ENTITY_ID: light, ATTR_TRANSITION: 1}
        await self.hass.services.async_call(
                LIGHT_DOMAIN, SERVICE_TURN_OFF, service_data
            )
        await self.async_update()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._ambihue.remove_listener(self)
        self._follow = False
        self._is_on = False
        await self.async_turn_off_bulbs()
        _LOGGER.debug('Ambi rgblight turned off')

    async def async_update(self) -> None:
        for light in self._lights:
            if self.hass.states.get(light) == STATE_UNAVAILABLE:
                self._available = False
            else:
                self._available = True
            if not is_on(self.hass, light):
                self._is_on = False
                return
    
    async def async_is_update_needed(self, r, g, b, brightness):
        if brightness != self._brightness:
            return True
        if r != self._r:
            return True
        if g != self._g:
            return True
        if b != self._b:
            return True
        return False

    async def async_update_bulbs(self):
        try:
            r, g, b = await self._ambihue.async_get_rgb(self._ambihue._layer, self._position)
            brightness = await self._ambihue.async_get_brightness(r, g, b)
            
            if not self.async_is_update_needed(r, g, b, brightness):
                return True
            
            if brightness < self._min_brightness:
                brightness = self._min_brightness
            if self._max_brightness_pct < 100:
                brightness = brightness / 100 * self._max_brightness_pct
            if brightness > self._max_brightness:
                brightness = self._max_brightness
            ambiSetting = self._ambihue._api.ambilight_current_configuration
            duration = 200 / 1000
            if r == None and g == None and b == None: # incase of a failure somewhere
                _LOGGER.error('RGB values are None.')
                r,g,b = DEFAULT_RGB_COLOR[0], DEFAULT_RGB_COLOR[1], DEFAULT_RGB_COLOR[2]
                brightness = self._min_brightness
            if 'menuSetting' in ambiSetting and ambiSetting['menuSetting'] == "GAME":
                if r == 0 and g == 0 and b == 0: # dim bulb in game mode
                    r,g,b = DEFAULT_RGB_COLOR[0], DEFAULT_RGB_COLOR[1], DEFAULT_RGB_COLOR[2]
                    brightness = self._min_brightness
            else:
                if ambiSetting['styleName'] == "FOLLOW_VIDEO":
                    duration = 300 / 1000

            tasks = []
            for light in self._lights:
                service_data = {ATTR_ENTITY_ID: light, ATTR_TRANSITION: duration}
                service_data[ATTR_BRIGHTNESS] = int(brightness)
                service_data[ATTR_RGB_COLOR] = (int(r), int(g), int(b))
                tasks.append(
                self.hass.services.async_call(
                        LIGHT_DOMAIN, SERVICE_TURN_ON, service_data
                    )
                )
            if tasks:
                await asyncio.wait(tasks, timeout=2)
                self._brightness = brightness
                self._r, self._g, self._b = r, g, b
            return True
        except Exception as e:
            _LOGGER.error('Unable to set the light colors' + str(e))
            return False

class AmbiHueCtLightSwitch(SwitchEntity):

    def __init__(self, hass: HomeAssistant, tv_coordinator: AmbiHue, name, lights_ct: string, option, icon, min_brightness, max_brightness) -> None:
        self._hass = hass
        self._name = name
        self._position = option
        self._icon = icon
        self._is_on = False
        self._connfail = 0
        self._available = False
        self._ambihue: AmbiHue = tv_coordinator

        self._lights_types = dict(zip(lights_ct, repeat("ct")))
        self._lights = list(self._lights_types.keys())

        self._brightness_pct = 30 # initial brightness
        self._min_brightness_pct = min_brightness
        self._max_brightness_pct = max_brightness
        self._brightness = int((self._brightness_pct / 100) * 254) # initial brightness
        self._min_brightness = int((self._min_brightness_pct / 100) * 254)
        self._max_brightness = int((self._max_brightness_pct / 100) * 254)

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return self._icon

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def available(self):
        return self._available

    async def async_turn_on(self, **kwargs):
        await self.async_turn_on_bulbs()
        await self.async_update()
        if self._is_on:
            self._follow = True
            self._ambihue.add_listener(self)
            _LOGGER.debug('Ambi CT Light turned on')

    async def async_turn_on_bulbs(self):
        tasks = []
        for light in self._lights:
            if is_on(self.hass, light):
                self._is_on = True
                continue
            service_data = {ATTR_ENTITY_ID: light, ATTR_TRANSITION: 1}
            if self._brightness is not None:
                service_data[ATTR_BRIGHTNESS] = int((self._brightness / 100) * 254)
            tasks.append(
            self.hass.services.async_call(
                    LIGHT_DOMAIN, SERVICE_TURN_ON, service_data
                )
            )
        if tasks:
            await asyncio.wait(tasks, timeout=5)           

    async def async_turn_on_bulb(self, light):
        if is_on(self.hass, light):
            self._is_on = True
            return
        service_data = {ATTR_ENTITY_ID: light, ATTR_TRANSITION: 1}
        if self._brightness is not None:
            service_data[ATTR_BRIGHTNESS] = int((self._brightness / 100) * 254)
        await self.hass.services.async_call(
                LIGHT_DOMAIN, SERVICE_TURN_ON, service_data
            )

    async def async_turn_off_bulbs(self):
        tasks = []
        for light in self._lights:
            if not is_on(self.hass, light):
                continue
            service_data = {ATTR_ENTITY_ID: light, ATTR_TRANSITION: 1}
            tasks.append(
            self.hass.services.async_call(
                    LIGHT_DOMAIN, SERVICE_TURN_OFF, service_data
                )
            )
        if tasks:
            await asyncio.wait(tasks, timeout=2)
            await self.async_update()
    
    async def async_turn_off_bulb(self, light):
        if not is_on(self.hass, light):
            self._is_on = False
            return
        service_data = {ATTR_ENTITY_ID: light, ATTR_TRANSITION: 1}
        await self.hass.services.async_call(
                LIGHT_DOMAIN, SERVICE_TURN_OFF, service_data
            )
        await self.async_update()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._ambihue.remove_listener(self)
        self._follow = False
        self._is_on = False
        await self.async_turn_off_bulbs()
        _LOGGER.debug('Ambi ctlight turned off')

    async def async_update(self) -> None:
        for light in self._lights:
            if self.hass.states.get(light) == STATE_UNAVAILABLE:
                self._available = False
            else:
                self._available = True
            if not is_on(self.hass, light):
                self._is_on = False
                return

    async def async_is_update_needed(self, brightness):
        if brightness != self._brightness:
            return True
        return False

    async def async_update_bulbs(self):
        try:
            r, g, b = await self._ambihue.async_get_rgb(self._ambihue._layer, self._position)
            brightness = await self._ambihue.async_get_brightness(r, g, b)

            if not self.async_is_update_needed(brightness):
                return True

            if brightness < self._min_brightness:
                brightness = self._min_brightness
            if self._max_brightness_pct < 100:
                brightness = brightness / 100 * self._max_brightness_pct
            if brightness > self._max_brightness:
                brightness = self._max_brightness
            ambiSetting = self._ambihue._api.ambilight_current_configuration
            duration = 200 / 1000
            if r == None and g == None and b == None: # incase of a failure somewhere
                _LOGGER.error('RGB values are None.')
                r,g,b = DEFAULT_RGB_COLOR[0], DEFAULT_RGB_COLOR[1], DEFAULT_RGB_COLOR[2]
                brightness = self._min_brightness
            if 'menuSetting' in ambiSetting and ambiSetting['menuSetting'] == "GAME":
                if r == 0 and g == 0 and b == 0: # dim bulb in game mode
                    r,g,b = DEFAULT_RGB_COLOR[0], DEFAULT_RGB_COLOR[1], DEFAULT_RGB_COLOR[2]
                    brightness = self._min_brightness
            else:
                if ambiSetting['styleName'] == "FOLLOW_VIDEO":
                    duration = 300 / 1000
            tasks = []
            for light in self._lights:
                service_data = {ATTR_ENTITY_ID: light, ATTR_TRANSITION: duration}
                service_data[ATTR_BRIGHTNESS] = int(brightness)
                tasks.append(
                self.hass.services.async_call(
                        LIGHT_DOMAIN, SERVICE_TURN_ON, service_data
                    )
                )
            if tasks:
                await asyncio.wait(tasks, timeout=2)
                self._brightness = brightness
            return True
        except Exception as e:
            _LOGGER.error('Unable to set the light colors' + str(e))
            return False

class AmbiHue:
    """The class for handling the data retrieval."""
    
    def __init__(self, hass: HomeAssistant, tvip, api_version, user, password) -> None:
        self._hass = hass
        self._ambihueip = tvip
        self._user = user
        self._password = password

        self._follow = False
        self._on_update: list[Callable] = []
        self._layer = None
        self._api = PhilipsTV(self._ambihueip, api_version, username=self._user, password=self._password)

    async def async_update(self):
        _LOGGER.info('Update TV info')
        await self._api.update()

    async def async_get_ambilayer(self):
        if self._api.ambilight_current_configuration is None:
            return None
        try:
            if self._api.ambilight_current_configuration['styleName'] == "FOLLOW_VIDEO":
                currentstate = await self._api.getAmbilightMeasured() # uses pre-processing r,g,b values from tv (see: http://jointspace.sourceforge.net/projectdata/documentation/jasonApi/1/doc/API-ambilight.html)
            else:
                currentstate = await self._api.getAmbilightProcessed() # uses post-processing r,g,b values from tv (allows yeelight bulb to follow tv's algorithms such as the follow audio effects and colours set by home assistant)
            self._layer = currentstate['layer1']
        except Exception as e:
            self._layer = None
            _LOGGER.error('Failed to get ambilight layer with error:' + str(e))
        return self._layer

    async def async_follow_ambilight(self, sleep, duration):
        _LOGGER.info('Follow ambilight for ' + str(duration / sleep) + ' times for the next ' + str(duration) + ' seconds')
        counter = 0
        while self._follow == True and counter < (duration / sleep): # second loop for updating the bulb
            await self.async_get_ambilayer()
            if self._layer is None:
                _LOGGER.error('self._layer is None.')
            else:
                try:
                    await self.notify_listeners()
                    counter += 1
                except Exception as e:
                    _LOGGER.error('Failed to transfer color values with error (from second loop):' + str(e))
                    # self._follow = False
            await asyncio.sleep(sleep)
        return counter

    async def async_follow_tv(self, sleep):
        _LOGGER.debug('Starting async_follow_tv')
        while self._follow == True: # main loop for updating the bulb
            try:
                await self._api.getAmbilightCurrentConfiguration()
                if self._api.ambilight_current_configuration is None:
                    _LOGGER.error('AmbiSetting is None. Trying again in 5 seconds')
                    await self._api.update()
                    await asyncio.sleep(5)
                    continue
                
                await self._api.getPowerState()
                await self._api.getAmbilightPower()
                if self._api.ambilight_power == 'On' and self._api.powerstate == 'On':
                    counter = await self.async_follow_ambilight(sleep, 10)
                    if counter < (10/sleep) * 0.5:
                        _LOGGER.info('Unable to refresh the ambilight layer as often as configured.')
                        await self._api.update()
                        await asyncio.sleep(5)
                    if counter < 2:
                        _LOGGER.info('Unable to refresh the ambilight layer as often as configured..')
                        await self._api.update()
                        await asyncio.sleep(30)
                elif not self._api.powerstate == 'On':
                    _LOGGER.info('The TV seems to be turned OFF but reachable, therefore going to check the ambicolors and then wait 5 seconds before checking again.')
                    counter = await self.async_follow_ambilight(1, 1)
                    await asyncio.sleep(4)
                    continue
                else:
                    _LOGGER.info('The ambilight seems to be turned OFF, checking again in 5 seconds.')
                    await asyncio.sleep(5)
                    continue

            except Exception as e:
                _LOGGER.error('Failed to transfer color values with error (from main loop):' + str(e))
                self._follow = False
                return False
        return True
    
    def add_listener(self, listener):
        _LOGGER.info('Added listener')
        if listener in self._on_update:
            self._on_update.remove(listener)
        self._on_update.append(listener)
        if len(self._on_update) > 0:
            self.start_following()
            _LOGGER.info('Added listener, there are ' + str(len(self._on_update)) + ' listeners.')

    def remove_listener(self, listener):
        if listener in self._on_update:
            self._on_update.remove(listener)
            _LOGGER.info('Removed listener, there are ' + str(len(self._on_update)) + ' listeners remaining.')
        if len(self._on_update) == 0:
            _LOGGER.info('The last listener is being removed')
            self.stop_following()

    def remove_listeners(self):
        _LOGGER.info('Removed listeners')
        self._on_update.clear()
    
    async def notify_listeners(self):
        tasks = []
        try:
            await asyncio.gather(*(listener.async_update_bulbs() for listener in self._on_update), return_exceptions=True)
        except Exception as e:
                _LOGGER.error('Error occured while notifying the listeners. ' + str(e))

    def start_following(self):
        if not self._follow:
            _LOGGER.info('Start following')
            self._follow = True
            self._future = asyncio.ensure_future(self.async_follow_tv(0.1))

    def stop_following(self):
        _LOGGER.info('Stop following (because there are no more lights listening)')
        self._follow = False

    async def async_get_brightness(self, r, g, b):
        try:
            brightness = math.sqrt(
                r * r * .241 + 
                g * g * .691 + 
                b * b * .068
            )
            if brightness == 0:
                brightness = 1
        except:
            brightness = 5
        return brightness

    async def async_get_rgb(self, layer1, position):
        r,g,b = None, None, None

        if layer1 is None:
            return r,g,b

        # below calulates different average r,g,b values to send to the lamp
        # see: http://jointspace.sourceforge.net/projectdata/documentation/jasonApi/1/doc/API-Method-ambilight-measured-GET.html
        # etc in http://jointspace.sourceforge.net/projectdata/documentation/jasonApi/1/doc/API.html
        
        if position == 'top-middle-average': # 'display_options' value given in home assistant 
            pixels = layer1['top'] # for tv topology see http://jointspace.sourceforge.net/projectdata/documentation/jasonApi/1/doc/API-Method-ambilight-topology-GET.html
            pixel3 = str((int(len(pixels)/2)-1)) # selects pixels
            pixel4 = str(int(len(pixels)/2))
            r = int( ((pixels[pixel3]['r'])**2+(pixels[pixel4]['r'])**2) ** (1/2) ) # function to calulcate desired values
            g = int( ((pixels[pixel3]['g'])**2+(pixels[pixel4]['g'])**2) ** (1/2) )
            b = int( ((pixels[pixel3]['b'])**2+(pixels[pixel4]['b'])**2) ** (1/2) )
            # r,g and b used later in the bulb transition/flow
        
        elif position == 'top-average':
            pixels = layer1['top']
            r_sum, g_sum, b_sum = 0,0,0
            for i in range(0,len(pixels)):
                pixel = str(int(i))
                r_sum = r_sum + ((pixels[pixel]['r']) ** 2)
                g_sum = g_sum + ((pixels[pixel]['g']) ** 2)
                b_sum = b_sum + ((pixels[pixel]['b']) ** 2)
            r = int((r_sum/len(pixels))**(1/2))
            g = int((g_sum/len(pixels))**(1/2))
            b = int((b_sum/len(pixels))**(1/2))
        elif position == 'right-average':
            pixels = layer1['right']
            r_sum, g_sum, b_sum = 0,0,0
            for i in range(0,len(pixels)):
                pixel = str(int(i))
                r_sum = r_sum + ((pixels[pixel]['r']) ** 2)
                g_sum = g_sum + ((pixels[pixel]['g']) ** 2)
                b_sum = b_sum + ((pixels[pixel]['b']) ** 2)
            r = int((r_sum/len(pixels))**(1/2))
            g = int((g_sum/len(pixels))**(1/2))
            b = int((b_sum/len(pixels))**(1/2))
        elif position == 'left-average':
            pixels = layer1['left']
            r_sum, g_sum, b_sum = 0,0,0
            for i in range(0,len(pixels)):
                pixel = str(int(i))
                r_sum = r_sum + ((pixels[pixel]['r']) ** 2)
                g_sum = g_sum + ((pixels[pixel]['g']) ** 2)
                b_sum = b_sum + ((pixels[pixel]['b']) ** 2)
            r = int((r_sum/len(pixels))**(1/2))
            g = int((g_sum/len(pixels))**(1/2))
            b = int((b_sum/len(pixels))**(1/2))
        elif position == 'bottom-average':
            pixels = layer1['bottom']
            r_sum, g_sum, b_sum = 0,0,0
            for i in range(0,len(pixels)):
                pixel = str(int(i))
                r_sum = r_sum + ((pixels[pixel]['r']) ** 2)
                g_sum = g_sum + ((pixels[pixel]['g']) ** 2)
                b_sum = b_sum + ((pixels[pixel]['b']) ** 2)
            r = int((r_sum/len(pixels))**(1/2))
            g = int((g_sum/len(pixels))**(1/2))
            b = int((b_sum/len(pixels))**(1/2))
        elif position == 'top-middle' or position == 'top-center' or position == 'top':
            pixels = layer1['top']
            pixel = str(int(len(pixels)/2))
            r = int(pixels[pixel]['r'])
            g = int(pixels[pixel]['g'])
            b = int(pixels[pixel]['b'])
        elif position == 'bottom-middle' or position == 'bottom-center' or position == 'bottom':
            pixels = layer1['bottom']
            pixel = str(int(len(pixels)/2))
            r = int(pixels[pixel]['r'])
            g = int(pixels[pixel]['g'])
            b = int(pixels[pixel]['b'])
        elif position == 'right':
            pixels = layer1['right']
            pixel = str(int(len(pixels)/2))
            r = int(pixels[pixel]['r'])
            g = int(pixels[pixel]['g'])
            b = int(pixels[pixel]['b'])
        elif position == 'left':
            pixels = layer1['left']
            pixel = str(int(len(pixels)/2))
            r = int(pixels[pixel]['r'])
            g = int(pixels[pixel]['g'])
            b = int(pixels[pixel]['b'])
        elif position == 'top-right-average':
            r_sum, g_sum, b_sum = 0,0,0
            rightpixels = layer1['right']
            rtpixel = rightpixels['0']
            toppixels = layer1['top']
            trpixel = toppixels[str(int(len(toppixels)-1))]
            selected_pixels = [rtpixel,trpixel]
            for pixel in selected_pixels:
                r_sum = r_sum + ((pixel['r']) ** 2)
                g_sum = g_sum + ((pixel['g']) ** 2)
                b_sum = b_sum + ((pixel['b']) ** 2)
            r = int((r_sum/len(selected_pixels))*(1/2))
            g = int((g_sum/len(selected_pixels))*(1/2))
            b = int((b_sum/len(selected_pixels))*(1/2))
        elif position == 'top-left-average':
            r_sum, g_sum, b_sum = 0,0,0
            leftpixels = layer1['left']
            ltpixel = leftpixels[str(int(len(leftpixels)-1))]
            toppixels = layer1['top']
            tlpixel = toppixels['0']
            selected_pixels = [ltpixel,tlpixel]
            for pixel in selected_pixels:
                r_sum = r_sum + ((pixel['r']) ** 2)
                g_sum = g_sum + ((pixel['g']) ** 2)
                b_sum = b_sum + ((pixel['b']) ** 2)
            r = int((r_sum/len(selected_pixels))*(1/2))
            g = int((g_sum/len(selected_pixels))*(1/2))
            b = int((b_sum/len(selected_pixels))*(1/2))
        elif position == 'bottom-right-average':
            r_sum, g_sum, b_sum = 0,0,0
            rightpixels = layer1['right']
            rbpixel = rightpixels[str(int(len(rightpixels)-1))]
            bottompixels = layer1['bottom']
            rbpixel = bottompixels[str(int(len(bottompixels)-1))]
            selected_pixels = [rbpixel,brpixel]
            for pixel in selected_pixels:
                r_sum = r_sum + ((pixel['r']) ** 2)
                g_sum = g_sum + ((pixel['g']) ** 2)
                b_sum = b_sum + ((pixel['b']) ** 2)
            r = int((r_sum/len(selected_pixels))*(1/2))
            g = int((g_sum/len(selected_pixels))*(1/2))
            b = int((b_sum/len(selected_pixels))*(1/2))
        elif position == 'bottom-left-average':
            r_sum, g_sum, b_sum = 0,0,0
            leftixels = layer1['left']
            lbpixel = leftixels['0']
            bottompixels = layer1['bottom']
            blpixel = bottompixels['0']
            selected_pixels = [lbpixel,blpixel]
            for pixel in selected_pixels:
                r_sum = r_sum + ((pixel['r']) ** 2)
                g_sum = g_sum + ((pixel['g']) ** 2)
                b_sum = b_sum + ((pixel['b']) ** 2)
            r = int((r_sum/len(selected_pixels))*(1/2))
            g = int((g_sum/len(selected_pixels))*(1/2))
            b = int((b_sum/len(selected_pixels))*(1/2))
        elif position == 'right-top':
            pixels = layer1['right']
            r = int(pixels['0']['r'])
            g = int(pixels['0']['g'])
            b = int(pixels['0']['b'])
        elif position == 'left-top':
            pixels = layer1['left']
            pixel = str(int(len(pixels)-1))
            r = int(pixels[pixel]['r'])
            g = int(pixels[pixel]['g'])
            b = int(pixels[pixel]['b'])
        elif position == 'top-left':
            pixels = layer1['top']
            r = int(pixels['0']['r'])
            g = int(pixels['0']['g'])
            b = int(pixels['0']['b'])
        elif position == 'top-right':
            pixels = layer1['top']
            pixel = str(int(len(pixels)-1))
            r = int(pixels[pixel]['r'])
            g = int(pixels[pixel]['g'])
            b = int(pixels[pixel]['b'])
        elif position == 'right-bottom':
            pixels = layer1['right']
            pixel = str(int(len(pixels)-1))
            r = int(pixels[pixel]['r'])
            g = int(pixels[pixel]['g'])
            b = int(pixels[pixel]['b'])
        elif position == 'left-bottom':
            pixels = layer1['left']
            r = int(pixels['0']['r'])
            g = int(pixels['0']['g'])
            b = int(pixels['0']['b'])
        elif position == 'bottom-left':
            pixels = layer1['bottom']
            r = int(pixels['0']['r'])
            g = int(pixels['0']['g'])
            b = int(pixels['0']['b'])
        elif position == 'bottom-right':
            pixels = layer1['bottom']
            pixel = str(int(len(pixels)-1))
            r = int(pixels[pixel]['r'])
            g = int(pixels[pixel]['g'])
            b = int(pixels[pixel]['b'])
        return r, g, b
