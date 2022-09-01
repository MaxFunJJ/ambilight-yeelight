from __future__ import annotations

import asyncio

import logging

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components.switch import (
    DOMAIN, PLATFORM_SCHEMA, SwitchEntity, ENTITY_ID_FORMAT)
from homeassistant.const import (
    CONF_HOST, 
    CONF_NAME,
    CONF_ICON,
    CONF_USERNAME, 
    CONF_PASSWORD, 
    CONF_ADDRESS, 
    CONF_DISPLAY_OPTIONS,
    CONF_LIGHTS,
    CONF_API_VERSION,
    STATE_UNAVAILABLE, 
    STATE_OFF, 
    STATE_STANDBY, 
    STATE_ON)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from requests.auth import HTTPDigestAuth
from requests.adapters import HTTPAdapter

from haphilipsjs import PhilipsTV

from collections.abc import Callable

import json, string, requests
from yeelight import *
import time, random, urllib3
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)

MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=1)

DEFAULT_DEVICE = 'default'
DEFAULT_HOST = '127.0.0.1'
DEFAULT_USER = 'user'
DEFAULT_PASS = 'pass'
DEFAULT_NAME = 'Ambilights+Yeelight'
DEFAULT_ICON = "mdi:television-ambient-light"
DEFAULT_DISPLAY_OPTIONS = 'top'
BASE_URL = 'https://{0}:1926/6/{1}' # for older philps tv's, try changing this to 'http://{0}:1925/1/{1}'
TIMEOUT = 5.0 # get/post request timeout with tv
CONNFAILCOUNT = 5 # number of get/post attempts
DEFAULT_RGB_COLOR = [255,255,255] # default colour for bulb when dimmed in game mode (and incase of failure) 
DEFAULT_API_VERSION = 6

RESOURCE_SCHEMA = vol.Any({
	vol.Required(CONF_ADDRESS, default=DEFAULT_HOST): cv.string,
	vol.Optional(CONF_DISPLAY_OPTIONS, default=DEFAULT_DISPLAY_OPTIONS): cv.string,
	vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_ICON, default=DEFAULT_ICON): cv.icon
})

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
	vol.Required(CONF_HOST, default=DEFAULT_HOST): cv.string,
    vol.Required(CONF_API_VERSION, default=DEFAULT_API_VERSION): cv.string,
	vol.Required(CONF_USERNAME, default=DEFAULT_USER): cv.string,
	vol.Required(CONF_PASSWORD, default=DEFAULT_PASS): cv.string,
	vol.Required(CONF_LIGHTS): vol.Schema({cv.string: RESOURCE_SCHEMA}),
})

async def async_setup_platform(
    hass: HomeAssistant, 
    config: ConfigType, 
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None
) -> None:
    tvip = config[CONF_HOST]
    user = config[CONF_USERNAME]
    password = config[CONF_PASSWORD]
    resources = config[CONF_LIGHTS]
    api_version = config[CONF_API_VERSION]

    tv_coordinator = AmbiHue(hass, tvip, api_version, user, password)

    dev: list[SwitchEntity] = []
    for entry, data in resources.items():
        name = data[CONF_NAME]
        bulbip = data[CONF_ADDRESS]
        option = data[CONF_DISPLAY_OPTIONS]
        icon = data[CONF_ICON]
        try:
            bulb = Bulb(bulbip)
            bulb_properties = bulb.get_properties()
            if not bulb_properties:
                _LOGGER.error("Bulb is not available: %s", bulbip)
                continue
        except KeyError:
            _LOGGER.error("Bulb is not available: %s", bulbip)
            continue

        dev.append(
            AmbiHueSwitch(
                hass, tv_coordinator, name, bulbip, option, icon
            )
        )

    async_add_entities(dev, True)

