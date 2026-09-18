import shutil
import socket
import sys
import tempfile
from unittest import mock

from electrum import dns_hacks
from electrum.dns_hacks import configure_dns_depending_on_proxy, use_windows_dns_hack
from electrum.simple_config import SimpleConfig

from . import ElectrumTestCase


class Test_WindowsDnsHack(ElectrumTestCase):
    """Resolving DNS ourselves on Windows is off.

    dnspython reads Windows' resolvers from the registry, which holds an entry
    for every adapter the machine has ever had, and the pinned version skips
    only adapters that are disabled rather than merely disconnected -- so a
    dead adapter answers for the wallet while the rest of the machine resolves
    correctly. See #19.
    """

    def setUp(self):
        super(Test_WindowsDnsHack, self).setUp()
        self.dir = tempfile.mkdtemp()
        # configure_dns_depending_on_proxy replaces socket.getaddrinfo, so put
        # the module back the way it was found.
        original = socket.getaddrinfo
        had_underscore = hasattr(socket, '_getaddrinfo')

        def restore():
            socket.getaddrinfo = original
            if not had_underscore and hasattr(socket, '_getaddrinfo'):
                del socket._getaddrinfo

        self.addCleanup(restore)

    def tearDown(self):
        super(Test_WindowsDnsHack, self).tearDown()
        shutil.rmtree(self.dir)

    def _config(self, **kwargs):
        return SimpleConfig({'electrum_path': self.dir, **kwargs})

    def test_off_by_default(self):
        self.assertFalse(use_windows_dns_hack(self._config()))

    def test_off_without_any_config(self):
        self.assertFalse(use_windows_dns_hack(None))

    def test_can_be_switched_on(self):
        self.assertTrue(use_windows_dns_hack(self._config(windows_dns_hack=True)))

    def test_explicit_false_stays_off(self):
        self.assertFalse(use_windows_dns_hack(self._config(windows_dns_hack=False)))

    def test_default_hands_resolution_to_the_system(self):
        with mock.patch.object(sys, 'platform', 'win32'):
            configure_dns_depending_on_proxy(False, config=self._config())
        self.assertIs(socket._getaddrinfo, socket.getaddrinfo)

    def test_switched_on_it_resolves_through_dnspython(self):
        with mock.patch.object(sys, 'platform', 'win32'), \
                mock.patch.object(dns_hacks, '_prepare_windows_dns_hack'):
            configure_dns_depending_on_proxy(
                False, config=self._config(windows_dns_hack=True))
        self.assertIs(dns_hacks._fast_getaddrinfo, socket.getaddrinfo)

    def test_a_proxy_still_wins(self):
        # names must not leak around the proxy, whatever the setting says
        with mock.patch.object(sys, 'platform', 'win32'):
            configure_dns_depending_on_proxy(
                True, config=self._config(windows_dns_hack=True))
        self.assertIsNot(socket._getaddrinfo, socket.getaddrinfo)
        self.assertIsNot(dns_hacks._fast_getaddrinfo, socket.getaddrinfo)
