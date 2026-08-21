#!/usr/bin/env python3
"""The sandbox is WRITE containment, not network isolation — pin that, and pin
the opt-in that changes it.

Verified facts this file locks down (see PB-SANDBOX-READ-NETWORK):
  * bwrap starts with `--ro-bind / /` — the whole filesystem stays READABLE;
    "read-only" means writes are denied, not that reads are blocked.
  * the DEFAULT argv carries NO `--unshare-net` — the network is reachable by
    design, because judges invoke provider CLIs that call model APIs. This is
    the pin that stops the default from silently flipping to isolated.
  * the OPT-IN `--no-network` adds `--unshare-net` on the bwrap path ONLY.
  * on any non-bwrap backend (macOS seatbelt, Windows, nested sandbox) the
    opt-in FAILS LOUDLY rather than pretending the network is contained.

Run: python3 tests/test_sandbox_network_boundary.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from provider import sandbox  # noqa: E402
from provider.sandbox import build_bwrap_argv  # noqa: E402


class BwrapNetworkArgv(unittest.TestCase):
    def _argv(self, no_network):
        proj = str(Path(tempfile.mkdtemp()).resolve())
        return build_bwrap_argv(proj, None, ["true"], None,
                                project_writable=False, no_network=no_network)

    def test_default_has_no_unshare_net(self):
        """PIN: the default bwrap argv must NOT isolate the network — judges call
        model APIs. If this flips, the judge path silently loses the network."""
        self.assertNotIn("--unshare-net", self._argv(False),
                         "the default sandbox must leave the network reachable")

    def test_no_network_adds_unshare_net(self):
        self.assertIn("--unshare-net", self._argv(True),
                      "--no-network must add --unshare-net on the bwrap path")

    def test_full_filesystem_is_readable_via_ro_bind_root(self):
        """'read-only' == write-denial, NOT read isolation: root stays --ro-bind,
        so every path outside the project is still readable."""
        self.assertEqual(self._argv(False)[:4], ["bwrap", "--ro-bind", "/", "/"])


class NoNetworkFailsLoudly(unittest.TestCase):
    """The opt-in refuses to run rather than pretend, when the backend that would
    actually carry it (Linux bwrap) is not the one in play."""

    def _no_backend(self):
        return (
            mock.patch.object(sandbox, "is_sandboxed", return_value=False),
            mock.patch.object(sandbox.platform, "system", return_value="Linux"),
            mock.patch.object(sandbox.shutil, "which", return_value=None),
        )

    def test_raises_when_backend_is_not_bwrap(self):
        a, b, c = self._no_backend()
        with a, b, c:
            self.assertFalse(sandbox.network_isolation_available())
            with self.assertRaises(RuntimeError):
                sandbox._wrapped_argv("claude", ["-p", "hi"], Path("/proj"),
                                      None, False, no_network=True)

    def test_negative_control_no_flag_does_not_raise(self):
        """Same no-backend environment, but WITHOUT the opt-in the run proceeds
        uncontained (no --unshare-net) — so the raise above is caused by the flag,
        not by the missing backend on its own."""
        a, b, c = self._no_backend()
        with a, b, c:
            argv = sandbox._wrapped_argv("claude", ["-p", "hi"], Path("/proj"),
                                         None, False, no_network=False)
            self.assertNotIn("--unshare-net", argv)


class SeatbeltHasNoNetworkKnob(unittest.TestCase):
    def test_seatbelt_profile_never_isolates_network(self):
        """macOS seatbelt cannot isolate the network here — the generated profile
        must contain no network directive, so the opt-in can honestly fail
        loud on that backend instead of emitting a no-op."""
        profile = sandbox.build_seatbelt_profile("/proj", "/proj/.git", None)
        self.assertNotIn("network", profile.lower())


class CliNoNetworkGuards(unittest.TestCase):
    def test_no_network_with_prompt_is_rejected(self):
        """--no-network must not silently ride the --prompt/subagent (judge) path,
        where it is not threaded — reject it loudly instead of pretending."""
        rc = sandbox._main([
            "--no-network", "--agent", "claude", "--prompt", "hi",
            "--project-root", str(_HERE.parent),
        ])
        self.assertEqual(rc, 2)


@unittest.skipUnless(shutil.which("bwrap"), "bwrap not available")
class LiveNetworkIsolation(unittest.TestCase):
    """Real-os: --no-network actually collapses the interface set to loopback,
    while the default leaves the host's interfaces reachable.

    Offline-safe. The probe is `socket.if_nameindex()`, NOT `/sys/class/net`:
    `--ro-bind / /` bind-mounts the host's /sys, so /sys/class/net keeps showing
    the host's netdevs even inside a fresh netns — only the namespace-aware
    syscall reflects `--unshare-net`."""

    _PROBE = ("import socket; "
              "print(' '.join(sorted(n for _, n in socket.if_nameindex())))")

    def _ifaces(self, no_network):
        proj = Path(tempfile.mkdtemp(prefix="sbx-net-")).resolve()
        argv = build_bwrap_argv(proj, None, [sys.executable, "-c", self._PROBE],
                                None, project_writable=False, no_network=no_network)
        r = subprocess.run(argv, cwd=str(proj), capture_output=True,
                           text=True, timeout=60)
        return set(r.stdout.split())

    def test_no_network_leaves_only_loopback(self):
        self.assertEqual(self._ifaces(no_network=True), {"lo"},
                         "--unshare-net must collapse the sandbox to loopback only")

    def test_default_keeps_host_interfaces(self):
        """Negative control for the isolation test: without the flag the sandbox
        sees the host interfaces. Skips on the rare lo-only host so it never
        flakes — the isolation assertion above is the load-bearing one."""
        default = self._ifaces(no_network=False)
        if default == {"lo"}:
            self.skipTest("host itself exposes only loopback; contrast is vacuous")
        self.assertIn("lo", default)
        self.assertGreater(len(default), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
