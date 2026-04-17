"""Constants for the Converging Systems e-Node CS-Bus integration."""

DOMAIN = "csbus_enode"

# Config entry keys
CONF_HOST = "host"
CONF_PORT = "port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DEFAULT_TRANSITION = "default_transition"

# Defaults
DEFAULT_PORT = 23
DEFAULT_USERNAME = "Telnet 1"
DEFAULT_PASSWORD = "Password 1"
DEFAULT_SCAN_INTERVAL = 30   # seconds
DEFAULT_TRANSITION = 1       # seconds

# Device class values in FORM response
DEVICE_CLASS_LIGHT = "LIGHT"
DEVICE_CLASS_MOTOR = "MOTOR"
DEVICE_CLASS_KEYPAD = "KEYPAD"

# Color space values
COLOR_SPACE_HSV = "HSV"
COLOR_SPACE_MONO = "MONO"

# ZGN address defaults
DEFAULT_LIGHTING_ZGN = "2.1.0"
DEFAULT_MOTOR_ZGN = "1.1.0"

# CS-Bus command device types
CS_DEVICE_LED = "LED"
CS_DEVICE_MOTOR = "MOTOR"

# Motor status values returned by MOTOR.STATUS=?
MOTOR_STATUS_OPEN = "OPEN"
MOTOR_STATUS_CLOSE = "CLOSE"
MOTOR_STATUS_STOP = "STOP"
MOTOR_STATUS_HOME = "HOME"
MOTOR_STATUS_EXTENDING = "EXTENDING"
MOTOR_STATUS_RETRACTING = "RETRACTING"

# HA platform names
PLATFORM_LIGHT = "light"
PLATFORM_COVER = "cover"

# Internal data keys
DATA_CLIENT = "client"
DATA_COORDINATOR = "coordinator"
DATA_DEVICES = "devices"
DATA_LIGHT_ENTITIES = "light_entities"
DATA_COVER_ENTITIES = "cover_entities"

# Service names
SERVICE_RECALL_PRESET = "csbus_recall_preset"
SERVICE_STORE_PRESET = "csbus_store_preset"
SERVICE_SET_CIRCADIAN = "csbus_set_circadian"
SERVICE_RESUME_CIRCADIAN = "csbus_resume_circadian"
SERVICE_SET_DISSOLVE = "csbus_set_dissolve"
