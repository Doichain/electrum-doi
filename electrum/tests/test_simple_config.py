import ast
import sys
import os
import tempfile
import shutil

from io import StringIO
from electrum.simple_config import (SimpleConfig, read_user_config,
                                    FEERATE_STATIC_VALUES, FEERATE_MIN_MINEABLE,
                                    FEERATE_FALLBACK_STATIC_FEE,
                                    FEERATE_WARNING_HIGH_FEE)
from electrum import bitcoin

from . import ElectrumTestCase


class Test_SimpleConfig(ElectrumTestCase):

    def setUp(self):
        super(Test_SimpleConfig, self).setUp()
        # make sure "read_user_config" and "user_dir" return a temporary directory.
        self.electrum_dir = tempfile.mkdtemp()
        # Do the same for the user dir to avoid overwriting the real configuration
        # for development machines with electrum installed :)
        self.user_dir = tempfile.mkdtemp()

        self.options = {"electrum_path": self.electrum_dir}
        self._saved_stdout = sys.stdout
        self._stdout_buffer = StringIO()
        sys.stdout = self._stdout_buffer

    def tearDown(self):
        super(Test_SimpleConfig, self).tearDown()
        # Remove the temporary directory after each test (to make sure we don't
        # pollute /tmp for nothing.
        shutil.rmtree(self.electrum_dir)
        shutil.rmtree(self.user_dir)

        # Restore the "real" stdout
        sys.stdout = self._saved_stdout

    def test_simple_config_key_rename(self):
        """auto_cycle was renamed auto_connect"""
        fake_read_user = lambda _: {"auto_cycle": True}
        read_user_dir = lambda : self.user_dir
        config = SimpleConfig(options=self.options,
                              read_user_config_function=fake_read_user,
                              read_user_dir_function=read_user_dir)
        self.assertEqual(config.get("auto_connect"), True)
        self.assertEqual(config.get("auto_cycle"), None)
        fake_read_user = lambda _: {"auto_connect": False, "auto_cycle": True}
        config = SimpleConfig(options=self.options,
                              read_user_config_function=fake_read_user,
                              read_user_dir_function=read_user_dir)
        self.assertEqual(config.get("auto_connect"), False)
        self.assertEqual(config.get("auto_cycle"), None)

    def test_simple_config_command_line_overrides_everything(self):
        """Options passed by command line override all other configuration
        sources"""
        fake_read_user = lambda _: {"electrum_path": "b"}
        read_user_dir = lambda : self.user_dir
        config = SimpleConfig(options=self.options,
                              read_user_config_function=fake_read_user,
                              read_user_dir_function=read_user_dir)
        self.assertEqual(self.options.get("electrum_path"),
                         config.get("electrum_path"))

    def test_simple_config_user_config_is_used_if_others_arent_specified(self):
        """If no system-wide configuration and no command-line options are
        specified, the user configuration is used instead."""
        fake_read_user = lambda _: {"electrum_path": self.electrum_dir}
        read_user_dir = lambda : self.user_dir
        config = SimpleConfig(options={},
                              read_user_config_function=fake_read_user,
                              read_user_dir_function=read_user_dir)
        self.assertEqual(self.options.get("electrum_path"),
                         config.get("electrum_path"))

    def test_cannot_set_options_passed_by_command_line(self):
        fake_read_user = lambda _: {"electrum_path": "b"}
        read_user_dir = lambda : self.user_dir
        config = SimpleConfig(options=self.options,
                              read_user_config_function=fake_read_user,
                              read_user_dir_function=read_user_dir)
        config.set_key("electrum_path", "c")
        self.assertEqual(self.options.get("electrum_path"),
                         config.get("electrum_path"))

    def test_can_set_options_set_in_user_config(self):
        another_path = tempfile.mkdtemp()
        fake_read_user = lambda _: {"electrum_path": self.electrum_dir}
        read_user_dir = lambda : self.user_dir
        config = SimpleConfig(options={},
                              read_user_config_function=fake_read_user,
                              read_user_dir_function=read_user_dir)
        config.set_key("electrum_path", another_path)
        self.assertEqual(another_path, config.get("electrum_path"))

    def test_user_config_is_not_written_with_read_only_config(self):
        """The user config does not contain command-line options when saved."""
        fake_read_user = lambda _: {"something": "a"}
        read_user_dir = lambda : self.user_dir
        self.options.update({"something": "c"})
        config = SimpleConfig(options=self.options,
                              read_user_config_function=fake_read_user,
                              read_user_dir_function=read_user_dir)
        config.save_user_config()
        contents = None
        with open(os.path.join(self.electrum_dir, "config"), "r") as f:
            contents = f.read()
        result = ast.literal_eval(contents)
        result.pop('config_version', None)
        self.assertEqual({"something": "a"}, result)

    def test_depth_target_to_fee(self):
        config = SimpleConfig(self.options)
        config.mempool_fees = [[49, 100110], [10, 121301], [6, 153731], [5, 125872], [1, 36488810]]
        self.assertEqual( 2 * 1000, config.depth_target_to_fee(1000000))
        self.assertEqual( 6 * 1000, config.depth_target_to_fee( 500000))
        self.assertEqual( 7 * 1000, config.depth_target_to_fee( 250000))
        self.assertEqual(11 * 1000, config.depth_target_to_fee( 200000))
        self.assertEqual(50 * 1000, config.depth_target_to_fee( 100000))
        config.mempool_fees = []
        self.assertEqual( 1 * 1000, config.depth_target_to_fee(10 ** 5))
        self.assertEqual( 1 * 1000, config.depth_target_to_fee(10 ** 6))
        self.assertEqual( 1 * 1000, config.depth_target_to_fee(10 ** 7))
        config.mempool_fees = [[1, 36488810]]
        self.assertEqual( 2 * 1000, config.depth_target_to_fee(10 ** 5))
        self.assertEqual( 2 * 1000, config.depth_target_to_fee(10 ** 6))
        self.assertEqual( 2 * 1000, config.depth_target_to_fee(10 ** 7))
        self.assertEqual( 1 * 1000, config.depth_target_to_fee(10 ** 8))
        config.mempool_fees = [[5, 125872], [1, 36488810]]
        self.assertEqual( 6 * 1000, config.depth_target_to_fee(10 ** 5))
        self.assertEqual( 2 * 1000, config.depth_target_to_fee(10 ** 6))
        self.assertEqual( 2 * 1000, config.depth_target_to_fee(10 ** 7))
        self.assertEqual( 1 * 1000, config.depth_target_to_fee(10 ** 8))
        config.mempool_fees = []
        self.assertEqual(1 * 1000, config.depth_target_to_fee(10 ** 5))
        config.mempool_fees = None
        self.assertEqual(None, config.depth_target_to_fee(10 ** 5))

    def test_fee_to_depth(self):
        config = SimpleConfig(self.options)
        config.mempool_fees = [[49, 100000], [10, 120000], [6, 150000], [5, 125000], [1, 36000000]]
        self.assertEqual(100000, config.fee_to_depth(500))
        self.assertEqual(100000, config.fee_to_depth(50))
        self.assertEqual(100000, config.fee_to_depth(49))
        self.assertEqual(220000, config.fee_to_depth(48))
        self.assertEqual(220000, config.fee_to_depth(10))
        self.assertEqual(370000, config.fee_to_depth(9))
        self.assertEqual(370000, config.fee_to_depth(6.5))
        self.assertEqual(370000, config.fee_to_depth(6))
        self.assertEqual(495000, config.fee_to_depth(5.5))
        self.assertEqual(36495000, config.fee_to_depth(0.5))


