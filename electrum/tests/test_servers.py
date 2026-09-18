import socket

from electrum import constants
from electrum.interface import bucket_of_ip_address

from . import ElectrumTestCase


def _resolve(host, family):
    try:
        infos = socket.getaddrinfo(host, None, family)
    except socket.gaierror:
        return None
    return infos[0][4][0] if infos else None


class Test_DefaultServers(ElectrumTestCase):
    """The bundled server list is the whole universe a fresh wallet knows:
    these servers advertise no peers, so nothing is discovered later. The
    network layer keeps at most one connection per bucket -- a /16 for IPv4,
    a /48 for IPv6 -- so two servers sharing one are one server. See #22.
    """

    def test_list_is_not_empty(self):
        self.assertGreaterEqual(len(constants.net.DEFAULT_SERVERS), 4)

    def test_no_two_servers_share_a_bucket(self):
        # DNS, so it can only run with a network. Skipped rather than failed
        # offline: the thing being checked is a property of the live records.
        for family, label in ((socket.AF_INET, 'IPv4'), (socket.AF_INET6, 'IPv6')):
            buckets = {}
            resolved = 0
            for host in constants.net.DEFAULT_SERVERS:
                addr = _resolve(host, family)
                if addr is None:
                    continue
                resolved += 1
                bucket = bucket_of_ip_address(addr)
                self.assertNotIn(
                    bucket, buckets,
                    msg=(f"{label}: {host} ({addr}) is in {bucket}, "
                         f"same as {buckets.get(bucket)} -- only one of them "
                         f"will ever be connected"))
                buckets[bucket] = host
            if resolved < 2:
                self.skipTest(f"could not resolve enough servers over {label}")
