"""Supported transports and device models, independent of Home Assistant."""

AIRMINI = "airmini"
AIRSENSE11 = "airsense11"
AIR11_SERVICE = "0000fd56-0000-1000-8000-00805f9b34fb"
MODELS = {AIRMINI: "AirMini", AIRSENSE11: "AirSense 11"}


def detect(properties):
    """Identify the Air11 BLE service before applying Classic name hints."""
    uuids = {value.lower() for value in properties.get("UUIDs", [])}
    name = str(properties.get("Name") or properties.get("Alias") or "").lower()
    if AIR11_SERVICE in uuids or ("Class" not in properties and name.startswith("resmed ")):
        return AIRSENSE11
    from .bluez.survey import looks_like_airmini
    return AIRMINI if looks_like_airmini(properties) else None