class TestUserConfig(ElectrumTestCase):

    def setUp(self):
        super(TestUserConfig, self).setUp()
        self._saved_stdout = sys.stdout
        self._stdout_buffer = StringIO()
        sys.stdout = self._stdout_buffer

        self.user_dir = tempfile.mkdtemp()

    def tearDown(self):
        super(TestUserConfig, self).tearDown()
        shutil.rmtree(self.user_dir)
        sys.stdout = self._saved_stdout

    def test_no_path_means_no_result(self):
       result = read_user_config(None)
       self.assertEqual({}, result)

    def test_path_without_config_file(self):
        """We pass a path but if does not contain a "config" file."""
        result = read_user_config(self.user_dir)
        self.assertEqual({}, result)

    def test_path_with_reprd_object(self):

        class something(object):
            pass

        thefile = os.path.join(self.user_dir, "config")
        payload = something()
        with open(thefile, "w") as f:
            f.write(repr(payload))

        result = read_user_config(self.user_dir)
        self.assertEqual({}, result)


class Test_FeeFloor(ElectrumTestCase):
    """Doichain refuses to relay below DEFAULT_MIN_RELAY_TX_FEE = COIN/1000
    (100000 sat/kvB). The public servers lower that limit, the mining node does
    not -- so anything cheaper is relayed, looks sent, and is never mined. No
    rate this wallet can produce may fall below it. See #23.
    """

    def setUp(self):
        super(Test_FeeFloor, self).setUp()
        self.electrum_dir = tempfile.mkdtemp()
        self.options = {"electrum_path": self.electrum_dir}

    def tearDown(self):
        super(Test_FeeFloor, self).tearDown()
        shutil.rmtree(self.electrum_dir)

    def test_every_slider_step_is_mineable(self):
        for i, rate in enumerate(FEERATE_STATIC_VALUES):
            self.assertGreaterEqual(rate, FEERATE_MIN_MINEABLE,
                                    msg=f"slider step {i} is below the floor")
        self.assertEqual(FEERATE_MIN_MINEABLE, FEERATE_STATIC_VALUES[0])
        self.assertEqual(sorted(FEERATE_STATIC_VALUES), FEERATE_STATIC_VALUES)
        self.assertEqual(len(set(FEERATE_STATIC_VALUES)), len(FEERATE_STATIC_VALUES))

    def test_fallback_rate_sits_on_a_slider_step(self):
        # otherwise the slider jumps the moment the dialog is opened
        self.assertIn(FEERATE_FALLBACK_STATIC_FEE, FEERATE_STATIC_VALUES)
        self.assertGreaterEqual(FEERATE_FALLBACK_STATIC_FEE, FEERATE_MIN_MINEABLE)

    def test_high_fee_warning_means_something(self):
        # it should flag the top of the scale, not half of it
        n = sum(1 for r in FEERATE_STATIC_VALUES if r > FEERATE_WARNING_HIGH_FEE)
        self.assertGreater(n, 0)
        self.assertLessEqual(n, 3)

    def test_every_slider_position_is_mineable(self):
        config = SimpleConfig(self.options)
        for k in range(21):
            rate = config.fee_per_kb(dyn=False, mempool=False, fee_level=k / 20)
            self.assertGreaterEqual(rate, FEERATE_MIN_MINEABLE)

    def test_rate_saved_by_an_older_version_is_raised(self):
        # wallets in the field carry a 'fee_per_kb' from the old scale, which
        # started at 1000. Reading it back verbatim would put the slider on its
        # lowest step while still paying the old, unmineable rate.
        config = SimpleConfig({**self.options, "dynamic_fees": False, "fee_per_kb": 1000})
        self.assertEqual(FEERATE_MIN_MINEABLE, config.fee_per_kb(dyn=False, mempool=False))
        maxp, pos, rate = config.get_fee_slider(dyn=False, mempool=False)
        self.assertEqual(0, pos)
        self.assertEqual(FEERATE_MIN_MINEABLE, rate)

    def test_dust_limit_does_not_follow_the_server(self):
        # Core derives dust from DUST_RELAY_TX_FEE (3000), which Doichain left
        # alone; deriving it from the relay fee a server reports gave a limit a
        # hundred times too high as soon as a server reported its real floor.
        class _Net:
            relay_fee = FEERATE_MIN_MINEABLE
        self.assertEqual(bitcoin.DUST_LIMIT_DEFAULT_SAT_LEGACY, bitcoin.dust_threshold())
        self.assertEqual(bitcoin.dust_threshold(), bitcoin.dust_threshold(_Net()))

    def test_honest_server_relay_fee_survives_the_sanity_cap(self):
        class _Net:
            relay_fee = FEERATE_MIN_MINEABLE
        self.assertEqual(FEERATE_MIN_MINEABLE, bitcoin.relayfee(_Net()))
