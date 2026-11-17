"""The one place state lives, and the operations the page can start.

Every operation runs as a background task; the page polls `state()` and
renders whatever is there. One operation at a time, because they all want
the same serial channel, and a person watching a page is not going to click
two things at once on purpose.

Read-only. Nothing here can reach the machine's Set path; see
airmini/session.py for where that line is drawn.
"""

import asyncio
import collections
import datetime
import json
import logging
import os
import re
import time

from dbus_next import DBusError

from . import entities, hass, mqtt, store
from .bluez import adapter, agent, spp
from .machine import AIRMINI, AIRSENSE11

log = logging.getLogger("airsupply")

SCAN_SECONDS = 20
HANDSHAKE_TIMEOUT = 60
READ_TIMEOUT = 30
LOG_LINES = 300

# The bond, and why it takes this much care. A BR/EDR page reaches a machine
# that is listening for one, using inquiry data that has not gone stale, from
# a controller that is not inquiring itself. Miss any of the three and BlueZ
# answers ConnectionAttemptFailed: Page Timeout -- which says nothing about
# which one it was, so the sequence covers all three before it gives up.
# How often every paired machine is read. The AirMini is a nightstand device
# whose numbers move once a night, so this is about keeping Home Assistant
# current rather than about resolution -- and every read is a window in which
# ResMed's own app cannot have the machine.
READ_EVERY = int(os.environ.get("AIRSUPPLY_READ_EVERY") or 900)

REFRESH_SCAN_SECONDS = 8

RADIO_SETTLE = 2.0
PAIR_ATTEMPTS = 3
PAIR_RETRY_PAUSE = 4.0

# A paired device may not answer a Classic Bluetooth page immediately after
# leaving the phone app or a previous session. Reconnect using a bounded
# discovery / settle / retry sequence rather than waiting 15 minutes.
# A genuine second client can still prevent a connection: never steal it.
RECONNECT_ATTEMPTS = max(1, min(5, int(os.environ.get("AIRSUPPLY_RECONNECT_ATTEMPTS") or 3)))
RECONNECT_SCAN_SECONDS = max(2, min(20, int(os.environ.get("AIRSUPPLY_RECONNECT_SCAN_SECONDS") or 8)))
RECONNECT_RETRY_PAUSE = max(2.0, min(60.0, float(os.environ.get("AIRSUPPLY_RECONNECT_RETRY_PAUSE") or 8)))

PAGE_TIMEOUT_ADVICE = "No answer. Put the machine in pairing mode and try again."


# libairmini answers a completed pairing with
#     {"masterPairKey":"<64 hex>","sessionKey":"<64 hex>"}
# built by snprintf into a `char res[160]` in airmini.c -- 5 bytes short of
# the 165 that string needs. So what arrives is truncated at 159 characters,
# with the closing quote and brace cut off, which is why it will not parse as
# JSON and turns up here as a bare string.
#
# The key itself is unhurt: it ends at character 82, well inside what
# survives. Only the tail of the session key is lost, and nothing here wants
# that -- libairmini has already opened the session with it by this point.
#
# So read the key out of the text rather than requiring the JSON to be whole.
# Reported upstream; this keeps working either way, since a fixed libairmini
# produces a string this still matches.
_PAIR_KEY = re.compile(r"[0-9a-fA-F]{32,128}")
_PAIR_KEY_FIELD = re.compile(r'"masterPairKey"\s*:\s*"([0-9a-fA-F]{32,128})"')

class Held:
    """A machine we are connected to, and staying connected to."""

    def __init__(self, address, path, fd, session):
        self.address = address
        self.path = path
        self.fd = fd
        self.session = session
        self.since = time.time()

        # One request at a time down one channel. Two reads of the same
        # machine at once would interleave on the wire.
        self.lock = asyncio.Lock()

    @property
    def alive(self):
        return self.session.alive


class Busy(Exception):

    pass


class _PageLog(logging.Handler):
    """Mirror the add-on log into a ring buffer the page can show."""

    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        try:
            self.sink.append({
                "t": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname.lower(),
                "msg": self.format(record),
            })
        except Exception:  # noqa: BLE001 -- a logging handler must not raise
            pass


