"""Synthetic Air11 transport, history and existing HTTP/MQTT contract checks.

The simulated FIG/SRP peripheral follows the MIT-licensed upstream tests by
Marcelo Oliveira de Araujo. The license is in airsense/protocol/LICENSE.
"""

import asyncio
import base64
import datetime as dt
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from airsupply import entities, mqtt, store, web
from airsupply.airsense.driver import Driver
from airsupply.airsense.history import nightly_summaries
from airsupply.airsense.protocol import AirSenseClient, AirSenseError, AuthError, Credentials
from airsupply.airsense.protocol.protocol import (
    SessionCipher, Reassembler, T_APP_ENC, T_DEV_ENC, T_DEV_PLAIN,
    build_frame, challenge_response, encode_json,
)
from airsupply.airsense.protocol.srp import SrpClient, SrpServer
from airsupply.bluez.adapter import _device
from airsupply.controller import Controller
from airsupply.history_store import HistoryStore
from airsupply.history_sync import HistoryManager
from airsupply.machine import AIR11_SERVICE, AIRMINI, AIRSENSE11

ADDRESS = "AA:BB:CC:DD:EE:11"
MINI_ADDRESS = "AA:BB:CC:DD:EE:01"


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, bytes):
        return varint(number << 3 | 2) + varint(len(value)) + value
    return varint(number << 3) + varint(value)


def summary_payload(usage=240, ahi=250):
    day = int(dt.datetime(2026, 1, 2, 12, tzinfo=dt.timezone(dt.timedelta(hours=11))).timestamp() * 1000)
    record = field(2, day) + field(3, day + 86400000) + field(5, usage)
    if ahi is not None:
        record += field(7, ahi)
    return field(2, record)


class SyntheticBLE:
    """A fake Air11 peripheral that speaks real encrypted FIG frames."""

    mtu_size = 64

    def __init__(self):
        self.is_connected = False
        self.notification = None
        self.disconnected = lambda _: None
        self.reader = Reassembler()
        self.server = None
        self.key = None
        self.cipher = None
        self.client_id = "SYNTHETIC"
        self.challenge = "11" * 32
        self.nonce = "22" * 32
        self.status = "Therapy"
        self.summary = summary_payload()
        self.requests = []
        self.summary_downloads = 0

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        self.disconnected(self)

    async def start_notify(self, characteristic, callback):
        self.notification = callback

    async def stop_notify(self, characteristic):
        self.notification = None

    def send(self, message, encrypted=False):
        payload = self.cipher.encrypt(message) if encrypted else encode_json(message)
        frame = build_frame(payload, T_DEV_ENC if encrypted else T_DEV_PLAIN)
        for offset in range(0, len(frame), 20):
            self.notification(None, bytearray(frame[offset:offset + 20]))

    def set_status(self, status):
        self.status = status
        self.send({
            "method": "EventNotification",
            "params": {"dataId": "FGState", "events": [{"value": status}]},
        }, encrypted=True)

    async def write_gatt_char(self, characteristic, data, response=True):
        for frame_type, payload in self.reader.feed(bytes(data)):
            message = self.cipher.decrypt(payload) if frame_type == T_APP_ENC else json.loads(payload)
            self.handle(message)

    def handle(self, message):
        method, params, identifier = message["method"], message.get("params"), message["id"]
        self.requests.append(method)
        encrypted = method not in {"StartKeyExchange", "ConfirmKeyExchange", "RequestSession", "CheckSessionIntegrity"}
        result = {}
        if method == "StartKeyExchange":
            self.server = SrpServer("123456")
            self.client_public = params["clientPk"]
            result = {"serverPk": self.server.B.hex(), "salt": self.server.salt.hex()}
        elif method == "ConfirmKeyExchange":
            confirmation = self.server.process_client(self.client_public, params["clientConfirmation"])
            self.key = self.server.key
            self.cipher = SessionCipher(self.key, self.nonce)
            result = {"clientId": self.client_id, "serverConfirmation": confirmation, "nonce": self.nonce}
        elif method == "RequestSession":
            result = {"challenge": self.challenge, "nonce": self.nonce}
        elif method == "CheckSessionIntegrity":
            result = {"confirmation": params["response"] == challenge_response(self.key, self.challenge)}
            self.cipher = SessionCipher(self.key, self.nonce)
        elif method == "Get":
            available = {
                "FGState": self.status,
                "MachineMetrics": {"TherapyRunMeter": "PT72000S"},
                "IdentificationProfiles": {
                    "Product": {"ProductName": "AirSense 11 AutoSet"},
                    "Software": {"ApplicationIdentifier": "SYNTHETIC"},
                },
                "SettingProfiles": {"ActiveProfiles": {"TherapyProfile": "AutoSetProfile"},
                                    "TherapyProfiles": {"AutoSetProfile": {"TherapyMode": "AutoSet"}}},
            }
            result = {name: available[name] for name in params}
        elif method == "StartSpool":
            self.summary_downloads += 1
            result = {"spoolId": 3}
        self.send({"id": identifier, "result": result}, encrypted)
        if method == "PullSpoolFragments":
            self.send({
                "method": "SpoolFragment",
                "params": {"spoolId": 3, "seq": 0, "data": base64.b64encode(self.summary).decode(),
                           "status": "SPOOL_COMPLETE_NO_MORE_DATA"},
            }, encrypted=True)


