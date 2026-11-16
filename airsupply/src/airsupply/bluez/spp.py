"""Open the AirMini's RFCOMM channel the way BlueZ intends.

Rather than opening an AF_BLUETOOTH socket ourselves, we register a Profile1
implementation for the Serial Port UUID and let BlueZ hand us a connected file
descriptor through NewConnection. Two reasons:

  * It respects the rule this add-on inherits from
    scyto/ha-bluetooth-audio-manager -- never touch /dev/hci*, always go through
    BlueZ -- so Home Assistant's passive BLE scan is undisturbed.
  * The fd is exactly the bidirectional byte stream libairmini wants for its
    `send` callback and `airmini_feed`, with no framing of our own in between.

Receiving a file descriptor over D-Bus requires the bus to have negotiated fd
passing; see main(). Without it dbus-next will refuse the message rather than
hand back a broken fd.
"""

import asyncio
import logging
import os

from dbus_next import Variant
from dbus_next.service import ServiceInterface, method

from . import constants as c

log = logging.getLogger(__name__)


class SerialProfile(ServiceInterface):
    """Where a connected serial channel arrives.

    One slot per machine rather than one for the profile. With a connection
    held open to each machine, two of them can be connecting at the same
    moment, and a single slot would hand the second machine's channel to
    whoever asked first.
    """

    def __init__(self):
        super().__init__(c.PROFILE)
        self._waiting = {}
        self._hangup = {}

    def expect(self, device_path):
        """Arm for a connection from one machine, and return its future."""
        pending = self._waiting.get(device_path)
        if pending is not None:
            self.cancel_expectation(device_path, pending)
        future = asyncio.get_running_loop().create_future()
        self._waiting[device_path] = future
        return future

    def cancel_expectation(self, device_path, pending):
        """Clean up a failed/aborted attempt, including an early delivered fd."""
        if self._waiting.get(device_path) is pending:
            self._waiting.pop(device_path, None)
        if pending.done() and not pending.cancelled():
            try:
                _, fd, _ = pending.result()
            except Exception:
                return
            close(fd)
        elif not pending.done():
            pending.cancel()

    def on_hangup(self, device_path, callback):
        self._hangup[device_path] = callback

    def forget(self, device_path):
        pending = self._waiting.get(device_path)
        if pending is not None:
            self.cancel_expectation(device_path, pending)
        self._hangup.pop(device_path, None)

    @method()
    def NewConnection(self, device: "o", fd: "h", fd_properties: "a{sv}"):  # noqa: N802
        props = {k: v.value for k, v in fd_properties.items()}
        future = self._waiting.pop(device, None)
        if future is not None and not future.done():
            log.debug("Channel open for %s on fd %s, %s", device, fd, props)
            future.set_result((device, fd, props))
        else:
            # In dual-role diagnostic mode an unsolicited inbound connection
            # is possible. Don't let an unknown peer take over the app, or
            # leak the descriptor. A later change can explicitly accept it.
            log.info("Unsolicited inbound RFCOMM connection from %s; closed.",
                     device)
            close(fd)

    @method()
    def RequestDisconnection(self, device: "o"):  # noqa: N802
        log.info("The machine asked to disconnect.")
        callback = self._hangup.get(device)
        if callback is not None:
            callback()

    @method()
    def Release(self):  # noqa: N802
        log.info("Serial profile released.")


def profile_options(*, dual_role=False):
    """Build BlueZ options without forcing the default dual role off.

    BlueZ enables client and server for Serial Port Profile by default.
    Passing Role=client prevents the local SDP service from being advertised;
    a paired AirMini was observed querying the Pi for that record. Enable the
    experimental dual role only when opted in, retaining outbound connections.
    """
    options = {
        "Name": Variant("s", "airsupply"),
        "AutoConnect": Variant("b", False),
        "RequireAuthentication": Variant("b", True),
        "RequireAuthorization": Variant("b", False),
    }
    if not dual_role:
        options["Role"] = Variant("s", "client")
    return options