class Controller:
    def __init__(self, bus, version):
        self.bus = bus
        self.version = version
        self.adapter = None
        self.devices = []
        self.selected = store.selected()
        self.scanning = False
        self.busy = None
        self.error = None
        # One reading per machine, keyed by address. A reading belongs to the
        # machine it came from rather than to the add-on, which is what makes
        # a second machine possible at all.
        self.readings = {}
        self.session_state = None

        # Machines we are connected to, and machines somebody has let go of
        # on purpose. A deliberate disconnect stays disconnected: the point of
        # it is to give the machine to the phone app, and a schedule that took
        # it straight back would make the button a lie.
        self.sessions = {}
        self.air11 = {}
        self.released = set()

        self.publisher = mqtt.Publisher(version)
        # Opt in only after the device clock offset has been measured.
        self.history = None
        self._therapy_history_pending = set()
        if os.environ.get("AIRSUPPLY_HISTORY_ENABLED", "0") == "1":
            from .history_sync import HistoryManager
            self.history = HistoryManager()
        self.people = []
        self.people_problem = None
        self.log = collections.deque(maxlen=LOG_LINES)
        self.agent = None
        self.agent_ok = False
        self.profile = None
        self._prompt = None
        self._last_refresh = 0.0
        self._task = None
        self._scan_task = None
        self._scan_stop = None
        self._schedule_task = None

    # --- lifecycle ---------------------------------------------------------

    async def start(self):
        store.upgrade()
        handler = _PageLog(self.log)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(handler)

        self.agent = agent.PairingAgent(self._on_prompt)
        try:
            await agent.register(self.bus, self.agent)
            self.agent_ok = True
        except Exception as err:  # noqa: BLE001
            log.error("Could not register a pairing agent: %s", err)
            log.error("The Bluetooth bond step will not work from this page.")

        self.profile = await spp.register(self.bus, on_incoming=self._accept_incoming)
        await self.refresh(force=True)

        self.publisher.start()
        await self.refresh_people()
        self._schedule_task = asyncio.create_task(self._schedule())

    async def stop(self):
        """Gracefully release only sessions that this process actually owns.

        Docker sends SIGTERM when an image is replaced. Without an explicit
        shutdown path, the process exits before telling BlueZ to disconnect
        its held SPP profile. The next process may then see an 'already
        connected' link without owning a usable RFCOMM descriptor.
        """
        pending = [
            task for task in (
                self._schedule_task, self._scan_task, self._task,
            ) if task is not None and not task.done()
        ]
        if self._scan_stop is not None:
            self._scan_stop.set()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        for driver in list(self.air11.values()):
            await driver.stop()
        self.air11.clear()

        for address in list(self.sessions):
            try:
                await asyncio.wait_for(self._release(address), timeout=3)
            except Exception as err:  # noqa: BLE001 -- shutdown is best effort
                log.warning("Could not fully release %s on shutdown: %s.",
                            address, type(err).__name__)

        if self.profile is not None:
            self.profile.close()
        await self.publisher.stop()
        log.info("Stopped AirSupply; released owned Bluetooth sessions.")

    async def refresh_people(self):
        """Who Home Assistant knows about, so a machine can belong to one."""
        if not hass.configured():
            self.people_problem = "the add-on has not been granted homeassistant_api"
            return
        try:
            self.people = await hass.people()
            self.people_problem = None
            log.info("Home Assistant knows %d people.", len(self.people))
        except hass.Unavailable as err:
            self.people_problem = str(err)
            log.warning("Could not ask Home Assistant who lives here: %s", err)

    def _accept_incoming(self, device_path):
        """Vet an inbound SPP connection and request a normal read.

        BlueZ authenticates the link, but a trusted device alone cannot
        authorise AirSupply to access stored therapy data. The peer must be
        a machine registered in our local store and have a saved application
        pairing key. Deliberately released devices never auto-reconnect.
        """
        match = re.search(r"/dev_([0-9A-Fa-f_]{17})$", device_path)
        if match is None:
            return False
        address = match.group(1).replace("_", ":").upper()
        if address in self.released or address in self.sessions:
            return False
        saved = store.machines().get(address)
        if not saved or not saved.get("paired"):
            return False
        if not store.master_pair_key(address):
            return False

        # The Profile1 handler will cache the fd after this returns; defer
        # consumption until the callback has completed.
        loop = asyncio.get_running_loop()
        loop.call_soon(self._schedule_incoming, address)
        return True

    def _schedule_incoming(self, address):
        if address in self.released or address in self.sessions:
            return
        if self.busy:
            # The in-flight outbound read can consume the incoming fd from
            # its existing Profile1 expectation; otherwise the schedule
            # will pick it up after the current operation.
            log.info("Incoming AirMini connection queued during %s.", self.busy)
            return
        try:
            self._spawn("read", lambda: self._read(address))
        except Busy:
            pass

    def _paired(self):
        """Machines there is a key for, so they can be read without anybody."""
        return [a for a, m in store.machines().items() if m["paired"]]

    async def _schedule(self):
        """Read every paired machine, in turn, for as long as we are running.

        In turn, because the machine accepts one connection at a time and
        while we hold it ResMed's own app cannot. A machine with no key yet
        is left alone rather than woken up to ask it for one.

        The first pass is immediate: after a restart the numbers in Home
        Assistant are as old as the add-on was down for.
        """
        while True:
            for address in self._paired():
                if self.busy or address in self.released:
                    continue
                if store.driver(address) == AIRSENSE11:
                    self._air11_driver(address).start()
                    continue
                try:
                    await self._spawn("read", lambda a=address: self._read(a))
                except Busy:
                    break
            await asyncio.sleep(READ_EVERY)

    # --- state -------------------------------------------------------------

    async def refresh(self, force=False):
        if not force and time.monotonic() - self._last_refresh < 1.0:
            return
        try:
            self.adapter, self.devices = await adapter.snapshot(self.bus)
        except Exception as err:  # noqa: BLE001
            log.error("Could not list Bluetooth devices: %s", err)
        self._last_refresh = time.monotonic()

    def _selected_device(self):
        for d in self.devices:
            if d["address"] == self.selected:
                return d
        return None

    async def state(self):
        await self.refresh()
        sel = self._selected_device()
        return {
            "version": self.version,
            "busy": self.busy,
            "scanning": self.scanning,
            "agent_ok": self.agent_ok,
            "adapter": self.adapter,
            "devices": self.devices,
            "selected": self.selected,
            "selected_device": sel,
            "bonded": bool(sel and sel["paired"]),
            "has_key": bool(self.selected and store.machines().get(self.selected, {}).get("paired")),
            "prompt": self._prompt.to_json() if self._prompt else None,
            "machines": self.machines(),
            "people": [person.to_json() for person in self.people],
            "people_problem": self.people_problem,
            "reading": self.readings.get(self.selected),
            "publishing": {
                "configured": mqtt.configured(),
                "connected": self.publisher.connected,
                "problem": self.publisher.problem,
            },
            "session_state": self.session_state,
            "error": self.error,
            "log": list(self.log),
        }

    def machines(self):
        """Every machine we have been told about, with whatever is known.

        The store says which machines exist and whose they are; BlueZ says
        whether one is in range and bonded right now. Neither is the whole
        picture on its own -- a machine that is paired and out of range is
        still a machine somebody owns.
        """
        people = {person.id: person for person in self.people}
        out = []
        for address, record in store.machines().items():
            device = next((d for d in self.devices if d["address"] == address), None) or {}
            reading = self.readings.get(address)
            person = people.get(record["person_id"])
            ble = self.air11.get(address)
            driver = record.get("driver", AIRMINI)
            out.append({
                "address": address,
                "name": device.get("name") or record["name"] or "",
                "signal": device.get("rssi"),
                "classic": bool(device.get("classic", driver == AIRMINI)),
                "serial": bool(device.get("spp")),
                "bonded": bool(device.get("paired")),
                "paired": bool(record["paired"] and not (ble and ble.auth_required)),
                "connected": address in self.sessions or bool(ble and ble.connected),
                "driver": driver,
                "released": address in self.released,
                "person": person.to_json() if person else None,
                "person_id": record["person_id"],
                "missing_person": bool(record["person_id"] and not person),
                "last_read": reading["at"] if reading else None,
            })
        return out

    def _on_prompt(self, prompt):
        self._prompt = prompt

    # --- operations --------------------------------------------------------

    def _spawn(self, name, work):
        if self.busy:
            raise Busy(f"{self.busy} is still running")
        self.busy = name
        self.error = None

        async def runner():
            try:
                await work()
            except Exception as err:  # noqa: BLE001
                self.error = str(err) or type(err).__name__
                log.error("%s failed: %s", name, self.error)
                log.debug("traceback", exc_info=True)
            finally:
                self.busy = None
                self._last_refresh = 0.0

        self._task = asyncio.create_task(runner())
        return self._task

    def scan(self, seconds=SCAN_SECONDS):
        if self.scanning:
            return
        if not self.adapter:
            raise RuntimeError("No Bluetooth adapter.")
        self.scanning = True
        self._scan_stop = asyncio.Event()
        self._scan_task = asyncio.create_task(self._scan(seconds))

    async def _scan(self, seconds):
        path = self.adapter["path"]
        stop = self._scan_stop
        try:
            await adapter.start_discovery(self.bus, path)
            log.info("Scanning %ds.", seconds)
            try:
                # Ends early if the bond step asks for the radio back.
                await asyncio.wait_for(stop.wait(), seconds)
            except asyncio.TimeoutError:
                pass
        except Exception as err:  # noqa: BLE001
            self.error = f"scan failed: {err}"
            log.error("Scan failed: %s", err)
        finally:
            try:
                await adapter.stop_discovery(self.bus, path)
            except Exception:  # noqa: BLE001 -- ours may already be over
                pass
            self.scanning = False
            self._last_refresh = 0.0
            log.info("Scan finished.")

    async def _scan_finished(self):
        """Wait for the scan task, if any, to have stopped discovery."""
        task = self._scan_task
        if task is not None:
            await task

    async def stop_scan(self):
        """Cut a running scan short and wait for the inquiry to be over."""
        if self._scan_stop is not None:
            self._scan_stop.set()
        await self._scan_finished()

    def select(self, address):
        """Add a machine, or move the page's attention to one already added.

        A machine BlueZ cannot see is still selectable once it is in the
        store: it is out of range, not gone, and its readings are still
        worth looking at.
        """
        address = (address or "").upper()
        device = next((d for d in self.devices if d["address"] == address), None)
        if device is None and address not in store.machines():
            raise ValueError(f"Unknown machine: {address}.")
        store.add(address, (device or {}).get("name", ""), driver=(device or {}).get("driver"))
        store.select(address)
        self.selected = address
        log.info("Looking at %s.", address)

    def assign(self, address, person_id):
        """Say which of Home Assistant's people a machine belongs to."""
        address = (address or self.selected or "").upper()
        if not address:
            raise RuntimeError("No machine chosen.")
        person_id = person_id or None
        if person_id and not any(person.id == person_id for person in self.people):
            raise ValueError("No such person in Home Assistant.")
        store.assign(address, person_id)
        self._republish(address)

    def _publish(self, address):
        record = store.machines().get(address) or {}
        person = next((p for p in self.people if p.id == record.get("person_id")), None)
        reading = self.readings.get(address) or {}
        nights = self.history.recent(address) if self.history is not None else None
        ble = self.air11.get(address)
        self.publisher.publish(
            address, record.get("name") or "", person,
            reading.get("results"), reading.get("at_iso"),
            address in self.sessions or bool(ble and ble.connected), nights=nights,
            model=ble.model if ble else record.get("model", "AirMini"),
        )

    def _air11_driver(self, address):
        driver = self.air11.get(address)
        if driver is None:
            from .airsense.driver import Driver
            driver = Driver(
                address, lambda: self._air11_update(address),
                history=self.history, read_every=READ_EVERY,
            )
            self.air11[address] = driver
        return driver

    def _air11_update(self, address):
        driver = self.air11.get(address)
        if driver is None:
            return
        self.readings[address] = {
            "results": driver.results, "at": driver.at, "at_iso": driver.at_iso,
        }
        if driver.auth_required:
            self.error = driver.problem
        self._publish(address)

    def _republish(self, address):

        """Say a machine again, after something about it has changed."""
        if address in self.readings:
            self._publish(address)

    def bond(self):
        self._spawn("bond", self._bond)

    async def _bond(self):
        dev = self._selected_device()
        if dev is None:
            raise RuntimeError("No machine selected.")
        if dev["paired"]:
            log.info("%s is already bonded.", dev["address"])
            return
        if not self.agent_ok:
            raise RuntimeError("No pairing agent.")

        dev = await self._ready_to_page(dev)
        log.info("Bonding with %s.", dev["address"])
        for attempt in range(1, PAIR_ATTEMPTS + 1):
            try:
                await adapter.pair(self.bus, dev["path"])
                break
            except DBusError as err:
                if not adapter.is_page_timeout(err):
                    raise
                log.warning(
                    "Attempt %d/%d: no answer (%s).",
                    attempt, PAIR_ATTEMPTS, err.text or err.type,
                )
                if attempt == PAIR_ATTEMPTS:
                    raise RuntimeError(PAGE_TIMEOUT_ADVICE) from err
                await asyncio.sleep(PAIR_RETRY_PAUSE)
            finally:
                # A failed attempt leaves a stale question on the page.
                self.agent.clear()
        log.info("Bonded with %s.", dev["address"])

    async def _ready_to_page(self, dev):
        """Give the radio its best chance of reaching the machine.

        Paging a Classic device is not connecting to a BLE one. The controller
        pages using the clock offset and page-scan mode it learned from an
        inquiry, and it will not page while it is still inquiring -- so a bond
        started straight off the scan's own device list, which is the obvious
        thing to do, pages into a running inquiry and gets Page Timeout back.
        That is the failure this exists to prevent.

        Home Assistant's passive BLE scan keeps the adapter's `Discovering`
        true whatever we do, so there is nothing to wait for there: it is LE,
        it does not inquire, and it does not block a page. Ours does.
        """
        if self.scanning:
            log.info("Stopping the scan to page.")
            await self.stop_scan()
            dev = await self._reselect(dev)

        if dev["rssi"] is None:
            log.info(
                "Not seen recently; inquiring %ds first.", REFRESH_SCAN_SECONDS,
            )
            self.scan(REFRESH_SCAN_SECONDS)
            await self._scan_finished()
            dev = await self._reselect(dev)
            if dev["rssi"] is None:
                log.info("Still not seen; paging anyway.")

        # The controller finishes the inquiry window it is in before it pages.
        await asyncio.sleep(RADIO_SETTLE)
        return dev

    async def _reselect(self, dev):
        """Re-read the device after an inquiry; BlueZ may have replaced it."""
        await self.refresh(force=True)
        fresh = self._selected_device()
        if fresh is None:
            raise RuntimeError(
                "Machine gone. Scan again."
            )
        return fresh

    def answer(self, value=None, accept=True):
        if self._prompt is None:
            raise RuntimeError("nothing is waiting for an answer")
        self._prompt.answer(value, accept)

    def pair(self, pin):
        pin = (pin or "").strip()
        if not pin:
            raise ValueError("The PIN on the machine's screen is needed.")
        self._spawn("pair", lambda: self._first_pair(pin))

    async def _first_pair(self, pin):
        address = self.selected
        if address and store.driver(address) == AIRSENSE11:
            await self._release(address)
            self.released.discard(address)
            await self._air11_driver(address).pair(pin)
            return
        held = await self._held(address, pin=pin)
        async with held.lock:
            await self._reads(held.session, address)

    def read(self):
        self._spawn("read", lambda: self._read(self.selected))

    def connect(self):
        self._spawn("connect", lambda: self._read(self.selected))

    def disconnect(self, address=None):
        address = (address or self.selected or "").upper()
        if not address:
            raise RuntimeError("No machine chosen.")
        self.released.add(address)
        self._spawn("disconnect", lambda: self._release(address))

    async def _read(self, address):
        if not address:
            raise RuntimeError("No machine selected.")
        if store.driver(address) == AIRSENSE11:
            if not store.credentials(address):
                raise RuntimeError("Pair the AirSense using its screen code first.")
            self.released.discard(address)
            driver = self._air11_driver(address)
            if driver.auth_required:
                raise RuntimeError(driver.problem)
            driver.start()
            driver.request_read()
            return
        held = await self._held(address)
        try:
            async with held.lock:
                await self._reads(held.session, address)
        except Exception:
            await self._release(address)
            raise

    def forget(self):
        self._spawn("forget", self._forget)

    async def _forget(self):
        address = self.selected
        dev = self._selected_device()
        if dev is not None and self.adapter and store.driver(address) == AIRMINI:
            try:
                await adapter.remove(self.bus, self.adapter["path"], dev["path"])
                log.info("Unpaired %s.", dev["address"])
            except Exception as err:  # noqa: BLE001
                log.warning("Could not unpair the machine: %s", err)
        if address:
            await self._release(address)
            self.released.discard(address)
            self.publisher.forget(address)
            store.forget(address)
            if self.history is not None:
                self.history.forget(address)
            self._therapy_history_pending.discard(address)
            self.readings.pop(address, None)
        self.selected = store.selected()
        self.session_state = None

    # --- connections we keep -----------------------------------------------

    async def _held(self, address, pin=None):
        """The open connection to a machine, opening one if there is not one.

        Connections are kept rather than made for each reading. The machine
        only accepts one for a while after it is powered on, so letting go
        can mean needing the button on it to get back in -- which is not
        something to ask of anybody at 3am. Holding it costs the ResMed app
        the machine, which is what Disconnect is for.
        """
        held = self.sessions.get(address)
        if pin is None and held is not None and held.alive:
            return held
        if held is not None:
            await self._release(address)
        if pin is None:
            return await self._reconnect(address)
        return await self._connect(address, pin)

    async def _reconnect(self, address):
        """Try the existing application key without unpairing or stealing SPP.

        A transient page/profile error triggers a short Classic inquiry. This
        refreshes BlueZ's remote clock offset and page-scan information before
        another attempt. Failed attempts never erase the stored pairing key.
        """
        for attempt in range(1, RECONNECT_ATTEMPTS + 1):
            try:
                held = await self._connect(address)
                if attempt > 1:
                    log.info("Reconnected to %s on attempt %d/%d.",
                             address, attempt, RECONNECT_ATTEMPTS)
                return held
            except spp.NotReachable as err:
                if not err.retryable or attempt == RECONNECT_ATTEMPTS:
                    raise
                log.warning("Bluetooth reconnect %d/%d: %s.",
                            attempt, RECONNECT_ATTEMPTS, err)
                # A manually started scan owns its own inquiry: don't start a
                # competing scan or touch the Bluetooth bond.
                if not self.scanning and self.adapter:
                    try:
                        self.scan(RECONNECT_SCAN_SECONDS)
                        await self._scan_finished()
                        await self.refresh(force=True)
                    except Exception as scan_err:  # noqa: BLE001
                        log.info("Bluetooth discovery refresh skipped: %s",
                                 type(scan_err).__name__)
                await asyncio.sleep(RECONNECT_RETRY_PAUSE)
        raise RuntimeError("Reconnect attempts exhausted")

    async def _connect(self, address, pin=None):
        dev = next((d for d in self.devices if d["address"] == address), None)
        if dev is None:
            raise RuntimeError("Machine not in range.")
        if not dev["paired"]:
            raise RuntimeError("Not paired. Pair first.")

        fd = await spp.connect(self.bus, dev["path"], self.profile)

        # Imported here so the page still comes up on an image where
        # libairmini failed to build -- the survey is useful on its own.
        from .airmini import Session

        session = Session(fd, on_lost=lambda err, a=address: self._lost(a, err))
        try:
            session.open()
            await self._handshake(session, address, pin)
        except BaseException:
            session.close()
            spp.close(fd)
            raise

        held = Held(address, dev["path"], fd, session)
        self.sessions[address] = held
        self.profile.on_hangup(dev["path"], lambda a=address: self._lost(a, None))
        self.released.discard(address)
        log.info("Connected to %s.", address)
        return held

    async def _handshake(self, session, address, pin):
        if pin is not None:
            log.info("Pairing with the machine.")
            result = await asyncio.wait_for(session.pair(pin), HANDSHAKE_TIMEOUT)
            key = master_pair_key(result)
            if key:
                store.remember_master_pair_key(address, key)
            else:
                log.warning("No pairing key in the result (%s).", type(result).__name__)
            return

        key = store.master_pair_key(address)
        if not key:
            raise RuntimeError("No pairing key. Enter the PIN first.")
        await asyncio.wait_for(session.open_session(key), HANDSHAKE_TIMEOUT)

    def _lost(self, address, err):
        """The machine went away on its own. Tidy up in the background."""
        held = self.sessions.pop(address, None)
        if held is None:
            return
        log.info("Disconnected from %s.", address)
        self._republish(address)
        asyncio.create_task(self._tidy(held))

    async def _release(self, address):
        """Let go of a machine deliberately."""
        ble = self.air11.get(address)
        if ble is not None:
            await ble.stop()
            self.air11.pop(address, None)
        held = self.sessions.pop(address, None)
        if held is None:
            return
        await self._tidy(held)
        self._republish(address)
        log.info("Released %s.", address)

    async def _tidy(self, held):
        self.session_state = held.session.state
        held.session.close()
        spp.close(held.fd)
        self.profile.forget(held.path)
        try:
            await adapter.disconnect_profile(self.bus, held.path)
        except Exception:  # noqa: BLE001 -- the machine may have hung up first
            pass

    async def _reads(self, session, address):
        """Everything the machine will say, in one connection.

        Each read is reported on its own rather than aborting on the first
        failure: three of four is a more useful answer than the first one not.
        """
        reads = (
            ("GetVersion", session.get_version),
            ("GetDateTime", session.get_datetime),
            ("Settings", session.get_settings),
            ("MachineMetrics", session.get_metrics),
        )
        results = {}
        failures = 0
        for title, call in reads:
            try:
                value = await asyncio.wait_for(call(), READ_TIMEOUT)
                results[title] = {"ok": True, "value": value}
                _dump(title, value)
            except asyncio.TimeoutError:
                results[title] = {"ok": False, "error": f"timed out after {READ_TIMEOUT}s"}
                log.error("%s timed out after %ds.", title, READ_TIMEOUT)
                failures += 1
            except Exception as err:  # noqa: BLE001
                results[title] = {"ok": False, "error": str(err)}
                log.error("%s failed: %s", title, err)
                failures += 1
        if self.history is not None:
            await self._sync_history(session, address, results)
        self.readings[address] = {
            "results": results,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            # What Home Assistant is told, which wants an offset rather than
            # a local time with nothing to anchor it.
            "at_iso": _now_iso(),
        }
        self._publish(address)
        if failures:
            log.warning("%d of 4 reads failed.", failures)
        else:
            log.info("All four reads returned.")

    async def _sync_history(self, session, address, results):
        """Use the existing session lock for standby and scheduled downloads."""
        snapshot = entities.state(results, None)
        status = snapshot.get("status", "").casefold()
        if status == "therapy":
            self._therapy_history_pending.add(address)
        standby = status == "standby"
        therapy_ended = standby and address in self._therapy_history_pending
        last_therapy_use = snapshot.get("last_used") if standby else None
        if not therapy_ended and not self.history.is_due(address, last_therapy_use=last_therapy_use):
            return

        if therapy_ended:
            log.info("AirMini entered standby after therapy; syncing history.")
        try:
            received, nights = await self.history.sync(session, address)
        except Exception as err:  # noqa: BLE001
            # Keep a failed standby download pending for the next read.
            log.warning("Historical sync deferred: %s", type(err).__name__)
        else:
            if therapy_ended:
                self._therapy_history_pending.discard(address)
            log.info("Historical sync completed: %d notifications, %d nights.",
                     received, nights)


