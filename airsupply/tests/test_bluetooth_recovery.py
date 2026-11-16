"""Unattended Bluetooth reconnection policy; all simulations are synthetic."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from airsupply.controller import Controller
from airsupply.bluez.spp import NotReachable, SerialProfile, transient, profile_options


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


class ProfileRoleTests(unittest.TestCase):
    def test_client_only_is_the_safe_default(self):
        opts = profile_options()
        self.assertEqual("client", opts["Role"].value)
        self.assertFalse(opts["AutoConnect"].value)

    def test_dual_role_does_not_force_client_only(self):
        opts = profile_options(dual_role=True)
        self.assertNotIn("Role", opts)
        self.assertFalse(opts["AutoConnect"].value)
        self.assertTrue(opts["RequireAuthentication"].value)


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


class InboundProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepted_incoming_is_retained_and_taken_once(self):
        profile = SerialProfile(on_incoming=lambda path: path == "/allowed")
        profile.dual_role = True
        with patch("airsupply.bluez.spp.close") as close:
            profile.NewConnection("/allowed", 91, {})
            self.assertEqual(91, profile.take_incoming("/allowed")[1])
            self.assertIsNone(profile.take_incoming("/allowed"))
            close.assert_not_called()

    async def test_unknown_and_unpaired_paths_are_closed(self):
        profile = SerialProfile(on_incoming=lambda path: False)
        profile.dual_role = True
        with patch("airsupply.bluez.spp.close") as close:
            profile.NewConnection("/unknown", 92, {})
            close.assert_called_once_with(92)
            self.assertEqual((), profile.pending_incoming())

    async def test_duplicate_incoming_does_not_replace_original_fd(self):
        profile = SerialProfile(on_incoming=lambda _: True)
        profile.dual_role = True
        with patch("airsupply.bluez.spp.close") as close:
            profile.NewConnection("/allowed", 93, {})
            profile.NewConnection("/allowed", 94, {})
            self.assertEqual(93, profile.take_incoming("/allowed")[1])
            close.assert_called_once_with(94)

    async def test_pending_outbound_wins_race_without_leak(self):
        profile = SerialProfile(on_incoming=lambda _: True)
        profile.dual_role = True
        future = profile.expect("/allowed")
        profile.NewConnection("/allowed", 95, {})
        self.assertEqual(95, (await future)[1])
        self.assertEqual((), profile.pending_incoming())

    async def test_unclaimed_incoming_closed_on_shutdown(self):
        profile = SerialProfile(on_incoming=lambda _: True)
        profile.dual_role = True
        with patch("airsupply.bluez.spp.close") as close:
            profile.NewConnection("/allowed", 96, {})
            profile.close()
            close.assert_called_once_with(96)

    async def test_controller_accepts_only_stored_paired_not_released(self):
        ctl = object.__new__(Controller)
        ctl.sessions = {}
        ctl.released = set()
        ctl._schedule_incoming = MagicMock()
        path = "/org/bluez/hci0/dev_58_26_3A_E8_C9_7D"
        entry = {"58:26:3A:E8:C9:7D": {"paired": True}}
        with patch("airsupply.controller.store.machines", return_value=entry), \
             patch("airsupply.controller.store.master_pair_key", return_value="fake-key"):
            self.assertTrue(ctl._accept_incoming(path))
            await asyncio.sleep(0)
            ctl._schedule_incoming.assert_called_once_with("58:26:3A:E8:C9:7D")
            ctl.released.add("58:26:3A:E8:C9:7D")
            self.assertFalse(ctl._accept_incoming(path))
            self.assertFalse(ctl._accept_incoming("/org/bluez/hci0/dev_UNKNOWN"))


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
