import json
import os

from electrum import blockchain, constants
from electrum.blockchain import (Blockchain, compact_to_target, target_to_compact, derive_target,
                                 get_next_work_required, deserialize_full_header, hash_header)
from electrum.simple_config import SimpleConfig
from electrum.util import bfh, make_dir

from . import ElectrumTestCase


with open(os.path.join(os.path.dirname(__file__), 'doichain_headers.json')) as f:
    FIXTURE = json.load(f)

RULE_HEADERS = {int(height): {'block_height': int(height), 'timestamp': timestamp, 'bits': bits}
                for height, (timestamp, bits) in FIXTURE['rules'].items()}


def read_rule_header(height: int) -> dict:
    return RULE_HEADERS[height]


def work(target: int) -> int:
    return ((2 ** 256 - target - 1) // (target + 1)) + 1


class TestCompact(ElectrumTestCase):

    def test_set_and_get_compact(self):
        self.assertEqual((0xffff << 224, False, False), compact_to_target(0x1f00ffff))
        self.assertEqual(0x1f00ffff, target_to_compact(0xffff << 224))
        # vectors from Bitcoin Core's arith_uint256 tests
        self.assertEqual((0x12, False, False), compact_to_target(0x01123456))
        self.assertEqual(0x01120000, target_to_compact(0x12))
        self.assertTrue(compact_to_target(0x04923456)[1])  # negative
        self.assertTrue(compact_to_target(0xff123456)[2])  # overflow

    def test_derive_target(self):
        self.assertEqual(0xffff << 224, derive_target(0x1f00ffff))
        self.assertIsNone(derive_target(0x2000ffff))  # easier than powLimit
        self.assertIsNone(derive_target(0x1a000000))  # zero
        self.assertIsNone(derive_target(0x04923456))  # negative


class TestDifficultyRules(ElectrumTestCase):
    """The rules against real Doichain mainnet headers."""

    def assert_rule_matches_chain(self, height: int):
        header = read_rule_header(height)
        self.assertEqual(hex(header['bits']), hex(get_next_work_required(header, read_rule_header)), height)

    def test_all_fixture_heights(self):
        for height in FIXTURE['rule_heights']:
            self.assert_rule_matches_chain(height)

    def test_first_retarget_wraps_like_arith_uint256(self):
        # 0x1f00ffff times a timespan of about 50 days exceeds 256 bits. Core's
        # arith_uint256 wraps, and the chain carries the wrapped result.
        self.assertEqual(0x1e063102, read_rule_header(2016)['bits'])
        self.assert_rule_matches_chain(2016)

    def test_retarget_at_lower_clamp(self):
        self.assert_rule_matches_chain(4032)

    def test_retarget_spans_2016_intervals(self):
        self.assert_rule_matches_chain(229824)

    def test_bits_constant_within_period(self):
        self.assert_rule_matches_chain(431016)

    def test_reset_window(self):
        # 431017 and 431018 came more than an hour after their parents: valve
        self.assertEqual(0x1a100334, read_rule_header(431017)['bits'])
        self.assert_rule_matches_chain(431017)
        self.assert_rule_matches_chain(431018)
        self.assertEqual(0x1a0400cd, read_rule_header(431019)['bits'])
        self.assert_rule_matches_chain(431019)

    def test_digishield(self):
        self.assert_rule_matches_chain(431045)  # first block averaged over the window
        self.assert_rule_matches_chain(431423)

    def test_valve(self):
        header = dict(read_rule_header(431019))
        header['timestamp'] = read_rule_header(431018)['timestamp'] + 3601
        self.assertEqual(0x1a100334, get_next_work_required(header, read_rule_header))


class TestChainAboveCheckpoint(ElectrumTestCase):
    """Real headers with AuxPoW directly after the last checkpoint."""

    def setUp(self):
        super().setUp()
        make_dir(os.path.join(self.electrum_path, 'forks'))
        self.config = SimpleConfig({'electrum_path': self.electrum_path})
        blockchain.blockchains = {}
        blockchain.read_blockchains(self.config)
        blockchain.init_headers_file_for_best_chain()
        self.chain = blockchain.get_best_chain()
        self.headers = [deserialize_full_header(bfh(raw), FIXTURE['chain_start'] + i)
                        for i, raw in enumerate(FIXTURE['chain'])]

    def test_tail_headers_chain_up_to_the_checkpoint(self):
        tail = blockchain.checkpoint_tail_headers()
        self.assertEqual(28, len(tail))
        self.assertEqual(constants.net.CHECKPOINTS[-1][0], hash_header(tail[constants.net.max_checkpoint()]))

    def test_connects_real_headers(self):
        self.assertEqual(constants.net.max_checkpoint(), self.chain.height())
        for header in self.headers:
            self.assertTrue(self.chain.can_connect(header), header['block_height'])
            self.chain.save_header(header)
        self.assertEqual(self.headers[-1]['block_height'], self.chain.height())

    def test_rejects_wrong_bits(self):
        header = dict(self.headers[0])
        header['bits'] += 1
        self.assertFalse(self.chain.can_connect(header))
        with self.assertRaisesRegex(Exception, 'bits mismatch'):
            Blockchain.verify_header(header, header['prev_block_hash'], self.chain.get_expected_target(header))

    def test_rejects_insufficient_proof_of_work(self):
        header = dict(self.headers[0])
        header['auxpow'] = dict(header['auxpow'])
        parent = dict(header['auxpow']['parent_header'])
        parent['nonce'] ^= 1
        header['auxpow']['parent_header'] = parent
        self.assertFalse(self.chain.can_connect(header))
        with self.assertRaisesRegex(Exception, 'insufficient proof of work'):
            Blockchain.verify_header(header, header['prev_block_hash'], self.chain.get_expected_target(header))

    def test_connects_chunk(self):
        index = FIXTURE['chain_start'] // 2016
        self.assertEqual(FIXTURE['chain_start'], index * 2016)
        self.assertTrue(self.chain.connect_chunk(index, ''.join(FIXTURE['chain'])))
        self.assertEqual(self.headers[-1]['block_height'], self.chain.height())

    def test_rejects_chunk_with_wrong_bits(self):
        index = FIXTURE['chain_start'] // 2016
        tampered = bytearray(bfh(FIXTURE['chain'][2]))
        tampered[72] ^= 1  # lowest byte of bits
        chunk = FIXTURE['chain'][0] + FIXTURE['chain'][1] + tampered.hex() + ''.join(FIXTURE['chain'][3:])
        self.assertFalse(self.chain.connect_chunk(index, chunk))
        self.assertEqual(constants.net.max_checkpoint(), self.chain.height())

    def test_restart_keeps_the_chain(self):
        # headers are stored without AuxPoW; the startup check must not need it
        for header in self.headers:
            self.chain.save_header(header)
        blockchain.blockchains = {}
        blockchain.read_blockchains(self.config)
        self.assertEqual(self.headers[-1]['block_height'], blockchain.get_best_chain().height())

    def test_chainwork_counts_every_header_above_checkpoint(self):
        for header in self.headers:
            self.chain.save_header(header)
        expected = sum(work(derive_target(header['bits'])) for header in self.headers)
        self.assertEqual(expected, self.chain.get_chainwork() - self.chain.get_chainwork(constants.net.max_checkpoint()))