class AmbiHueSwitch(SwitchEntity):

    def __init__(self, hass: HomeAssistant, tv_coordinator: AmbiHue, name, bulbip: string, option, icon) -> None:
        self._hass = hass
        self._name = name
        self._position = option
        self._icon = icon
        self._is_on = False
        self._connfail = 0
        self._available = False
        self._tv: AmbiHue = tv_coordinator
        self._bulbips = bulbip.split(', ')
        self._bulbs: list[Bulb] = []
        for address in self._bulbips:
            self._bulbs.append(Bulb(address))

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
        await self._tv.async_update()
        await self.async_turn_on_bulbs_and_music()
        await self.async_update()
        if self._is_on:
            self._follow = True
            self._tv.add_listener(self)
            _LOGGER.debug('AmbiYeelight turned on')
    
    async def async_turn_on_bulbs_and_music(self):
        tasks = []
        for bulb in self._bulbs:
            tasks.append(asyncio.create_task(self.async_turn_on_bulb_and_music(bulb)))
        while True:
            tasks = [t for t in tasks if not t.done()]
            if len(tasks) == 0:
                return
            await tasks[0]
    
    async def async_turn_on_bulb_and_music(self, bulb: Bulb):
        powerstate, musicmode = await self.async_getState(bulb)
        if not powerstate:
            bulb.turn_on()
        if not musicmode:
            bulb.start_music()

    async def async_turn_on_bulbs(self):
        tasks = []
        for bulb in self._bulbs:
            tasks.append(asyncio.create_task(self.async_turn_on_bulb(bulb)))
        while True:
            tasks = [t for t in tasks if not t.done()]
            if len(tasks) == 0:
                return
            await tasks[0]

    async def async_turn_on_bulb(self, bulb: Bulb):
        powerstate, musicmode = await self.async_getState(bulb)
        if not powerstate:
            bulb.turn_on()

    async def async_turn_off_bulbs(self):
        tasks = []
        for bulb in self._bulbs:
            tasks.append(asyncio.create_task(self.async_turn_off_bulb(bulb)))
        while True:
            tasks = [t for t in tasks if not t.done()]
            if len(tasks) == 0:
                return
            await tasks[0]
    
    async def async_turn_off_bulb(self, bulb: Bulb):
        powerstate, musicmode = await self.async_getState(bulb)
        if powerstate:
            bulb.turn_off()
    
    async def async_start_music_bulbs(self):
        tasks = []
        for bulb in self._bulbs:
            tasks.append(asyncio.create_task(self.async_start_music_bulb(bulb)))
        while True:
            tasks = [t for t in tasks if not t.done()]
            if len(tasks) == 0:
                return
            await tasks[0]
    
    async def async_start_music_bulb(self, bulb: Bulb):
        powerstate, musicmode = await self.async_getState(bulb)
        if not musicmode:
            bulb.start_music()

    async def async_stop_music_bulbs(self):
        tasks = []
        for bulb in self._bulbs:
            tasks.append(asyncio.create_task(self.async_stop_music_bulb(bulb)))
        while True:
            tasks = [t for t in tasks if not t.done()]
            if len(tasks) == 0:
                return
            await tasks[0]
    
    async def async_stop_music_bulb(self, bulb: Bulb):
        powerstate, musicmode = await self.async_getState(bulb)
        if musicmode:
            bulb.stop_music()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._tv.remove_listener(self)
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
            _LOGGER.error('The following error occured while getting the bulb state: ' + str(e))
        return power_on, musicmode

    async def async_update(self) -> None:
        for bulb in self._bulbs:
            powerstate, musicmode = await self.async_getState(bulb)
            if powerstate and musicmode:
                self._is_on = True
            else:
                self._is_on = False
                return

    async def async_is_available(self):
        try:
            properties = self._bulb.get_properties()
            if properties:
                self._available = True
            else:
                self._available = False
        except Exception as e:
            self._available = False
            _LOGGER.error('Failed to find bulb, trying again in 2s. Error: ' + str(e))
            await asyncio.sleep(2)
    
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
    
    async def async_set_bulb(self, r, g, b, bulb: Bulb, ambiSetting):
        try:
            if r == None and g == None and b == None: # incase of a failure somewhere
                r,g,b = DEFAULT_RGB_COLOR[0], DEFAULT_RGB_COLOR[1], DEFAULT_RGB_COLOR[2]
                bulb.set_brightness(1)
            
            if r == 0 and g == 0 and b == 0: # dim bulb in game mode
                if 'menuSetting' in ambiSetting and ambiSetting['menuSetting'] == "GAME":
                    r,g,b = DEFAULT_RGB_COLOR[0], DEFAULT_RGB_COLOR[1], DEFAULT_RGB_COLOR[2]
                    bulb.set_brightness(1)
            else:
                if ambiSetting['styleName'] == "FOLLOW_VIDEO":
                    transitions = [RGBTransition(r,g,b,duration=300,brightness=30)] # this transition can be customised (see: https://yeelight.readthedocs.io/en/latest/yeelight.html#yeelight.Flow)
                else:
                    transitions = [RGBTransition(r,g,b,duration=200,brightness=30)]
                flow = Flow(
                    count=1,
                    action=Flow.actions.stay,
                    transitions=transitions)
                bulb.start_flow(flow)
                return True
        except Exception as e:
            _LOGGER.error('Failed to set the bulb color values with error:' + str(e))
            return False

    async def async_update_bulbs(self):
        r, g, b = await self.async_get_rgb(self._tv._layer, self._position)
        if r == None and g == None and b == None:
            _LOGGER.error('RGB values are None.')
        ambiSetting = self._tv._api.ambilight_current_configuration
        tasks = []
        for bulb in self._bulbs:
            tasks.append(asyncio.create_task(self.async_set_bulb(r, g, b, bulb, ambiSetting)))
        while True:
            tasks = [t for t in tasks if not t.done()]
            if len(tasks) == 0:
                return
            await tasks[0]


