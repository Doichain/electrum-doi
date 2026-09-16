#!/usr/bin/env python3
"""Check the wallet's header verification against the live Doichain mainnet chain.

The unit tests in electrum/tests/test_doichain_difficulty.py run against recorded
headers, which is enough for the arithmetic but never touches the network, the
AuxPoW parsing or the checkpoint boundary. This script does the other half: it
pulls real headers from a running ElectrumX server and checks two things.

  1. Every header above the last checkpoint is accepted, with AuxPoW verified.
  2. Tampered headers are rejected -- in particular one whose merkle root has
     been replaced, which is the attack the missing verification allowed:
     a server could commit to fabricated transactions and have them shown as
     confirmed.

Run it before a release:

    PYTHONPATH=. python3 contrib/verify_against_mainnet.py [host] [port]

Exits non-zero if any real header is rejected or any forgery is accepted.
"""
import copy
import json
import socket
import sys

from electrum import blockchain as bc
from electrum import constants
from electrum.blockchain import (Blockchain, deserialize_full_header,
                                 get_next_work_required, hash_header)

DEFAULT_SERVER = "78.47.147.220"
DEFAULT_PORT = 50001


class ElectrumXClient:
    """Minimal JSON-RPC client. Matches on the request id, because the server
    sends notifications of its own that a naive readline() would pick up."""

    def __init__(self, host: str, port: int, timeout: int = 90):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._file = self._sock.makefile("rwb")
        self._id = 0

    def call(self, method: str, params: list):
        self._id += 1
        request_id = self._id
        payload = json.dumps({"jsonrpc": "2.0", "id": request_id,
                              "method": method, "params": params})
        self._file.write((payload + "\n").encode())
        self._file.flush()
        while True:
            line = self._file.readline()
            if not line:
                raise RuntimeError("server closed the connection")
            message = json.loads(line.decode())
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise RuntimeError(message["error"])
            return message["result"]


def main(host: str, port: int) -> int:
    client = ElectrumXClient(host, port)
    server_version = client.call("server.version", ["verify_against_mainnet", "1.4"])
    tip = client.call("blockchain.headers.subscribe", [])["height"]
    checkpoint = constants.net.max_checkpoint()
    start = checkpoint + 1

    print(f"server          {host}:{port} {server_version}")
    print(f"chain tip       {tip}")
    print(f"last checkpoint {checkpoint}")
    print(f"checking        {start}..{tip}")

    if start > tip:
        print("nothing above the checkpoint to check yet")
        return 0

    # Above the checkpoint the server sends full headers including AuxPoW, which
    # is what the wallet parses there. Below it the wallet reads 80 bytes only,
    # so a range starting below the checkpoint would desynchronise the parser.
    raw = bytes.fromhex(client.call("blockchain.block.headers",
                                    [start, tip - start + 1, 0])["hex"])
    live = {}
    position, height = 0, start
    while position < len(raw):
        header, position = deserialize_full_header(
            raw, height, expect_trailing_data=True, start_position=position)
        live[height] = header
        height += 1

    # The first headers above the checkpoint look back at headers below it,
    # which a fresh wallet does not have on disk.
    tail = bc.checkpoint_tail_headers()
    print(f"parsed          {len(live)} headers with AuxPoW, "
          f"{len(tail)} look-back headers from checkpoint_tail_headers\n")

    def read_header(h: int) -> dict:
        if h in live:
            return live[h]
        if h in tail:
            return tail[h]
        raise KeyError(h)

    accepted = rejected = 0
    problems = []
    for h in sorted(live):
        header = live[h]
        prev_hash = hash_header(read_header(h - 1))
        target = bc.compact_to_target(get_next_work_required(header, read_header))[0]
        try:
            Blockchain.verify_header(header, prev_hash, target, skip_auxpow=False)
            accepted += 1
        except Exception as exc:
            rejected += 1
            if len(problems) < 5:
                problems.append(f"  height {h}: {type(exc).__name__}: {exc}")
    print(f"real headers    {accepted} accepted, {rejected} rejected")
    for line in problems:
        print(line)

    # A forged header has to be refused, otherwise the verification is decorative.
    sample_height = sorted(live)[len(live) // 2]
    sample = live[sample_height]
    prev_hash = hash_header(read_header(sample_height - 1))
    target = bc.compact_to_target(get_next_work_required(sample, read_header))[0]

    def is_rejected(mutate, description: str) -> bool:
        header = copy.deepcopy(sample)
        mutate(header)
        try:
            Blockchain.verify_header(header, prev_hash, target, skip_auxpow=False)
        except Exception:
            print(f"  rejected: {description}")
            return True
        print(f"  ACCEPTED: {description}  <-- verification is not working")
        return False

    print(f"\nforged headers  (based on real height {sample_height})")
    results = [
        is_rejected(lambda h: h.__setitem__("bits", 0x1d00ffff),
                    "difficulty lowered to Bitcoin's minimum"),
        is_rejected(lambda h: h.__setitem__("merkle_root", "11" * 32),
                    "merkle root replaced (fabricated transactions)"),
        is_rejected(lambda h: h.__setitem__("timestamp", h["timestamp"] + 99999),
                    "timestamp moved"),
        is_rejected(lambda h: h.__setitem__("nonce", h["nonce"] ^ 0xffff),
                    "nonce changed"),
    ]

    failed = rejected > 0 or not all(results)
    print(f"\n{'FAILED' if failed else 'OK'}: {accepted} real headers accepted, "
          f"{sum(results)}/{len(results)} forgeries rejected")
    return 1 if failed else 0


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SERVER
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT
    sys.exit(main(host, port))