def master_pair_key(result):
    """Pull the pairing key out of whatever libairmini handed back.

    Three shapes, because the one that actually arrives is a truncated
    version of the one that was meant to: the object when the JSON survives,
    the `masterPairKey` field lifted out of the text when it does not, and a
    bare key on its own.

    Checked rather than trusted, in every case. Storing something that is not
    the key is a reconnect that fails every night afterwards and looks like
    the machine's fault. Never logged: holding this is what makes a PIN
    unnecessary, so it is as good as the PIN.
    """
    if isinstance(result, dict):
        return _checked(result.get("masterPairKey"))
    if not isinstance(result, str):
        log.warning("The pairing result is a %s, which carries no key.", type(result).__name__)
        return None

    found = _PAIR_KEY_FIELD.search(result)
    if found:
        return _checked(found.group(1))
    key = _checked(result.strip())
    if key is None:
        log.warning(
            "No key in the pairing result: %d characters, no masterPairKey field, not hex.",
            len(result),
        )
    return key


def _checked(candidate):
    if not isinstance(candidate, str) or not _PAIR_KEY.fullmatch(candidate.strip()):
        return None
    key = candidate.strip()
    log.info("Pairing returned a %d-character key.", len(key))
    return key


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def _dump(title, value):

    """Log a result in full, at debug level.

    The page lays readings out now, so this is no longer how anyone reads
    them -- but a whole machine's JSON is exactly what is worth pasting into
    an issue, and at info level ninety lines of it buried everything else in
    the activity log. AIRSUPPLY_LOG_LEVEL=debug brings it back.
    """
    log.debug("--- %s ---", title)
    if value is None:
        log.debug("(empty)")
        return
    text = value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True)
    for line in text.splitlines():
        log.debug("%s", line)
