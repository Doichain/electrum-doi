# Electrum-DOI -lightweight Doichain client
# Copyright (C) 2012 thomasv@ecdsa.org
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import os
import threading
import time
from typing import Optional, Dict, Mapping, Sequence, Callable, Tuple

from . import util
from .bitcoin import hash_encode, int_to_hex, rev_hex
from .crypto import sha256d
from . import constants
from .util import bfh, bh2u, with_lock
from .simple_config import SimpleConfig
from .logging import get_logger, Logger

from . import auxpow

_logger = get_logger(__name__)

HEADER_SIZE = 80  # bytes
UINT256_MASK = (1 << 256) - 1
MEDIAN_TIME_SPAN = 11  # CBlockIndex::nMedianTimeSpan in Doichain Core


class MissingHeader(Exception):
    pass

class InvalidHeader(Exception):
    pass

def serialize_header(header_dict: dict) -> str:
    s = int_to_hex(header_dict['version'], 4) \
        + rev_hex(header_dict['prev_block_hash']) \
        + rev_hex(header_dict['merkle_root']) \
        + int_to_hex(int(header_dict['timestamp']), 4) \
        + int_to_hex(int(header_dict['bits']), 4) \
        + int_to_hex(int(header_dict['nonce']), 4)
    return s

def deserialize_pure_header(s: bytes, height: int) -> dict:
    if not s:
        raise InvalidHeader('Invalid header: {}'.format(s))
    if len(s) != HEADER_SIZE:
        raise InvalidHeader('Invalid header length: {}'.format(len(s)))
    hex_to_int = lambda s: int.from_bytes(s, byteorder='little')
    h = {}
    h['version'] = hex_to_int(s[0:4])
    h['prev_block_hash'] = hash_encode(s[4:36])
    h['merkle_root'] = hash_encode(s[36:68])
    h['timestamp'] = hex_to_int(s[68:72])
    h['bits'] = hex_to_int(s[72:76])
    h['nonce'] = hex_to_int(s[76:80])
    h['block_height'] = height
    return h

def deserialize_full_header(s: bytes, height: int, expect_trailing_data=False, start_position=0):
    """Deserialises a full block header which may include AuxPoW.

    If expect_trailing_data is true, then we allow trailing data and return
    the end position in the byte array alongside the header dict.  Otherwise
    an error is raised if there is trailing, unconsumed data."""

    original_start = start_position

    pure_header_bytes = s[start_position : start_position + HEADER_SIZE]
    h = deserialize_pure_header(pure_header_bytes, height)
    start_position += HEADER_SIZE

    if auxpow.auxpow_active(h) and height > constants.net.max_checkpoint():
        h['auxpow'], start_position = auxpow.deserialize_auxpow_header(h, s, start_position=start_position)

    if expect_trailing_data:
        return h, start_position

    if start_position != len(s):
        raise Exception('Invalid header length: {}'.format(len(s) - original_start))
    return h

def hash_header(header: dict) -> str:
    if header is None:
        return '0' * 64
    if header.get('prev_block_hash') is None:
        header['prev_block_hash'] = '00'*32
    return hash_raw_header(serialize_header(header))


def hash_raw_header(header: str) -> str:
    return hash_encode(sha256d(bfh(header)))


# Doichain's difficulty rules, as Doichain Core implements them (src/pow.cpp):
# up to DIGISHIELD_HEIGHT - 1 a retarget every 2016 blocks, from DIGISHIELD_HEIGHT
# on DigiShield-v3 per block, with a one-time reset window and a bounded
# emergency valve. Core computes with arith_uint256, so multiplications below
# wrap modulo 2**256 and divisions truncate, exactly as there.

def compact_to_target(bits: int) -> Tuple[int, bool, bool]:
    """arith_uint256::SetCompact. Returns (target, negative, overflow)."""
    size = bits >> 24
    word = bits & 0x007fffff
    if size <= 3:
        target = word >> (8 * (3 - size))
    else:
        target = (word << (8 * (size - 3))) & UINT256_MASK
    negative = word != 0 and (bits & 0x00800000) != 0
    overflow = word != 0 and (size > 34 or (word > 0xff and size > 33) or (word > 0xffff and size > 32))
    return target, negative, overflow