class AmbiHue:
    """The class for handling the data retrieval."""
    
    def __init__(self, hass: HomeAssistant, tvip, api_version, user, password) -> None:
        self._hass = hass
        self._tvip = tvip
        self._user = user
        self._password = password

        self._follow = False
        self._on_update: list[Callable] = []
        self._layer = None
        self._api = PhilipsTV(self._tvip, api_version, username=self._user, password=self._password)

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
        _LOGGER.info('Follow ambilight')
        counter = 0
        while self._follow == True and counter < (duration / sleep): # second loop for updating the bulb
            try:
                await self.async_get_ambilayer()
                if self._layer is None:
                    _LOGGER.error('self._layer is None.')

                await self.notify_listeners()

                counter += 1
                await asyncio.sleep(sleep)
            except Exception as e:
                _LOGGER.error('Failed to transfer color values with error (from second loop):' + str(e))
                self._follow = False
        return counter

    async def async_follow_tv(self, sleep):
        _LOGGER.debug('Starting async_follow_tv')
        while self._follow == True: # main loop for updating the bulb
            try:
                await self._api.getAmbilightCurrentConfiguration()
                if self._api.ambilight_current_configuration is None:
                    _LOGGER.error('AmbiSetting is None. Trying again in 5 seconds')
                    await asyncio.sleep(5)
                    continue
                
                await self._api.getAmbilightPower()
                if self._api.ambilight_power == 'On':
                    counter = await self.async_follow_ambilight(sleep, 10)
                else:
                    _LOGGER.info('The ambilight seems to be turned OFF, checking again in 5 seconds.')
                    await asyncio.sleep(5)

            except Exception as e:
                _LOGGER.error('Failed to transfer color values with error (from main loop):' + str(e))
                self._follow = False
                return False
        return True
    
    def add_listener(self, listener):
        _LOGGER.info('Added listener')
        self._on_update.append(listener)
        if len(self._on_update) > 0:
            self.start_following()

    def remove_listener(self, listener):
        _LOGGER.info('Removed listener')
        self._on_update.remove(listener)
        if len(self._on_update) == 0:
            self.stop_following()

    def remove_listeners(self):
        _LOGGER.info('Removed listeners')
        self._on_update.clear()
    
    async def notify_listeners(self):
        tasks = []
        for listener in self._on_update:
            tasks.append(asyncio.create_task(listener.async_update_bulbs()))
        while True:
            tasks = [t for t in tasks if not t.done()]
            if len(tasks) == 0:
                return
            await tasks[0]

    def start_following(self):
        _LOGGER.info('Start following')
        self._follow = True
        future = asyncio.ensure_future(self.async_follow_tv(0.1))

    def stop_following(self):
        _LOGGER.info('Stop following')
        self._follow = False