async def register(bus):
    """Export our Profile1 and register it for the SPP UUID."""
    profile = SerialProfile()
    bus.export(c.PROFILE_PATH, profile)

    introspection = await bus.introspect(c.BLUEZ, c.BLUEZ_ROOT)
    obj = bus.get_proxy_object(c.BLUEZ, c.BLUEZ_ROOT, introspection)
    manager = obj.get_interface(c.PROFILE_MANAGER)

    # With Role omitted BlueZ registers *both* client and server. That
    # advertises a local Serial Port SDP record, while preserving existing
    # outbound ConnectProfile behaviour. This is opt-in until hardware tested.
    dual_role = os.environ.get("AIRSUPPLY_SPP_DUAL_ROLE", "0") == "1"
    await manager.call_register_profile(
        c.PROFILE_PATH,
        c.SPP_UUID,
        profile_options(dual_role=dual_role),
    )
    log.info("Registered Bluetooth SPP profile (%s).",
             "dual-role diagnostic" if dual_role else "outbound client")
    return profile


class NotReachable(Exception):
    """A failed SPP connection; retryable says whether another attempt is safe."""

    def __init__(self, message, *, retryable=False, reason=None):
        super().__init__(message)
        self.retryable = retryable
        self.reason = reason


def transient(err):
    """A transport-level failure worth retrying without touching the bond."""
    message = str(getattr(err, "text", "") or err).lower().replace("-", " ")
    if "profile unavailable" in message or "not paired" in message:
        return False
    return any(term in message for term in (
        "timeout", "already connected", "in progress",
        "refused", "not ready", "temporarily unavailable",
    ))


def explain(err):
    text = str(getattr(err, "text", "") or err).lower().replace("-", " ")
    if "timeout" in text:
        return (
            "No answer. Machine off or out of range."
        )
    if "profile unavailable" in text:
        return (
            "No serial port on the machine."
        )
    if "already connected" in text:
        return "Bluetooth connected elsewhere or stale."
    if "in progress" in text:
        return "Already connecting."
    if "refused" in text or "not available" in text:
        return "Connection refused."
    return f"Channel did not open: {getattr(err, 'text', None) or err}"


async def connect(bus, device_path, profile, timeout=30.0):
    """Connect the paired SPP profile via BlueZ, returning our owned fd.

    Both the D-Bus ConnectProfile call and its NewConnection callback are
    bounded. If either fails or is cancelled, discard the registered
    expectation and close any fd that arrived before the error.
    """
    introspection = await bus.introspect(c.BLUEZ, device_path)
    obj = bus.get_proxy_object(c.BLUEZ, device_path, introspection)
    device = obj.get_interface(c.DEVICE)

    if not await device.get_paired():
        raise NotReachable("Not paired.")

    arrived = profile.expect(device_path)
    log.debug("Opening the serial channel to %s", device_path)
    succeeded = False
    try:
        try:
            await asyncio.wait_for(device.call_connect_profile(c.SPP_UUID), timeout)
        except asyncio.TimeoutError as err:
            raise NotReachable(
                "Timed out opening the Bluetooth profile.", retryable=True,
                reason="page-timeout",
            ) from err
        except Exception as err:  # noqa: BLE001 -- BlueZ errors vary by version
            log.debug("ConnectProfile failed: %s", err)
            raise NotReachable(
                explain(err), retryable=transient(err), reason="bluez",
            ) from err

        try:
            _, fd, _ = await asyncio.wait_for(arrived, timeout)
        except asyncio.TimeoutError as err:
            raise NotReachable(
                "Connected, but the serial channel never opened.",
                retryable=True, reason="fd-timeout",
            ) from err

        succeeded = True
        return fd
    finally:
        if not succeeded:
            profile.cancel_expectation(device_path, arrived)

def close(fd):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
