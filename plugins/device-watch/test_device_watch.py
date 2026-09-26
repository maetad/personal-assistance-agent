"""Unit tests for device-watch's pure logic and tool handlers.
Run inside the hermes-agent container (tools.registry/hermes_constants live there, not on host):
  docker cp plugins/device-watch hermes-agent:/tmp/device-watch
  docker exec hermes-agent /opt/hermes/.venv/bin/python3 -m unittest -v discover -s /tmp/device-watch -p "test_*.py"
"""

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

_MODULE_PATH = pathlib.Path(__file__).parent / "__init__.py"
_spec = importlib.util.spec_from_file_location("device_watch", _MODULE_PATH)
dw = importlib.util.module_from_spec(_spec)
sys.modules["device_watch"] = dw
_spec.loader.exec_module(dw)


class UpdateStateTests(unittest.TestCase):
    def test_first_check_sets_baseline_without_notifying(self):
        watch = {"ip": "192.168.0.42", "online": None}
        message = dw._update_state("phone", watch, True)
        self.assertIsNone(message)
        self.assertTrue(watch["online"])

    def test_unchanged_state_does_not_notify(self):
        watch = {"ip": "192.168.0.42", "online": True}
        message = dw._update_state("phone", watch, True)
        self.assertIsNone(message)

    def test_online_to_offline_notifies_disconnect(self):
        watch = {"ip": "192.168.0.42", "online": True}
        message = dw._update_state("phone", watch, False)
        self.assertIn("phone", message)
        self.assertIn("disconnected", message)
        self.assertFalse(watch["online"])

    def test_offline_to_online_notifies_connect(self):
        watch = {"ip": "192.168.0.42", "online": False}
        message = dw._update_state("phone", watch, True)
        self.assertIn("phone", message)
        self.assertIn("connected", message)
        self.assertTrue(watch["online"])

    def test_records_changed_at_on_every_call(self):
        watch = {"ip": "192.168.0.42", "online": None}
        dw._update_state("phone", watch, True, now=1000.0)
        self.assertEqual(watch["changed_at"], 1000.0)

    def test_missing_min_offline_minutes_defaults_to_always_notify(self):
        watch = {"ip": "192.168.0.42", "online": False, "changed_at": 1000.0}
        message = dw._update_state("phone", watch, True, now=1000.1)
        self.assertIn("connected", message)

    def test_reconnect_before_threshold_is_suppressed_but_state_still_updates(self):
        watch = {"ip": "192.168.0.42", "online": False, "changed_at": 1000.0, "min_offline_minutes": 60}
        message = dw._update_state("phone", watch, True, now=1000.0 + 30 * 60)
        self.assertIsNone(message)
        self.assertTrue(watch["online"])
        self.assertEqual(watch["changed_at"], 1000.0 + 30 * 60)

    def test_reconnect_after_threshold_notifies(self):
        watch = {"ip": "192.168.0.42", "online": False, "changed_at": 1000.0, "min_offline_minutes": 60}
        message = dw._update_state("phone", watch, True, now=1000.0 + 61 * 60)
        self.assertIn("connected", message)

    def test_disconnect_is_never_suppressed_by_threshold(self):
        watch = {"ip": "192.168.0.42", "online": True, "changed_at": 1000.0, "min_offline_minutes": 60}
        message = dw._update_state("phone", watch, False, now=1000.1)
        self.assertIn("disconnected", message)


class ValidIpv4Tests(unittest.TestCase):
    def test_accepts_valid_ip(self):
        self.assertTrue(dw._valid_ipv4("192.168.0.1"))

    def test_rejects_garbage(self):
        self.assertFalse(dw._valid_ipv4("not-an-ip"))
        self.assertFalse(dw._valid_ipv4(""))


class WatchesRoundTripTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._path = pathlib.Path(self._tmpdir.name) / "watches.json"
        self._orig_path_fn = dw._watches_path
        dw._watches_path = lambda: self._path

    def tearDown(self):
        dw._watches_path = self._orig_path_fn
        self._tmpdir.cleanup()

    def test_load_missing_file_returns_empty_dict(self):
        self.assertEqual(dw._load_watches(), {})

    def test_save_then_load_round_trips(self):
        dw._save_watches({"phone": {"ip": "192.168.0.42", "online": True}})
        self.assertEqual(dw._load_watches(), {"phone": {"ip": "192.168.0.42", "online": True}})

    def test_corrupt_file_returns_empty_dict_instead_of_raising(self):
        self._path.write_text("not json")
        self.assertEqual(dw._load_watches(), {})


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._path = pathlib.Path(self._tmpdir.name) / "watches.json"
        self._orig_path_fn = dw._watches_path
        dw._watches_path = lambda: self._path
        self._orig_ensure_worker = dw._ensure_worker
        dw._ensure_worker = lambda: None  # no background thread in unit tests

    def tearDown(self):
        dw._watches_path = self._orig_path_fn
        dw._ensure_worker = self._orig_ensure_worker
        self._tmpdir.cleanup()

    def test_watch_device_rejects_invalid_ip(self):
        result = json.loads(dw._handle_watch_device({"name": "phone", "ip": "nope"}))
        self.assertFalse(result["success"])
        self.assertEqual(dw._load_watches(), {})

    def test_watch_device_persists_new_watch(self):
        result = json.loads(dw._handle_watch_device({"name": "phone", "ip": "192.168.0.42"}))
        self.assertTrue(result["success"])
        self.assertEqual(
            dw._load_watches(),
            {"phone": {"ip": "192.168.0.42", "online": None, "changed_at": None, "min_offline_minutes": 0}},
        )

    def test_watch_device_accepts_min_offline_minutes(self):
        result = json.loads(dw._handle_watch_device({"name": "phone", "ip": "192.168.0.42", "min_offline_minutes": 60}))
        self.assertTrue(result["success"])
        self.assertEqual(dw._load_watches()["phone"]["min_offline_minutes"], 60)

    def test_watch_device_rejects_negative_min_offline_minutes(self):
        result = json.loads(dw._handle_watch_device({"name": "phone", "ip": "192.168.0.42", "min_offline_minutes": -1}))
        self.assertFalse(result["success"])
        self.assertEqual(dw._load_watches(), {})

    def test_unwatch_device_removes_existing_watch(self):
        dw._handle_watch_device({"name": "phone", "ip": "192.168.0.42"})
        result = json.loads(dw._handle_unwatch_device({"name": "phone"}))
        self.assertTrue(result["success"])
        self.assertEqual(dw._load_watches(), {})

    def test_unwatch_device_unknown_name_errors(self):
        result = json.loads(dw._handle_unwatch_device({"name": "ghost"}))
        self.assertFalse(result["success"])

    def test_list_watched_devices_reports_current_state(self):
        dw._handle_watch_device({"name": "phone", "ip": "192.168.0.42"})
        result = json.loads(dw._handle_list_watched_devices({}))
        self.assertEqual(
            result["devices"],
            [{"name": "phone", "ip": "192.168.0.42", "online": None, "changed_at": None, "min_offline_minutes": 0}],
        )


class GuessSubnetTests(unittest.TestCase):
    def test_no_watches_returns_none(self):
        self.assertIsNone(dw._guess_subnet({}))

    def test_derives_slash_24_from_a_watched_ip(self):
        watches = {"phone": {"ip": "192.168.0.42", "online": True}}
        self.assertEqual(dw._guess_subnet(watches), "192.168.0.0/24")


class ResolveSubnetTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._path = pathlib.Path(self._tmpdir.name) / "watches.json"
        self._orig_path_fn = dw._watches_path
        dw._watches_path = lambda: self._path

    def tearDown(self):
        dw._watches_path = self._orig_path_fn
        self._tmpdir.cleanup()

    def test_configured_subnet_wins_over_guess(self):
        dw._save_watches({"phone": {"ip": "10.0.0.5", "online": True}})
        ctx = unittest.mock.Mock()
        ctx.get_config.return_value = "192.168.1.0/24"
        self.assertEqual(dw._resolve_subnet(ctx), "192.168.1.0/24")

    def test_falls_back_to_guess_when_unconfigured(self):
        dw._save_watches({"phone": {"ip": "10.0.0.5", "online": True}})
        ctx = unittest.mock.Mock()
        ctx.get_config.return_value = None
        self.assertEqual(dw._resolve_subnet(ctx), "10.0.0.0/24")

    def test_none_when_nothing_configured_and_nothing_watched(self):
        ctx = unittest.mock.Mock()
        ctx.get_config.return_value = None
        self.assertIsNone(dw._resolve_subnet(ctx))

    def test_none_ctx_falls_back_to_guess(self):
        dw._save_watches({"phone": {"ip": "10.0.0.5", "online": True}})
        self.assertEqual(dw._resolve_subnet(None), "10.0.0.0/24")


class ResolveHostnameTests(unittest.TestCase):
    def setUp(self):
        self._orig_gethostbyaddr = dw.socket.gethostbyaddr

    def tearDown(self):
        dw.socket.gethostbyaddr = self._orig_gethostbyaddr

    def test_returns_short_hostname_on_success(self):
        dw.socket.gethostbyaddr = lambda ip: ("phone.lan", [], [ip])
        self.assertEqual(dw._resolve_hostname("192.168.0.42"), "phone")

    def test_returns_none_when_lookup_fails(self):
        def _raise(ip):
            raise dw.socket.herror("unknown host")
        dw.socket.gethostbyaddr = _raise
        self.assertIsNone(dw._resolve_hostname("192.168.0.42"))


