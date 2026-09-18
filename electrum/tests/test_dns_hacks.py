import shutil
import tempfile

from electrum.dns_hacks import use_windows_dns_hack
from electrum.simple_config import SimpleConfig

from . import ElectrumTestCase


class Test_WindowsDnsHack(ElectrumTestCase):
    """The way out for a machine whose registry offers the wrong resolvers.

    dnspython reads Windows' DNS servers from the registry, which holds an
    entry for every adapter the machine has ever had, and the pinned version
    skips only adapters that are disabled rather than merely disconnected.
    See #19.
    """

    def setUp(self):
        super(Test_WindowsDnsHack, self).setUp()
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        super(Test_WindowsDnsHack, self).tearDown()
        shutil.rmtree(self.dir)

    def _config(self, **kwargs):
        return SimpleConfig({'electrum_path': self.dir, **kwargs})

    def test_on_by_default(self):
        self.assertTrue(use_windows_dns_hack(self._config()))

    def test_no_config_at_all_is_still_on(self):
        self.assertTrue(use_windows_dns_hack(None))

    def test_can_be_switched_off(self):
        self.assertFalse(use_windows_dns_hack(self._config(windows_dns_hack=False)))

    def test_switched_back_on(self):
        self.assertTrue(use_windows_dns_hack(self._config(windows_dns_hack=True)))