class ProtocolTests(unittest.TestCase):
    def test_srp_and_session_cipher_roundtrip(self):
        server = SrpServer("123456")
        client = SrpClient("123456")
        proof = client.process_challenge(server.B.hex(), server.salt.hex())
        confirmation = server.process_client(client.public_key_hex, proof)
        key = client.verify_server(confirmation)
        self.assertEqual(key, server.key)
        cipher = SessionCipher(key, "22" * 32)
        document = {"id": 5, "method": "Get", "params": ["FGState"]}
        self.assertEqual(document, cipher.decrypt(cipher.encrypt(document)))

    def test_fragmented_frames_and_dropped_notification(self):
        first = build_frame(b"x" * 300, T_DEV_PLAIN)
        second = build_frame(b'{"ok":true}', T_DEV_PLAIN)
        reader = Reassembler()
        output = reader.feed(first[:100]) + reader.feed(first[200:]) + reader.feed(second)
        self.assertEqual([(T_DEV_PLAIN, b'{"ok":true}')], output)


class HistoryTests(unittest.TestCase):
    def test_indices_stay_indices_and_machine_counts_are_unknown(self):
        nights = nightly_summaries(summary_payload(), "Australia/Melbourne")
        self.assertEqual("2026-01-02", nights[0]["night"])
        self.assertEqual(4, nights[0]["usage_hours"])
        self.assertEqual(2.5, nights[0]["ahi"])
        self.assertNotIn("ahi_events", nights[0])
        self.assertNotIn("obstructive_apnoeas", nights[0])

    def test_missing_ahi_and_zero_usage_remain_distinct(self):
        for payload in (summary_payload(ahi=None), summary_payload(usage=0)):
            self.assertNotIn("ahi", nightly_summaries(payload, "UTC")[0])

    def test_partial_spools_are_rejected(self):
        payload = summary_payload()
        for malformed in (payload[:-1], b"\x12\x80", b"\x00\x01"):
            with self.subTest(payload=malformed), self.assertRaises(ValueError):
                nightly_summaries(malformed, "UTC")

    def test_classic_and_ble_are_discovered_with_distinct_drivers(self):
        mini = _device("/mini", {"Address": MINI_ADDRESS, "Name": "AirMini", "Class": 0})
        sense = _device("/sense", {"Address": ADDRESS, "Name": "ResMed Test", "UUIDs": [AIR11_SERVICE]})
        self.assertEqual(AIRMINI, mini["driver"])
        self.assertEqual(AIRSENSE11, sense["driver"])
        self.assertTrue(sense["candidate"])


class DriverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(patch.object(store, "PATH", str(Path(folder.name) / "machines.json")))
        store.add(ADDRESS, "Synthetic AirSense", driver=AIRSENSE11)
        store.add(MINI_ADDRESS, "Synthetic AirMini")
        store.remember_master_pair_key(MINI_ADDRESS, "ab" * 32)
        self.history = HistoryManager(store=HistoryStore(str(Path(folder.name) / "h.db")))
        self.history.clock_offset = 0
        self.history.timezone = "Australia/Melbourne"
        self.updated = Mock()
        self.driver = Driver(ADDRESS, self.updated, self.history)
        self.driver._client = AsyncMock()
        self.driver._client.spool.return_value = summary_payload()
        self.addAsyncCleanup(self.driver.stop)

    async def test_standby_push_is_immediate_and_history_is_delayed(self):
        self.driver._on_event("FGState", "Therapy", {})
        self.driver._on_event("FGState", "Standby", {})
        self.assertEqual("Standby", entities.state(self.driver.results, None)["status"])
        self.assertGreater(self.driver._summary_at, time.monotonic() + 58)
        self.driver._client.spool.assert_not_awaited()
        await self.driver._sync_history()
        self.assertEqual(2.5, self.history.recent(ADDRESS)[0]["ahi"])
        self.assertEqual(float("inf"), self.driver._summary_at)

    async def test_partial_pull_preserves_prior_history_and_retries(self):
        self.history.store.save(ADDRESS, [{"night": "2026-01-01", "usage_hours": 3, "ahi": 1}])
        self.driver._summary_at = 0
        self.driver._client.spool.return_value = summary_payload()[:-1]
        with self.assertLogs("airsupply", level="WARNING"):
            await self.driver._sync_history()
        self.assertEqual("2026-01-01", self.history.recent(ADDRESS)[0]["night"])
        self.assertGreater(self.driver._summary_retry_at, time.monotonic() + 118)
        self.driver._client.spool.return_value = summary_payload()
        await self.driver._sync_history()
        self.assertEqual("2026-01-02", self.history.recent(ADDRESS)[0]["night"])

    async def test_therapy_end_during_download_keeps_its_followup(self):
        self.driver._on_event("FGState", "Therapy", {})
        self.driver._summary_at = 0
        async def completed_after_mask_off(*args):
            self.driver._on_event("FGState", "Standby", {})
            return summary_payload()
        self.driver._client.spool.side_effect = completed_after_mask_off
        await self.driver._sync_history()
        self.assertLess(self.driver._summary_at, float("inf"))
        self.assertGreater(self.driver._summary_at, time.monotonic() + 58)

    async def test_get_settings_normalises_air11_profiles(self):
        async def get(*names):
            if names == ("SettingProfiles",):
                return {"SettingProfiles": {
                    "ActiveProfiles": {"TherapyProfile": "AutoSetProfile"},
                    "TherapyProfiles": {"AutoSetProfile": {"TherapyMode": "AutoSet", "MinPressure": 8}},
                }}
            return {"FGState": "Standby"}
        self.driver._client.get.side_effect = get
        settings = await self.driver._settings()
        state = entities.state({"Settings": {"ok": True, "value": settings}}, None)
        self.assertEqual("AutoSet", state["mode"])
        self.assertEqual(8, state["pressure_min"])

    async def test_readonly_requests_cannot_write_settings(self):
        client = AirSenseClient(AsyncMock())
        for method in ("Set", "SetDateTime", "StartTherapy", "SetSettings"):
            with self.assertRaises(AirSenseError):
                await client.call(method)
        client._ble.write_gatt_char.assert_not_awaited()

    async def test_credentials_are_machine_scoped_and_private(self):
        credentials = Credentials("SYNTHETIC", b"x" * 32).as_dict()
        store.remember_credentials(ADDRESS, credentials)
        self.assertEqual(credentials, store.credentials(ADDRESS))
        self.assertEqual("ab" * 32, store.master_pair_key(MINI_ADDRESS))
        self.assertEqual(AIRMINI, store.driver(MINI_ADDRESS))
        self.assertNotIn(credentials["master_pair_key"], json.dumps(store.machines()))
        self.assertEqual(0o600, os.stat(store.PATH).st_mode & 0o777)

    async def test_pairing_stores_credentials_and_shutdown_releases_ble(self):
        radio = Mock(is_connected=True)
        radio.connect = AsyncMock()
        radio.disconnect = AsyncMock()
        protocol = AsyncMock()
        protocol.pair.return_value = Credentials("SYNTHETIC", b"x" * 32)
        wait = asyncio.Event()
        self.driver._monitor = wait.wait
        with patch("airsupply.airsense.driver.BleakScanner.find_device_by_address", AsyncMock(return_value=object())), \
             patch("airsupply.airsense.driver.BleakClient", return_value=radio), \
             patch("airsupply.airsense.driver.AirSenseClient", return_value=protocol):
            await self.driver.pair("123456")
            self.assertTrue(self.driver.connected)
            self.assertEqual("SYNTHETIC", store.credentials(ADDRESS)["client_id"])
            await self.driver.stop()
        self.assertFalse(self.driver.connected)
        protocol.stop.assert_awaited_once()
        radio.disconnect.assert_awaited_once()

    async def test_reconnect_uses_saved_credentials_and_handles_auth_rejection(self):
        store.remember_credentials(ADDRESS, Credentials("SYNTHETIC", b"x" * 32).as_dict())
        radio = Mock(is_connected=True)
        radio.connect = AsyncMock()
        radio.disconnect = AsyncMock()
        protocol = AsyncMock()
        protocol.open_session.side_effect = AuthError("replaced pairing")
        with patch("airsupply.airsense.driver.BleakScanner.find_device_by_address", AsyncMock(return_value=object())), \
             patch("airsupply.airsense.driver.BleakClient", return_value=radio), \
             patch("airsupply.airsense.driver.AirSenseClient", return_value=protocol):
            self.driver.start()
            await asyncio.wait_for(self.driver._task, 1)
        self.assertTrue(self.driver.auth_required)
        self.assertIsNotNone(store.credentials(ADDRESS))
        protocol.pair.assert_not_awaited()
        protocol.open_session.assert_awaited_once()


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.enterContext(patch.object(store, "PATH", str(Path(self.folder.name) / "machines.json")))
        self.enterContext(patch.dict(os.environ, {"AIRSUPPLY_HISTORY_ENABLED": "0"}))
        self.controller = Controller(None, "test")
        self.controller.devices = [_device("/sense", {"Address": ADDRESS, "Name": "ResMed Synthetic", "UUIDs": [AIR11_SERVICE]})]
        self.controller.adapter = {"path": "/org/bluez/hci0"}
        self.controller.refresh = AsyncMock()
        self.enterContext(patch.object(web, "ASSETS", Path(self.folder.name)))
        self.client = TestClient(TestServer(web.make_app(self.controller)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_discovered_air11_uses_the_existing_setup_route_without_a_bluez_bond(self):
        reply = await self.client.post("/api/select", json={"address": ADDRESS})
        page = await reply.json()
        self.assertEqual("pin", page["stage"])
        self.assertEqual(AIRSENSE11, page["machine"]["driver"])
        self.assertFalse(page["machine"]["bonded"])
        self.assertEqual(AIRSENSE11, store.driver(ADDRESS))

    async def test_original_airmini_identity_and_mixed_machine_publishing(self):
        store.add(MINI_ADDRESS, "AirMini")
        store.remember_master_pair_key(MINI_ADDRESS, "ab" * 32)
        store.add(ADDRESS, "AirSense", driver=AIRSENSE11)
        messages = {}
        publisher = mqtt.Publisher("test")
        publisher._send = messages.__setitem__
        with patch.object(mqtt, "configured", return_value=True):
            for address, model in ((MINI_ADDRESS, "AirMini"), (ADDRESS, "AirSense 11 AutoSet")):
                publisher.publish(address, model, None, {}, None, True, nights=[], model=model)
        mini = messages[f"homeassistant/binary_sensor/airsupply_{entities.node(MINI_ADDRESS)}/connected/config"]
        sense = messages[f"homeassistant/binary_sensor/airsupply_{entities.node(ADDRESS)}/connected/config"]
        self.assertEqual("AirMini", mini["device"]["model"])
        self.assertEqual("AirSense 11 AutoSet", sense["device"]["model"])
        self.assertEqual(f"airsupply_{entities.node(MINI_ADDRESS)}_connected", mini["unique_id"])
        self.assertNotEqual(mini["state_topic"], sense["state_topic"])

    async def test_deliberate_disconnect_stops_only_selected_driver(self):
        store.add(ADDRESS, driver=AIRSENSE11)
        driver = Mock(connected=True)
        driver.stop = AsyncMock()
        self.controller.air11[ADDRESS] = driver
        self.controller.sessions[MINI_ADDRESS] = object()
        await self.controller._release(ADDRESS)
        driver.stop.assert_awaited_once()
        self.assertIn(MINI_ADDRESS, self.controller.sessions)
        self.assertNotIn(ADDRESS, self.controller.air11)

    async def test_setup_read_standby_history_and_reconnect_through_http(self):
        self.controller.history = HistoryManager(
            store=HistoryStore(str(Path(self.folder.name) / "history.sqlite3")),
            timezone="Australia/Melbourne", clock_offset=0,
        )
        radio = SyntheticBLE()
        def ble_client(device, disconnected_callback, **kwargs):
            radio.disconnected = disconnected_callback
            return radio
        messages = {}
        self.controller.publisher._send = messages.__setitem__
        async def until(predicate):
            async with asyncio.timeout(2):
                while not predicate():
                    await asyncio.sleep(0.001)
        with patch("airsupply.airsense.driver.BleakScanner.find_device_by_address", AsyncMock(return_value=object())), \
             patch("airsupply.airsense.driver.BleakClient", side_effect=ble_client), \
             patch("airsupply.airsense.driver.SUMMARY_AFTER_STOP_SECONDS", 0), \
             patch.object(mqtt, "configured", return_value=True):
            await self.client.post("/api/select", json={"address": ADDRESS})
            try:
                response = await self.client.post("/api/pair", json={"pin": "123456"})
                self.assertEqual(200, response.status)
                await self.controller._task
                await until(lambda: bool(self.controller.history.recent(ADDRESS)))
                topic = f"airsupply/{entities.node(ADDRESS)}/state"
                self.assertEqual(2.5, messages[topic]["night_ahi"])
                self.assertEqual("on", messages[topic]["in_therapy"])
                self.assertEqual(20, messages[topic]["therapy_hours"])
                radio.summary = summary_payload(usage=360, ahi=125)
                radio.set_status("Standby")
                await until(lambda: messages[topic].get("night_ahi") == 1.25)
                self.assertEqual("off", messages[topic]["in_therapy"])
                self.assertEqual(6, messages[topic]["night_usage_hours"])
                saved = store.credentials(ADDRESS)
                await self.client.post("/api/disconnect", json={})
                await self.controller._task
                self.assertEqual("off", messages[topic]["connected"])
                await self.client.post("/api/connect", json={})
                await self.controller._task
                await until(lambda: messages[topic]["connected"] == "on")
                self.assertIn("RequestSession", radio.requests)
                self.assertEqual(saved, store.credentials(ADDRESS))
                page = await (await self.client.get("/api/state")).json()
                self.assertEqual("ready", page["stage"])
                self.assertEqual(AIRSENSE11, page["machine"]["driver"])
                self.assertNotIn(saved["master_pair_key"], json.dumps(page))
                self.assertFalse(any(request.startswith("Set") for request in radio.requests))
            finally:
                await self.controller._release(ADDRESS)
