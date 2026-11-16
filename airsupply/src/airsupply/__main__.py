"""airsupply add-on entry point.

Serves a page through Home Assistant's ingress: find the machine, pair with it,
and read back what it will say -- firmware, its clock, the therapy settings and
the run meters.

It reads and never writes. The AirMini is a medical device someone sleeps
attached to, and libairmini's write path is unverified upstream, so no Set
method is declared in this process and none can be sent from it. docs/verify.md
is where the case for changing that would have to be made.

Configuration is environment: AIRSUPPLY_LOG_LEVEL and AIRSUPPLY_PORT. Which
machine, and the key that pairs with it, live in /data and are managed from the
page.
"""

import asyncio
import logging
import os
import signal
import sys

from dbus_next import BusType
from dbus_next.aio import MessageBus

from . import web
from .controller import Controller

log = logging.getLogger("airsupply")

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


async def run():
    port = int(os.environ.get("AIRSUPPLY_PORT", "8099"))
    version = os.environ.get("AIRSUPPLY_VERSION", "dev").lstrip("v")

    # negotiate_unix_fd is not optional: without it BlueZ's NewConnection call
    # carries a file descriptor the bus is not prepared to receive, and the
    # message is rejected rather than delivered.
    bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
    log.debug("Connected to the system bus.")

    controller = Controller(bus, version)
    runner = None
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            pass

    try:
        await controller.start()
        runner = await web.serve(web.make_app(controller), port)

        # A normal container stop should release our SPP descriptor/profile.
        # An unexpected D-Bus disconnect still exits for Supervisor restart.
        bus_lost = asyncio.create_task(bus.wait_for_disconnect())
        stopping = asyncio.create_task(shutdown.wait())
        try:
            done, _ = await asyncio.wait(
                (bus_lost, stopping), return_when=asyncio.FIRST_COMPLETED
            )
            if bus_lost in done and not shutdown.is_set():
                log.error("Lost the system bus.")
                return 1
            log.info("Shutdown requested; releasing our Bluetooth sessions.")
            return 0
        finally:
            for task in (bus_lost, stopping):
                if not task.done():
                    task.cancel()
            await asyncio.gather(bus_lost, stopping, return_exceptions=True)
    finally:
        if runner is not None:
            await runner.cleanup()
        await controller.stop()
        if bus.connected:
            bus.disconnect()


def main():
    level = LEVELS.get(os.environ.get("AIRSUPPLY_LOG_LEVEL", "info").lower(), logging.INFO)
    logging.basicConfig(level=level, format="%(levelname)-7s %(message)s", stream=sys.stdout)
    # aiohttp's access log is noise under polling; errors still surface.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001
        log.exception("airsupply failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
