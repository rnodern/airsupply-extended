"""Independent BLE sessions using AirSupply's existing storage and publisher."""

import asyncio
import copy
import datetime as dt
import logging
import time

from bleak import BleakClient, BleakScanner

from .. import store
from .history import nightly_summaries
from .protocol import AirSenseClient, AuthError, Credentials

log = logging.getLogger("airsupply")
THERAPY_STATES = {"therapy", "running"}
KEEPALIVE_SECONDS = 60
RECONNECT_SECONDS = 30
SUMMARY_RETRY_SECONDS = 120
SUMMARY_AFTER_STOP_SECONDS = 60


class Driver:
    """One machine owns one task, so GATT requests never interleave."""

    def __init__(self, address, on_update, history=None, read_every=900):
        self.address = address
        self.on_update = on_update
        self.history = history
        self.read_every = read_every
        self.connected = False
        self.auth_required = False
        self.pairing_ready = False
        self.problem = None
        self.model = "AirSense 11"
        self.results = {}
        self.at_iso = None
        self.at = None
        self.status = None
        self._task = None
        self._wake = asyncio.Event()
        self._read_requested = False
        self._summary_at = float("inf")
        self._summary_retry_at = 0
        self._ble = None
        self._client = None
        self._pin = None
        self._paired = None

    def start(self):
        if not self.auth_required and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run())

    async def prepare_pairing(self):
        await self.stop()
        self.auth_required = False
        self.problem = None
        ready = asyncio.get_running_loop().create_future()
        self._pin = asyncio.get_running_loop().create_future()
        self._paired = asyncio.get_running_loop().create_future()
        # A pairing window can expire before a user submits its code.
        self._paired.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        self._task = asyncio.create_task(self._run(ready, self._paired))
        try:
            await asyncio.wait_for(ready, 45)
        except BaseException:
            await self.stop()
            raise

    async def pair(self, pin):
        if not self.pairing_ready or self._pin is None or self._pin.done():
            raise RuntimeError("Press Pair first to display a fresh code on the AirSense.")
        self._pin.set_result(pin)
        try:
            await asyncio.wait_for(asyncio.shield(self._paired), 60)
        except BaseException:
            await self.stop()
            raise

    def request_read(self):
        self._read_requested = True
        self._wake.set()

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self, prepared=None, paired=None):
        try:
            while True:
                try:
                    device = await BleakScanner.find_device_by_address(self.address, timeout=10)
                    if device is None:
                        raise RuntimeError("Machine is asleep or out of Bluetooth range.")
                    self._ble = BleakClient(device, disconnected_callback=lambda _: self._wake.set(), timeout=20)
                    await self._ble.connect()
                    self._client = AirSenseClient(self._ble, self._on_event)
                    await self._client.start()
                    if prepared is not None:
                        self.pairing_ready = True
                        self.on_update()
                        prepared.set_result(None)
                        pin = await asyncio.wait_for(self._pin, 90)
                        self.pairing_ready = False
                        credentials = await self._client.pair(pin)
                        store.remember_credentials(self.address, credentials.as_dict())
                        pin = None
                    else:
                        saved = store.credentials(self.address)
                        if not saved:
                            raise AuthError("Pair the machine using its screen code.")
                        await self._client.open_session(Credentials.from_dict(saved))
                    self.connected = True
                    self.auth_required = False
                    self.problem = None
                    self.on_update()
                    if paired is not None and not paired.done():
                        paired.set_result(None)
                    prepared = paired = None
                    log.info("AirSense Bluetooth session opened for %s.", self.address)
                    await self._client.subscribe(("FGState",))
                    await self._monitor()
                except AuthError:
                    self.auth_required = True
                    self.problem = "Pair again using the code on the machine. Its Bluetooth pairing may have changed."
                    for future in (prepared, paired):
                        if future is not None and not future.done():
                            future.set_exception(RuntimeError(self.problem))
                    return
                except Exception as err:  # noqa: BLE001 -- recover a lost BLE session
                    self.problem = "AirSense is asleep, out of range, or its Bluetooth session ended. Retrying."
                    log.warning("AirSense Bluetooth session deferred: %s.", type(err).__name__)
                    if prepared is not None:
                        for future in (prepared, paired):
                            if future is not None and not future.done():
                                future.set_exception(RuntimeError(self.problem))
                        return
                finally:
                    await self._cleanup()
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), RECONNECT_SECONDS)
                except TimeoutError:
                    pass
        finally:
            for future in (prepared, paired, self._pin):
                if future is not None and not future.done():
                    future.cancel()

    async def _cleanup(self):
        client, ble = self._client, self._ble
        self._client = self._ble = None
        self.connected = False
        self.pairing_ready = False
        self.on_update()
        if client is not None:
            try:
                await asyncio.wait_for(client.stop(), 5)
            except Exception:  # noqa: BLE001 -- the link may have ended already
                pass
        if ble is not None:
            try:
                await asyncio.wait_for(ble.disconnect(), 5)
            except Exception:  # noqa: BLE001 -- the link may have ended already
                pass

    async def _monitor(self):
        next_read = 0
        self._summary_at = time.monotonic()
        while self._ble.is_connected:
            self._wake.clear()
            now = time.monotonic()
            if self._read_requested or now >= next_read:
                self._read_requested = False
                await self._read()
                next_read = time.monotonic() + self.read_every
            else:
                response = await self._client.get("FGState")
                self._on_event("FGState", response.get("FGState"), {})
            now = time.monotonic()
            if self.history is not None and now >= self._summary_retry_at:
                if now >= self._summary_at or self.history.is_due(self.address):
                    await self._sync_history()
            if self._wake.is_set():
                continue
            delay = min(KEEPALIVE_SECONDS, max(0, next_read - time.monotonic()))
            if self.history is not None:
                deadline = max(self._summary_at, self._summary_retry_at)
                delay = min(delay, max(0, deadline - time.monotonic()))
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except TimeoutError:
                pass

    async def _read(self):
        results = {}
        for title, operation in (
            ("GetVersion", self._version),
            ("Settings", self._settings),
            ("MachineMetrics", lambda: self._client.get("MachineMetrics")),
        ):
            try:
                value = await operation()
                results[title] = {"ok": True, "value": value}
            except Exception as err:  # noqa: BLE001 -- publish the other successful reads
                results[title] = {"ok": False, "error": type(err).__name__}
        self.results = results
        self.at_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.on_update()
        log.info("AirSense standard readings refreshed for %s.", self.address)

    async def _version(self):
        version = await self._client.call("GetVersion", None, "2.0") or {}
        identity = (await self._client.get("IdentificationProfiles")).get("IdentificationProfiles", {})
        self.model = identity.get("Product", {}).get("ProductName") or self.model
        return {**version, "FlowGenerator": {"IdentificationProfiles": identity}}

    async def _settings(self):
        profiles = (await self._client.get("SettingProfiles")).get("SettingProfiles", {})
        response = await self._client.get("FGState")
        self._on_event("FGState", response.get("FGState"), {})
        return {**profiles, "FGState": self.status}

    def _on_event(self, data_id, value, _event):
        if data_id != "FGState" or not isinstance(value, str) or value == self.status:
            return
        previous, self.status = self.status, value
        if previous and previous.casefold() in THERAPY_STATES and value.casefold() == "standby":
            self._summary_at = time.monotonic() + SUMMARY_AFTER_STOP_SECONDS
            self._summary_retry_at = 0
            log.info("AirSense entered standby; history scheduled after finalisation.")
        self.results = copy.deepcopy(self.results)
        settings = self.results.setdefault("Settings", {"ok": True, "value": {}})
        if not settings.get("ok"):
            settings = self.results["Settings"] = {"ok": True, "value": {}}
        settings["value"]["FGState"] = value
        self.on_update()
        self._wake.set()

    async def _sync_history(self):
        requested_at = self._summary_at
        try:
            since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=16)
            payload = await asyncio.wait_for(self._client.spool("Summary", since), self.history.timeout)
            nights = nightly_summaries(payload, self.history.timezone)
            self.history.store.save(self.address, nights)
        except Exception as err:  # noqa: BLE001 -- preserve the prior complete history
            self._summary_retry_at = time.monotonic() + SUMMARY_RETRY_SECONDS
            log.warning("AirSense historical sync deferred: %s.", type(err).__name__)
        else:
            if self._summary_at == requested_at:
                self._summary_at = float("inf")
            self._summary_retry_at = 0
            self.on_update()
            log.info("AirSense historical sync completed: %d nights.", len(nights))
