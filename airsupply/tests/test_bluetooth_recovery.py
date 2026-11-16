"""Unattended Bluetooth reconnection policy; all simulations are synthetic."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from airsupply.controller import Controller
from airsupply.bluez.spp import NotReachable, SerialProfile, transient


class BluezClassifications(unittest.TestCase):
    def test_retries_only_transport_failures(self):
        for message in (
            "br-connection-page-timeout", "br-connection-in-progress",
            "br-connection-already-connected", "br-connection-refused",
        ):
            self.assertTrue(transient(RuntimeError(message)), message)
        for message in (
            "Not paired.", "br-connection-profile-unavailable",
            "br-connection-key-missing",
        ):
            self.assertFalse(transient(RuntimeError(message)), message)


class PendingConnections(unittest.IsolatedAsyncioTestCase):
    async def test_discard_a_failed_pending_future(self):
        profile = SerialProfile()
        future = profile.expect("/org/bluez/hci0/dev_AA")
        self.assertIn("/org/bluez/hci0/dev_AA", profile._waiting)
        profile.cancel_expectation("/org/bluez/hci0/dev_AA", future)
        self.assertNotIn("/org/bluez/hci0/dev_AA", profile._waiting)
        self.assertTrue(future.cancelled())

    async def test_old_cleanup_does_not_cancel_new_attempt(self):
        profile = SerialProfile()
        old = profile.expect("/a")
        new = profile.expect("/a")
        profile.cancel_expectation("/a", old)
        self.assertIs(new, profile._waiting["/a"])
        profile.cancel_expectation("/a", new)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctl = object.__new__(Controller)
        self.ctl.adapter = {"path": "/org/bluez/hci0"}
        self.ctl.scanning = False
        self.ctl.sessions = {}
        self.ctl.released = set()
        self.ctl.devices = [{"address": "AA:BB", "path": "/dev/AA", "paired": True}]
        self.ctl._connect = AsyncMock()
        self.ctl.scan = MagicMock()
        self.ctl._scan_finished = AsyncMock()
        self.ctl.refresh = AsyncMock()

    async def test_transport_recovers_after_inquiry(self):
        connected = object()
        self.ctl._connect.side_effect = [
            NotReachable("Page timeout", retryable=True),
            connected,
        ]
        with patch("airsupply.controller.asyncio.sleep", new_callable=AsyncMock):
            found = await self.ctl._reconnect("AA:BB")
        self.assertIs(found, connected)
        self.assertEqual(2, self.ctl._connect.await_count)
        self.ctl.scan.assert_called_once()

    async def test_no_retry_if_key_or_profile_missing(self):
        self.ctl._connect.side_effect = NotReachable("Not paired")
        with self.assertRaises(NotReachable):
            await self.ctl._reconnect("AA:BB")
        self.ctl.scan.assert_not_called()

    async def test_bounded_retry_does_not_steal_phone_session(self):
        self.ctl._connect.side_effect = NotReachable(
            "Bluetooth connected elsewhere or stale.", retryable=True
        )
        with patch("airsupply.controller.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(NotReachable):
                await self.ctl._reconnect("AA:BB")
        self.assertEqual(3, self.ctl._connect.await_count)
        self.assertEqual(2, self.ctl.scan.call_count)
        # No force disconnect, reset, or unpair is involved.
        self.assertFalse(hasattr(self.ctl, "_reset_adapter"))

    async def test_no_second_discovery_during_existing_scan(self):
        self.ctl.scanning = True
        self.ctl._connect.side_effect = [
            NotReachable("Page timeout", retryable=True), object()
        ]
        with patch("airsupply.controller.asyncio.sleep", new_callable=AsyncMock):
            await self.ctl._reconnect("AA:BB")
        self.ctl.scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