def target_to_compact(target: int) -> int:
    """arith_uint256::GetCompact for a non-negative target."""
    size = (target.bit_length() + 7) // 8
    if size <= 3:
        compact = target << (8 * (3 - size))
    else:
        compact = target >> (8 * (size - 3))
    if compact & 0x00800000:
        compact >>= 8
        size += 1
    return compact | (size << 24)


def derive_target(bits: int) -> Optional[int]:
    """Doichain Core's DeriveTarget: the target bits encodes, or None if it is
    negative, zero, overflowing or easier than powLimit."""
    target, negative, overflow = compact_to_target(bits)
    if negative or overflow or target == 0 or target > constants.net.POW_LIMIT:
        return None
    return target


def _div_trunc(a: int, b: int) -> int:
    """C++ integer division, which truncates towards zero."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def _median_time_past(height: int, read_header: Callable[[int], dict]) -> int:
    """CBlockIndex::GetMedianTimePast of the block at height."""
    first = max(0, height - MEDIAN_TIME_SPAN + 1)
    times = sorted(read_header(h)['timestamp'] for h in range(first, height + 1))
    return times[len(times) // 2]


def get_next_work_required(header: dict, read_header: Callable[[int], dict]) -> int:
    """The nBits Doichain Core's GetNextWorkRequired demands for header.
    read_header(h) must return the header at every height h below it."""
    net = constants.net
    height = header['block_height']
    prev = read_header(height - 1)
    if height >= net.DIGISHIELD_HEIGHT:
        return _get_next_work_required_digishield(header, prev, read_header)
    if height % 2016 != 0:
        return prev['bits']
    # Namecoin's retarget spans a full 2016 intervals, except at the first boundary.
    blocks_back = 2016 if (height - 1 >= net.AUXPOW_START_HEIGHT and height > 2016) else 2015
    first = read_header(height - 1 - blocks_back)
    timespan = prev['timestamp'] - first['timestamp']
    timespan = max(timespan, net.POW_TARGET_TIMESPAN // 4)
    timespan = min(timespan, net.POW_TARGET_TIMESPAN * 4)
    target, _, _ = compact_to_target(prev['bits'])
    # Close to Doichain's easy powLimit this product exceeds 256 bits. That
    # happened at the first retarget (height 2016), and the chain carries the
    # wrapped value, so the overflow is part of the rule.
    target = (target * timespan) & UINT256_MASK
    target //= net.POW_TARGET_TIMESPAN
    return target_to_compact(min(target, net.POW_LIMIT))


def _get_next_work_required_digishield(header: dict, prev: dict,
                                       read_header: Callable[[int], dict]) -> int:
    net = constants.net
    height = header['block_height']
    window = net.DIGISHIELD_AVERAGING_WINDOW
    if net.DIGISHIELD_RESET_BITS and height < net.DIGISHIELD_HEIGHT + window + MEDIAN_TIME_SPAN:
        # one-time reset window after activation
        target, _, _ = compact_to_target(net.DIGISHIELD_RESET_BITS)
    else:
        first_height = height - 1 - window
        if first_height < 0:
            return target_to_compact(net.POW_LIMIT)
        total = 0
        for h in range(height - window, height):
            total += compact_to_target(read_header(h)['bits'])[0]
        window_timespan = window * net.POW_TARGET_SPACING
        timespan = _median_time_past(height - 1, read_header) - _median_time_past(first_height, read_header)
        timespan = window_timespan + _div_trunc(timespan - window_timespan, 4)
        timespan = max(timespan, window_timespan * (100 - net.DIGISHIELD_MAX_ADJUST_UP) // 100)
        timespan = min(timespan, window_timespan * (100 + net.DIGISHIELD_MAX_ADJUST_DOWN) // 100)
        target = total // window // window_timespan
        target = min((target * timespan) & UINT256_MASK, net.POW_LIMIT)
        target, _, _ = compact_to_target(target_to_compact(target))
    # emergency valve: a block more than MIN_DIFFICULTY_GAP seconds after its
    # parent may be MIN_DIFFICULTY_VALVE_FACTOR times easier
    if (net.MIN_DIFFICULTY_GAP > 0 and net.MIN_DIFFICULTY_VALVE_FACTOR > 1
            and header['timestamp'] > prev['timestamp'] + net.MIN_DIFFICULTY_GAP):
        target = (target * net.MIN_DIFFICULTY_VALVE_FACTOR) & UINT256_MASK
    return target_to_compact(min(target, net.POW_LIMIT))


# net -> {height: header}
_CHECKPOINT_TAIL_HEADERS = {}  # type: Dict[type, Dict[int, dict]]


def _load_checkpoint_tail_headers(net) -> Dict[int, dict]:
    tail = net.CHECKPOINT_TAIL_HEADERS
    if not tail or not net.CHECKPOINTS:
        return {}
    headers = {}
    height = tail['height']
    prev_hash = None
    for raw in tail['headers']:
        header = deserialize_pure_header(bfh(raw), height)
        if prev_hash is not None and header['prev_block_hash'] != prev_hash:
            _logger.error("checkpoint tail headers do not chain up; ignoring them")
            return {}
        headers[height] = header
        prev_hash = hash_header(header)
        height += 1
    if height - 1 != net.max_checkpoint() or prev_hash != net.CHECKPOINTS[-1][0]:
        _logger.error("checkpoint tail headers do not end at the last checkpoint; ignoring them")
        return {}
    return headers


def checkpoint_tail_headers() -> Dict[int, dict]:
    """The headers just below the last checkpoint that ship with the release,
    by height. The difficulty rules of the first headers after the checkpoint
    look back at them, while headers below the checkpoint are only downloaded
    on demand. Empty unless they chain up to the checkpoint hash."""
    net = constants.net
    if net not in _CHECKPOINT_TAIL_HEADERS:
        _CHECKPOINT_TAIL_HEADERS[net] = _load_checkpoint_tail_headers(net)
    return _CHECKPOINT_TAIL_HEADERS[net]


# key: blockhash hex at forkpoint
# the chain at some key is the best chain that includes the given hash
blockchains = {}  # type: Dict[str, Blockchain]
blockchains_lock = threading.RLock()  # lock order: take this last; so after Blockchain.lock


def read_blockchains(config: 'SimpleConfig'):
    best_chain = Blockchain(config=config,
                            forkpoint=0,
                            parent=None,
                            forkpoint_hash=constants.net.GENESIS,
                            prev_hash=None)
    blockchains[constants.net.GENESIS] = best_chain
    # consistency checks
    if best_chain.height() > constants.net.max_checkpoint():
        header_after_cp = best_chain.read_header(constants.net.max_checkpoint()+1)
        # headers on disk are stored without their AuxPoW, so it cannot be checked here
        if not header_after_cp or not best_chain.can_connect(header_after_cp, check_height=False, skip_auxpow=True):
            _logger.info("[blockchain] deleting best chain. cannot connect header after last cp to last cp.")
            os.unlink(best_chain.path())
            best_chain.update_size()
    # forks
    fdir = os.path.join(util.get_headers_dir(config), 'forks')
    util.make_dir(fdir)
    # files are named as: fork2_{forkpoint}_{prev_hash}_{first_hash}
    l = filter(lambda x: x.startswith('fork2_') and '.' not in x, os.listdir(fdir))
    l = sorted(l, key=lambda x: int(x.split('_')[1]))  # sort by forkpoint

    def delete_chain(filename, reason):
        _logger.info(f"[blockchain] deleting chain {filename}: {reason}")
        os.unlink(os.path.join(fdir, filename))

    def instantiate_chain(filename):
        __, forkpoint, prev_hash, first_hash = filename.split('_')
        forkpoint = int(forkpoint)
        prev_hash = (64-len(prev_hash)) * "0" + prev_hash  # left-pad with zeroes
        first_hash = (64-len(first_hash)) * "0" + first_hash
        # forks below the max checkpoint are not allowed
        if forkpoint <= constants.net.max_checkpoint():
            delete_chain(filename, "deleting fork below max checkpoint")
            return
        # find parent (sorting by forkpoint guarantees it's already instantiated)
        for parent in blockchains.values():
            if parent.check_hash(forkpoint - 1, prev_hash):
                break
        else:
            delete_chain(filename, "cannot find parent for chain")
            return
        b = Blockchain(config=config,
                       forkpoint=forkpoint,
                       parent=parent,
                       forkpoint_hash=first_hash,
                       prev_hash=prev_hash)
        # consistency checks
        h = b.read_header(b.forkpoint)
        if first_hash != hash_header(h):
            delete_chain(filename, "incorrect first hash for chain")
            return
        if not b.parent.can_connect(h, check_height=False, skip_auxpow=True):
            delete_chain(filename, "cannot connect chain to parent")
            return
        chain_id = b.get_id()
        assert first_hash == chain_id, (first_hash, chain_id)
        blockchains[chain_id] = b

    for filename in l:
        instantiate_chain(filename)


def get_best_chain() -> 'Blockchain':
    return blockchains[constants.net.GENESIS]

# block hash -> chain work; up to and including that block
_CHAINWORK_CACHE = {
    "0000000000000000000000000000000000000000000000000000000000000000": 0,  # virtual block at height -1
}  # type: Dict[str, int]


def init_headers_file_for_best_chain():
    b = get_best_chain()
    filename = b.path()
    length = HEADER_SIZE * len(constants.net.CHECKPOINTS) * 2016
    if not os.path.exists(filename) or os.path.getsize(filename) < length:
        with open(filename, 'wb') as f:
            if length > 0:
                f.seek(length - 1)
                f.write(b'\x00')
        util.ensure_sparse_file(filename)
    with b.lock:
        b.update_size()


class Blockchain(Logger):
    """
    Manages blockchain headers and their verification
    """

    def __init__(self, config: SimpleConfig, forkpoint: int, parent: Optional['Blockchain'],
                 forkpoint_hash: str, prev_hash: Optional[str]):
        assert isinstance(forkpoint_hash, str) and len(forkpoint_hash) == 64, forkpoint_hash
        assert (prev_hash is None) or (isinstance(prev_hash, str) and len(prev_hash) == 64), prev_hash
        # assert (parent is None) == (forkpoint == 0)
        if 0 < forkpoint <= constants.net.max_checkpoint():
            raise Exception(f"cannot fork below max checkpoint. forkpoint: {forkpoint}")
        Logger.__init__(self)
        self.config = config
        self.forkpoint = forkpoint  # height of first header
        self.parent = parent
        self._forkpoint_hash = forkpoint_hash  # blockhash at forkpoint. "first hash"
        self._prev_hash = prev_hash  # blockhash immediately before forkpoint
        self.lock = threading.RLock()
        self.update_size()

    @property
    def checkpoints(self):
        return constants.net.CHECKPOINTS

    def get_max_child(self) -> Optional[int]:
        children = self.get_direct_children()
        return max([x.forkpoint for x in children]) if children else None

    def get_max_forkpoint(self) -> int:
        """Returns the max height where there is a fork
        related to this chain.
        """
        mc = self.get_max_child()
        return mc if mc is not None else self.forkpoint

    def get_direct_children(self) -> Sequence['Blockchain']:
        with blockchains_lock:
            return list(filter(lambda y: y.parent==self, blockchains.values()))

    def get_parent_heights(self) -> Mapping['Blockchain', int]:
        """Returns map: (parent chain -> height of last common block)"""
        with self.lock, blockchains_lock:
            result = {self: self.height()}
            chain = self
            while True:
                parent = chain.parent
                if parent is None: break
                result[parent] = chain.forkpoint - 1
                chain = parent
            return result

    def get_height_of_last_common_block_with_chain(self, other_chain: 'Blockchain') -> int:
        last_common_block_height = 0
        our_parents = self.get_parent_heights()
        their_parents = other_chain.get_parent_heights()
        for chain in our_parents:
            if chain in their_parents:
                h = min(our_parents[chain], their_parents[chain])
                last_common_block_height = max(last_common_block_height, h)
        return last_common_block_height

    @with_lock
    def get_branch_size(self) -> int:
        return self.height() - self.get_max_forkpoint() + 1

    def get_name(self) -> str:
        return self.get_hash(self.get_max_forkpoint()).lstrip('0')[0:10]

    def check_header(self, header: dict) -> bool:
        header_hash = hash_header(header)
        height = header.get('block_height')
        return self.check_hash(height, header_hash)

    def check_hash(self, height: int, header_hash: str) -> bool:
        """Returns whether the hash of the block at given height
        is the given hash.
        """
        assert isinstance(header_hash, str) and len(header_hash) == 64, header_hash  # hex
        try:
            return header_hash == self.get_hash(height)
        except Exception:
            return False

    def fork(parent, header: dict) -> 'Blockchain':
        if not parent.can_connect(header, check_height=False):
            raise Exception("forking header does not connect to parent chain")
        forkpoint = header.get('block_height')
        self = Blockchain(config=parent.config,
                          forkpoint=forkpoint,
                          parent=parent,
                          forkpoint_hash=hash_header(header),
                          prev_hash=parent.get_hash(forkpoint-1))
        self.assert_headers_file_available(parent.path())
        open(self.path(), 'w+').close()
        self.save_header(header)
        # put into global dict. note that in some cases
        # save_header might have already put it there but that's OK
        chain_id = self.get_id()
        with blockchains_lock:
            blockchains[chain_id] = self
        return self

    @with_lock
    def height(self) -> int:
        return self.forkpoint + self.size() - 1

    @with_lock
    def size(self) -> int:
        return self._size

    @with_lock
    def update_size(self) -> None:
        p = self.path()
        self._size = os.path.getsize(p)//HEADER_SIZE if os.path.exists(p) else 0

    @classmethod
    def verify_header(cls, header: dict, prev_hash: str, target: Optional[int], expected_header_hash: str=None, skip_auxpow: bool=False) -> None:
        """Raises unless header links to prev_hash, carries the bits of target
        (skipped if target is None) and has enough proof of work for its bits.
        Proof of work is not checked at or below the last checkpoint, where
        the header is pinned by its hash."""
        _hash = hash_header(header)
        if expected_header_hash and expected_header_hash != _hash:
            raise Exception("hash mismatches with expected: {} vs {}".format(expected_header_hash, _hash))
        if prev_hash != header.get('prev_block_hash'):
            raise Exception("prev hash mismatch: %s vs %s" % (prev_hash, header.get('prev_block_hash')))
        if constants.net.TESTNET:
            return
        bits = header.get('bits')
        if target is not None:
            expected_bits = target_to_compact(target)
            if bits != expected_bits:
                raise Exception("bits mismatch: %s vs %s" % (hex(expected_bits), hex(bits)))
        # Don't verify AuxPoW when covered by a checkpoint
        if header.get('block_height') <= constants.net.max_checkpoint():
            skip_auxpow = True
        if not skip_auxpow:
            pow_target = derive_target(bits)
            if pow_target is None:
                raise Exception(f"bits out of range: {hex(bits)}")
            _pow_hash = auxpow.hash_parent_header(header)
            block_hash_as_num = int.from_bytes(bfh(_pow_hash), byteorder='big')
            if block_hash_as_num > pow_target:
                raise Exception(f"insufficient proof of work: {block_hash_as_num} vs target {pow_target}")

    def read_header_for_rules(self, height: int, pending: Optional[Dict[int, dict]] = None) -> dict:
        """The header at height, for the difficulty rules: from pending (headers
        of a chunk being verified), from disk, or from the headers shipped below
        the last checkpoint."""
        header = pending.get(height) if pending else None
        if header is None:
            header = self.read_header(height)
        if header is None:
            header = checkpoint_tail_headers().get(height)
        if header is None:
            raise MissingHeader(height)
        return header

    def get_expected_target(self, header: dict, pending: Optional[Dict[int, dict]] = None) -> Optional[int]:
        """The target Doichain Core requires for header, or None at and below the
        last checkpoint. There every header is pinned by its hash, and headers
        are only downloaded on demand, so their predecessors may be missing."""
        if constants.net.TESTNET or header['block_height'] <= constants.net.max_checkpoint():
            return None
        bits = get_next_work_required(header, lambda h: self.read_header_for_rules(h, pending))
        return compact_to_target(bits)[0]

    def verify_chunk(self, index: int, data: bytes) -> bytes:
        stripped = bytearray()
        start_position = 0
        start_height = index * 2016
        prev_hash = self.get_hash(start_height - 1)
        # the headers of this chunk verified so far; the difficulty rules of the
        # headers after them look back at them
        pending = {}  # type: Dict[int, dict]
        i = 0
        while start_position < len(data):
            height = start_height + i
            try:
                expected_header_hash = self.get_hash(height)
            except MissingHeader:
                expected_header_hash = None

            # Strip auxpow header for disk
            stripped.extend(data[start_position:start_position+HEADER_SIZE])

            header, start_position = deserialize_full_header(data, index*2016 + i, expect_trailing_data=True, start_position=start_position)
            target = self.get_expected_target(header, pending)
            self.verify_header(header, prev_hash, target, expected_header_hash)
            pending[height] = header
            prev_hash = hash_header(header)

            i = i + 1

        return bytes(stripped)

    @with_lock
    def path(self):
        d = util.get_headers_dir(self.config)
        if self.parent is None:
            filename = 'blockchain_headers'
        else:
            assert self.forkpoint > 0, self.forkpoint
            prev_hash = self._prev_hash.lstrip('0')
            first_hash = self._forkpoint_hash.lstrip('0')
            basename = f'fork2_{self.forkpoint}_{prev_hash}_{first_hash}'
            filename = os.path.join('forks', basename)
        return os.path.join(d, filename)

    @with_lock
    def save_chunk(self, index: int, chunk: bytes):
        assert index >= 0, index
        chunk_within_checkpoint_region = index < len(self.checkpoints)
        # chunks in checkpoint region are the responsibility of the 'main chain'
        if chunk_within_checkpoint_region and self.parent is not None:
            main_chain = get_best_chain()
            main_chain.save_chunk(index, chunk)
            return

        delta_height = (index * 2016 - self.forkpoint)
        delta_bytes = delta_height * HEADER_SIZE
        # if this chunk contains our forkpoint, only save the part after forkpoint
        # (the part before is the responsibility of the parent)
        if delta_bytes < 0:
            chunk = chunk[-delta_bytes:]
            delta_bytes = 0
        truncate = not chunk_within_checkpoint_region
        self.write(chunk, delta_bytes, truncate)
        self.swap_with_parent()

    def swap_with_parent(self) -> None:
        with self.lock, blockchains_lock:
            # do the swap; possibly multiple ones
            cnt = 0
            while True:
                old_parent = self.parent
                if not self._swap_with_parent():
                    break
                # make sure we are making progress
                cnt += 1
                if cnt > len(blockchains):
                    raise Exception(f'swapping fork with parent too many times: {cnt}')
                # we might have become the parent of some of our former siblings
                for old_sibling in old_parent.get_direct_children():
                    if self.check_hash(old_sibling.forkpoint - 1, old_sibling._prev_hash):
                        old_sibling.parent = self

    def _swap_with_parent(self) -> bool:
        """Check if this chain became stronger than its parent, and swap
        the underlying files if so. The Blockchain instances will keep
        'containing' the same headers, but their ids change and so
        they will be stored in different files."""
        if self.parent is None:
            return False
        if self.parent.get_chainwork() >= self.get_chainwork():
            return False
        self.logger.info(f"swapping {self.forkpoint} {self.parent.forkpoint}")
        parent_branch_size = self.parent.height() - self.forkpoint + 1
        forkpoint = self.forkpoint  # type: Optional[int]
        parent = self.parent  # type: Optional[Blockchain]
        child_old_id = self.get_id()
        parent_old_id = parent.get_id()
        # swap files
        # child takes parent's name
        # parent's new name will be something new (not child's old name)
        self.assert_headers_file_available(self.path())
        child_old_name = self.path()
        with open(self.path(), 'rb') as f:
            my_data = f.read()
        self.assert_headers_file_available(parent.path())
        assert forkpoint > parent.forkpoint, (f"forkpoint of parent chain ({parent.forkpoint}) "
                                              f"should be at lower height than children's ({forkpoint})")
        with open(parent.path(), 'rb') as f:
            f.seek((forkpoint - parent.forkpoint)*HEADER_SIZE)
            parent_data = f.read(parent_branch_size*HEADER_SIZE)
        self.write(parent_data, 0)
        parent.write(my_data, (forkpoint - parent.forkpoint)*HEADER_SIZE)
        # swap parameters
        self.parent, parent.parent = parent.parent, self  # type: Optional[Blockchain], Optional[Blockchain]
        self.forkpoint, parent.forkpoint = parent.forkpoint, self.forkpoint
        self._forkpoint_hash, parent._forkpoint_hash = parent._forkpoint_hash, hash_raw_header(bh2u(parent_data[:HEADER_SIZE]))
        self._prev_hash, parent._prev_hash = parent._prev_hash, self._prev_hash
        # parent's new name
        os.replace(child_old_name, parent.path())
        self.update_size()
        parent.update_size()
        # update pointers
        blockchains.pop(child_old_id, None)
        blockchains.pop(parent_old_id, None)
        blockchains[self.get_id()] = self
        blockchains[parent.get_id()] = parent
        return True

    def get_id(self) -> str:
        return self._forkpoint_hash

    def assert_headers_file_available(self, path):
        if os.path.exists(path):
            return
        elif not os.path.exists(util.get_headers_dir(self.config)):
            raise FileNotFoundError('Electrum headers_dir does not exist. Was it deleted while running?')
        else:
            raise FileNotFoundError('Cannot find headers file but headers_dir is there. Should be at {}'.format(path))

    @with_lock
    def write(self, data: bytes, offset: int, truncate: bool=True) -> None:
        filename = self.path()
        self.assert_headers_file_available(filename)
        with open(filename, 'rb+') as f:
            if truncate and offset != self._size * HEADER_SIZE:
                f.seek(offset)
                f.truncate()
            f.seek(offset)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        self.update_size()

    @with_lock
    def save_header(self, header: dict) -> None:
        delta = header.get('block_height') - self.forkpoint
        data = bfh(serialize_header(header))
        # headers are only _appended_ to the end:
        assert delta == self.size(), (delta, self.size())
        assert len(data) == HEADER_SIZE
        self.write(data, delta*HEADER_SIZE)
        self.swap_with_parent()

    @with_lock
    def read_header(self, height: int) -> Optional[dict]:
        if height < 0:
            return
        if height < self.forkpoint:
            return self.parent.read_header(height)
        if height > self.height():
            return
        delta = height - self.forkpoint
        name = self.path()
        self.assert_headers_file_available(name)
        with open(name, 'rb') as f:
            f.seek(delta * HEADER_SIZE)
            h = f.read(HEADER_SIZE)
            if len(h) < HEADER_SIZE:
                raise Exception('Expected to read a full header. This was only {} bytes'.format(len(h)))
        if h == bytes([0])*HEADER_SIZE:
            return None
        return deserialize_pure_header(h, height)

    def header_at_tip(self) -> Optional[dict]:
        """Return latest header."""
        height = self.height()
        return self.read_header(height)

    def is_tip_stale(self) -> bool:
        STALE_DELAY = 8 * 60 * 60  # in seconds
        header = self.header_at_tip()
        if not header:
            return True
        # note: We check the timestamp only in the latest header.
        #       The Doichain consensus has a lot of leeway here:
        #       - needs to be greater than the median of the timestamps of the past 11 blocks, and
        #       - up to at most 2 hours into the future compared to local clock
        #       so there is ~2 hours of leeway in either direction
        if header['timestamp'] + STALE_DELAY < time.time():
            return True
        return False

    def get_hash(self, height: int) -> str:
        def is_height_checkpoint():
            within_cp_range = height <= constants.net.max_checkpoint()
            at_chunk_boundary = (height+1) % 2016 == 0
            return within_cp_range and at_chunk_boundary

        if height == -1:
            return '0000000000000000000000000000000000000000000000000000000000000000'
        elif height == 0:
            return constants.net.GENESIS
        elif is_height_checkpoint():
            index = height // 2016
            h, t = self.checkpoints[index]
            return h
        else:
            header = self.read_header(height)
            if header is None:
                raise MissingHeader(height)
            return hash_header(header)

    @classmethod
    def bits_to_target(cls, bits: int) -> int:
        bitsN = (bits >> 24) & 0xff
        if not (0x03 <= bitsN <= 0x1f): #Doichain
            raise Exception("First part of bits should be in [0x03, 0x1f]")
        bitsBase = bits & 0xffffff
        if not (0x8000 <= bitsBase <= 0x7fffff):
            raise Exception("Second part of bits should be in [0x8000, 0x7fffff]")
        return bitsBase << (8 * (bitsN-3))

    @classmethod
    def target_to_bits(cls, target: int) -> int:
        c = ("%064x" % target)[2:]
        while c[:2] == '00' and len(c) > 6:
            c = c[2:]
        bitsN, bitsBase = len(c) // 2, int.from_bytes(bfh(c[:6]), byteorder='big')
        if bitsBase >= 0x800000:
            bitsN += 1
            bitsBase >>= 8
        return bitsN << 24 | bitsBase

    def chainwork_of_header_at_height(self, height: int) -> int:
        """work done by single header at given height"""
        if height <= constants.net.max_checkpoint():
            # Below the checkpoint the target comes from the checkpoints, one per
            # chunk. Every chain shares that part, so only consistency matters.
            chunk_idx = height // 2016 - 1
            if chunk_idx == -1:
                target = compact_to_target(target_to_compact(constants.net.POW_LIMIT))[0]
            else:
                h, target = self.checkpoints[chunk_idx]
        else:
            header = self.read_header(height)
            if header is None:
                raise MissingHeader(height)
            target = derive_target(header['bits'])
            if target is None:
                raise Exception(f"bits out of range at height {height}")
        work = ((2 ** 256 - target - 1) // (target + 1)) + 1
        return work

    def _chainwork_of_range(self, first: int, last: int) -> int:
        """work done by the headers first..last, which lie in one chunk"""
        if last < first:
            return 0
        if last <= constants.net.max_checkpoint():
            return (last - first + 1) * self.chainwork_of_header_at_height(last)
        # above the checkpoint the difficulty changes with every block
        return sum(self.chainwork_of_header_at_height(h) for h in range(first, last + 1))

    @with_lock
    def get_chainwork(self, height=None) -> int:
        if height is None:
            height = max(0, self.height())
        if constants.net.TESTNET:
            # On testnet/regtest, difficulty works somewhat different.
            # It's out of scope to properly implement that.
            return height
        last_retarget = height // 2016 * 2016 - 1
        cached_height = last_retarget
        while _CHAINWORK_CACHE.get(self.get_hash(cached_height)) is None:
            if cached_height <= -1:
                break
            cached_height -= 2016
        assert cached_height >= -1, cached_height
        running_total = _CHAINWORK_CACHE[self.get_hash(cached_height)]
        while cached_height < last_retarget:
            cached_height += 2016
            running_total += self._chainwork_of_range(cached_height - 2015, cached_height)
            _CHAINWORK_CACHE[self.get_hash(cached_height)] = running_total
        return running_total + self._chainwork_of_range(last_retarget + 1, height)

    def can_connect(self, header: dict, check_height: bool=True, skip_auxpow: bool=False) -> bool:
        if header is None:
            return False
        height = header['block_height']
        if check_height and self.height() != height - 1:
            return False
        if height == 0:
            return hash_header(header) == constants.net.GENESIS
        try:
            prev_hash = self.get_hash(height - 1)
        except:
            return False
        if prev_hash != header.get('prev_block_hash'):
            return False
        try:
            target = self.get_expected_target(header)
        except MissingHeader:
            return False
        try:
            self.verify_header(header, prev_hash, target, skip_auxpow=skip_auxpow)
        except BaseException as e:
            return False
        return True

    def connect_chunk(self, idx: int, hexdata: str) -> bool:
        assert idx >= 0, idx
        try:
            data = bfh(hexdata)
            # verify_chunk also strips the AuxPoW headers
            data = self.verify_chunk(idx, data)
            self.save_chunk(idx, data)
            return True
        except BaseException as e:
            self.logger.info(f'verify_chunk idx {idx} failed: {repr(e)}')
            return False

    def get_checkpoints(self):
        # for each chunk, store the hash of the last block and the target of the
        # block after it (the target the next chunk starts with)
        cp = []
        n = self.height() // 2016
        for index in range(n):
            h = self.get_hash((index+1) * 2016 -1)
            header = self.read_header((index+1) * 2016)
            if header is None:
                break
            cp.append((h, compact_to_target(header['bits'])[0]))
        return cp


def check_header(header: dict) -> Optional[Blockchain]:
    """Returns any Blockchain that contains header, or None."""
    if type(header) is not dict:
        return None
    with blockchains_lock: chains = list(blockchains.values())
    for b in chains:
        if b.check_header(header):
            return b
    return None


def can_connect(header: dict) -> Optional[Blockchain]:
    """Returns the Blockchain that has a tip that directly links up
    with header, or None.
    """
    with blockchains_lock: chains = list(blockchains.values())
    for b in chains:
        if b.can_connect(header):
            return b
    return None


def get_chains_that_contain_header(height: int, header_hash: str) -> Sequence[Blockchain]:
    """Returns a list of Blockchains that contain header, best chain first."""
    with blockchains_lock: chains = list(blockchains.values())
    chains = [chain for chain in chains
              if chain.check_hash(height=height, header_hash=header_hash)]
    chains = sorted(chains, key=lambda x: x.get_chainwork(), reverse=True)
    return chains
