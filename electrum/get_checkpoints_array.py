#!/usr/bin/env python3
"""Generate checkpoints.json and checkpoint_tail_headers.json from a synced
Doichain Core node.

Electrum checkpoints are [hash, target] for every complete 2016-block chunk:
the hash of the chunk's last block (2015, 2015 + 2016, ...) and the target of
the block after it, i.e. the target the next chunk starts with.

The tail file holds the raw 80-byte headers of the last 28 blocks up to the
final checkpoint. The DigiShield rules of the first headers after the
checkpoint look back that far (17-block average, 11-block median time).

Usage: DOICHAIN_RPC_USER=... DOICHAIN_RPC_PASSWORD=... [DOICHAIN_RPC_PORT=8339] \
       python3 get_checkpoints_array.py
"""
import base64
import json
import os
import urllib.request

INTERVAL = 2016
TAIL = 17 + 11  # DigiShield averaging window + median time span


def rpc(method, params):
    port = os.environ.get('DOICHAIN_RPC_PORT', '8339')
    credentials = '%s:%s' % (os.environ['DOICHAIN_RPC_USER'], os.environ['DOICHAIN_RPC_PASSWORD'])
    request = urllib.request.Request(
        "http://127.0.0.1:{}/".format(port),
        json.dumps({"jsonrpc": "1.0", "id": "1", "method": method, "params": params}).encode(),
        {'content-type': 'application/json',
         'Authorization': 'Basic ' + base64.b64encode(credentials.encode()).decode()})
    with urllib.request.urlopen(request) as response:
        reply = json.loads(response.read())
    if reply.get('error'):
        raise Exception(reply['error'])
    return reply['result']


def compact_to_target(bits):
    size, word = bits >> 24, bits & 0x007fffff
    if size <= 3:
        return word >> (8 * (3 - size))
    return word << (8 * (size - 3))


def main():
    block_count = rpc('getblockcount', [])
    print('Blocks: {}'.format(block_count))
    checkpoints = []
    index = 0
    # the target after a chunk needs the first block of the next chunk
    while (index + 1) * INTERVAL <= block_count:
        last_hash = rpc('getblockhash', [(index + 1) * INTERVAL - 1])
        next_header = rpc('getblockheader', [rpc('getblockhash', [(index + 1) * INTERVAL])])
        checkpoints.append([last_hash, compact_to_target(int(next_header['bits'], 16))])
        index += 1
    last_height = len(checkpoints) * INTERVAL - 1
    tail = []
    for height in range(last_height - TAIL + 1, last_height + 1):
        # verbose=false returns the header including AuxPoW; the first 80 bytes are the block header
        tail.append(rpc('getblockheader', [rpc('getblockhash', [height]), False])[:160])
    with open('checkpoints_output.json', 'w') as f:
        f.write(json.dumps(checkpoints, indent=4))
    with open('checkpoint_tail_headers_output.json', 'w') as f:
        f.write(json.dumps({"height": last_height - TAIL + 1, "headers": tail}, indent=4))
    print('Done: {} checkpoints up to height {}.'.format(len(checkpoints), last_height))


if __name__ == '__main__':
    main()