class RouterBaseUrlTests(unittest.TestCase):
    def test_configured_host_wins(self):
        ctx = unittest.mock.Mock()
        ctx.get_config.return_value = "http://10.0.0.1"
        self.assertEqual(dw._router_base_url(ctx, "192.168.0.0/24"), "http://10.0.0.1")

    def test_falls_back_to_gateway_guess_from_subnet(self):
        ctx = unittest.mock.Mock()
        ctx.get_config.return_value = None
        self.assertEqual(dw._router_base_url(ctx, "192.168.0.0/24"), "http://192.168.0.1")

    def test_none_when_no_subnet_and_nothing_configured(self):
        ctx = unittest.mock.Mock()
        ctx.get_config.return_value = None
        self.assertIsNone(dw._router_base_url(ctx, None))


class FetchRouterHostnamesTests(unittest.TestCase):
    def setUp(self):
        self._orig_get_router_client = dw._get_router_client

    def tearDown(self):
        dw._get_router_client = self._orig_get_router_client

    def test_returns_empty_when_no_password_configured(self):
        ctx = unittest.mock.Mock()
        ctx.get_config.return_value = None
        self.assertEqual(dw._fetch_router_hostnames(ctx, "192.168.0.0/24"), {})

    def test_maps_ip_to_hostname_and_skips_blank_hostnames(self):
        class FakeDevice:
            def __init__(self, ipaddr, hostname):
                self.ipaddr = ipaddr
                self.hostname = hostname

        class FakeStatus:
            devices = [FakeDevice("192.168.0.42", "phone"), FakeDevice("192.168.0.7", "")]

        class FakeRouter:
            # ponytail: not every tplinkrouterc6u client class (e.g. TplinkRouterSG, used
            # by newer Archer BE-series routers) implements the context-manager protocol,
            # so we drive authorize()/get_status()/logout() explicitly instead of `with`.
            def authorize(self):
                pass

            def get_status(self):
                return FakeStatus()

            def logout(self):
                pass

        dw._get_router_client = lambda base_url, password, username: FakeRouter()
        ctx = unittest.mock.Mock()
        ctx.get_config.side_effect = lambda key, default=None: {"router_password": "secret"}.get(key, default)
        result = dw._fetch_router_hostnames(ctx, "192.168.0.0/24")
        self.assertEqual(result, {"192.168.0.42": "phone"})

    def test_returns_empty_on_failure_instead_of_raising(self):
        def _raise(base_url, password, username):
            raise RuntimeError("boom")

        dw._get_router_client = _raise
        ctx = unittest.mock.Mock()
        ctx.get_config.side_effect = lambda key, default=None: {"router_password": "secret"}.get(key, default)
        self.assertEqual(dw._fetch_router_hostnames(ctx, "192.168.0.0/24"), {})

    def test_logs_out_even_when_get_status_raises(self):
        calls = []

        class FakeRouter:
            def authorize(self):
                calls.append("authorize")

            def get_status(self):
                raise RuntimeError("boom")

            def logout(self):
                calls.append("logout")

        dw._get_router_client = lambda base_url, password, username: FakeRouter()
        ctx = unittest.mock.Mock()
        ctx.get_config.side_effect = lambda key, default=None: {"router_password": "secret"}.get(key, default)
        self.assertEqual(dw._fetch_router_hostnames(ctx, "192.168.0.0/24"), {})
        self.assertEqual(calls, ["authorize", "logout"])


class SweepSubnetTests(unittest.TestCase):
    def setUp(self):
        self._orig_check_online = dw._check_online

    def tearDown(self):
        dw._check_online = self._orig_check_online

    def test_ignores_ips_outside_the_subnet_and_sorts_matches(self):
        online = {"192.168.0.5", "192.168.0.2", "192.168.0.200"}  # .200 is outside the /29
        dw._check_online = lambda ip, timeout=None: ip in online
        result = dw._sweep_subnet("192.168.0.0/29")  # small range keeps the test fast
        self.assertEqual(result, ["192.168.0.2", "192.168.0.5"])

    def test_matches_within_small_range(self):
        dw._check_online = lambda ip, timeout=None: ip == "192.168.0.2"
        result = dw._sweep_subnet("192.168.0.0/29")
        self.assertEqual(result, ["192.168.0.2"])


class ListConnectedDevicesHandlerTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._path = pathlib.Path(self._tmpdir.name) / "watches.json"
        self._orig_path_fn = dw._watches_path
        dw._watches_path = lambda: self._path
        self._orig_sweep = dw._sweep_subnet
        self._orig_resolve = dw._resolve_subnet
        self._orig_resolve_hostname = dw._resolve_hostname
        self._orig_fetch_router_hostnames = dw._fetch_router_hostnames
        dw._resolve_subnet = lambda ctx: "192.168.0.0/24"
        dw._resolve_hostname = lambda ip: None
        dw._fetch_router_hostnames = lambda ctx, subnet: {}

    def tearDown(self):
        dw._watches_path = self._orig_path_fn
        dw._sweep_subnet = self._orig_sweep
        dw._resolve_subnet = self._orig_resolve
        dw._resolve_hostname = self._orig_resolve_hostname
        dw._fetch_router_hostnames = self._orig_fetch_router_hostnames
        self._tmpdir.cleanup()

    def test_marks_watched_online_device(self):
        dw._save_watches({"phone": {"ip": "192.168.0.42", "online": None}})
        dw._sweep_subnet = lambda subnet: ["192.168.0.42", "192.168.0.7"]
        result = json.loads(dw._handle_list_connected_devices({}))
        self.assertTrue(result["success"])
        by_ip = {d["ip"]: d for d in result["devices"]}
        self.assertEqual(by_ip["192.168.0.42"], {"ip": "192.168.0.42", "online": True, "watched": True, "name": "phone", "hostname": None})
        self.assertEqual(by_ip["192.168.0.7"], {"ip": "192.168.0.7", "online": True, "watched": False, "name": None, "hostname": None})

    def test_includes_offline_watched_device_not_seen_in_sweep(self):
        dw._save_watches({"phone": {"ip": "192.168.0.42", "online": True}})
        dw._sweep_subnet = lambda subnet: []
        result = json.loads(dw._handle_list_connected_devices({}))
        self.assertEqual(result["devices"], [{"ip": "192.168.0.42", "online": False, "watched": True, "name": "phone", "hostname": None}])

    def test_resolves_hostname_for_online_devices(self):
        dw._save_watches({"phone": {"ip": "192.168.0.42", "online": None}})
        dw._sweep_subnet = lambda subnet: ["192.168.0.42"]
        dw._resolve_hostname = lambda ip: "phone" if ip == "192.168.0.42" else None
        result = json.loads(dw._handle_list_connected_devices({}))
        self.assertEqual(result["devices"][0]["hostname"], "phone")

    def test_router_hostname_takes_priority_over_reverse_dns(self):
        dw._save_watches({})
        dw._sweep_subnet = lambda subnet: ["192.168.0.42"]
        dw._fetch_router_hostnames = lambda ctx, subnet: {"192.168.0.42": "router-name"}
        dw._resolve_hostname = lambda ip: "dns-name"
        result = json.loads(dw._handle_list_connected_devices({}))
        self.assertEqual(result["devices"][0]["hostname"], "router-name")

    def test_no_subnet_available_returns_error(self):
        dw._resolve_subnet = lambda ctx: None
        result = json.loads(dw._handle_list_connected_devices({}))
        self.assertFalse(result["success"])


class PollOnceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._path = pathlib.Path(self._tmpdir.name) / "watches.json"
        self._orig_path_fn = dw._watches_path
        dw._watches_path = lambda: self._path
        self._orig_check_online = dw._check_online
        self._orig_notify = dw._notify
        self._notified = []
        dw._notify = lambda message: self._notified.append(message)

    def tearDown(self):
        dw._watches_path = self._orig_path_fn
        dw._check_online = self._orig_check_online
        dw._notify = self._orig_notify
        self._tmpdir.cleanup()

    def test_state_change_triggers_notify_and_persists(self):
        dw._save_watches({"phone": {"ip": "192.168.0.42", "online": True}})
        dw._check_online = lambda ip: False
        dw._poll_once()
        self.assertEqual(len(self._notified), 1)
        self.assertEqual(dw._load_watches()["phone"]["online"], False)

    def test_first_check_persists_baseline_even_without_notifying(self):
        dw._save_watches({"phone": {"ip": "192.168.0.42", "online": None}})
        dw._check_online = lambda ip: True
        dw._poll_once()
        self.assertEqual(self._notified, [])
        persisted = dw._load_watches()["phone"]
        self.assertTrue(persisted["online"])
        self.assertIsNotNone(persisted["changed_at"])

    def test_no_state_change_does_not_notify(self):
        dw._save_watches({"phone": {"ip": "192.168.0.42", "online": True}})
        dw._check_online = lambda ip: True
        dw._poll_once()
        self.assertEqual(self._notified, [])


if __name__ == "__main__":
    unittest.main()
