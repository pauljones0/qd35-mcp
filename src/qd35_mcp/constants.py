"""QD35 UUIDs and protocol constants recovered from Edifier Connect."""

SEARCH_UUID = "0000a400-0000-1000-8000-00805f9b34fb"
SERVICE_UUID = "4809a401-1a48-11e9-ab14-d663bd873d93"
READ_UUID = "48090001-1a48-11e9-ab14-d663bd873d93"
WRITE_UUID = "48090002-1a48-11e9-ab14-d663bd873d93"
EDIFIER_COMPANY_ID = 0x07E0

QUERY_CAPABILITIES = 0xD8
QUERY_FIRMWARE = 0xC6
QUERY_ADDRESS = 0xC8
QUERY_NAME = 0xC9
SET_NAME = 0xCA
QUERY_INPUT = 0x61
SET_INPUT = 0x62
QUERY_VOLUME = 0x66
SET_VOLUME = 0x67
QUERY_EQ_PRESET = 0xD5
SET_EQ_PRESET = 0xC4
QUERY_EQ = 0x43
SET_EQ = 0x44
RESET_EQ = 0x45
QUERY_LIGHT = 0x6A
SET_LIGHT = 0x6B
RESET_LIGHT = 0x72
QUERY_LIGHT_TIMER = 0x69
SET_LIGHT_TIMER = 0x70
QUERY_AUDIO_STATUS = 0x68
QUERY_CUSTOM_LIGHT = 0x78
START_CUSTOM_LIGHT_TRANSFER = 0x79
PROCESS_CUSTOM_LIGHT_TRANSFER = 0x7A
FINISH_CUSTOM_LIGHT_TRANSFER = 0x7B
QUERY_LDAC = 0x48
SET_LDAC = 0x49
MEDIA_INFO = 0x50
MEDIA_TITLE_EVENT = 0x01
MEDIA_ARTIST_EVENT = 0x02
MEDIA_CONTROL = 0xC2
QUERY_PLAYBACK = 0xC3
SYNC_CLOCK = 0x77
POWER_OFF = 0xCE
DISCONNECT_AUDIO = 0xCD
FACTORY_RESET = 0x07
OTA_READY = 0x10
OTA_TRANSFER = 0x11

MUTATING_OPCODES = frozenset(
    {
        SET_NAME,
        SET_INPUT,
        SET_VOLUME,
        SET_EQ_PRESET,
        SET_EQ,
        RESET_EQ,
        SET_LIGHT,
        RESET_LIGHT,
        SET_LIGHT_TIMER,
        START_CUSTOM_LIGHT_TRANSFER,
        PROCESS_CUSTOM_LIGHT_TRANSFER,
        FINISH_CUSTOM_LIGHT_TRANSFER,
        SET_LDAC,
        MEDIA_CONTROL,
        SYNC_CLOCK,
        POWER_OFF,
        DISCONNECT_AUDIO,
        FACTORY_RESET,
        OTA_READY,
        OTA_TRANSFER,
    }
)

INPUTS = {"bluetooth": 1, "aux": 2, "usb": 3}
INPUT_NAMES = {value: key for key, value in INPUTS.items()}
EQ_PRESETS = {"music": 0, "game": 1, "movie": 2, "diy": 3}
EQ_PRESET_NAMES = {value: key for key, value in EQ_PRESETS.items()}
MEDIA_ACTIONS = {"play": 0, "pause": 1, "next": 4, "previous": 5}
LDAC_MODES = {"off": 0, "48khz": 1, "96khz": 2}
LDAC_MODE_NAMES = {value: key for key, value in LDAC_MODES.items()}

# Live-confirmed on QD35 firmware 1.3.0 for lighting group 1.
LIGHT_EFFECTS = {
    "ambient": 1,
    "magic_tides": 2,
    "shooting_star": 3,
    "breathing": 4,
    "tempo_emo": 5,
    "static": 6,
    "extended": 7,
}
LIGHT_EFFECT_NAMES = {value: key for key, value in LIGHT_EFFECTS.items()}
LIGHT_RGB_EFFECT_IDS = frozenset({3, 4, 5, 6})
LIGHT_COLOR_MODES = {
    1: {"light": 1, "deep": 2},
    2: {"light": 1, "deep": 2},
    7: {"off": 0, "on": 1},
}
