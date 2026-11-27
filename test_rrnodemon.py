"""Tests for rrnodemon's KISS parsing and monitoring (no serial port, TCP or
broker needed): python -m unittest -v"""

import json
import unittest

import rrnodemon as r


def kiss(cmd, data=b""):
    out = bytearray([r.FEND, cmd])
    for b in data:
        if b == r.FEND:
            out += bytes([r.FESC, r.TFEND])
        elif b == r.FESC:
            out += bytes([r.FESC, r.TFESC])
        else:
            out.append(b)
    out.append(r.FEND)
    return bytes(out)


# What an RNode sends rnsd while it sets it up: version, platform, and an
# echo of each radio setting (925.875 MHz, 250 kHz, SF9, CR 4/5)
SETUP = (kiss(r.CMD_FW_VERSION, bytes([1, 86])) + kiss(r.CMD_PLATFORM, bytes([0x80])) +
         kiss(r.CMD_FREQUENCY, (925875000).to_bytes(4, "big")) + kiss(r.CMD_BANDWIDTH, (250000).to_bytes(4, "big")) +
         kiss(r.CMD_SF, bytes([9])) + kiss(r.CMD_CR, bytes([5])) + kiss(r.CMD_RADIO_STATE, bytes([1])))


def chtm(util_s, nf, interference=None, airtime_s=0.0):
    u = lambda v: int(round(v * 100)).to_bytes(2, "big")
    return kiss(r.CMD_STAT_CHTM, u(airtime_s) + u(0) + u(util_s) + u(1.5) +
                bytes([nf + r.RSSI_OFFSET + 3, nf + r.RSSI_OFFSET,
                       0xFF if interference is None else interference + r.RSSI_OFFSET]))


class KissTest(unittest.TestCase):
    def test_escapes_and_split_reads(self):
        payload = bytes([1, r.FEND, 2, r.FESC, 3])
        stream = kiss(r.CMD_DATA, payload) + kiss(r.CMD_SF, b"\x09")
        dec = r.KissDecoder()
        frames = []
        for i in range(len(stream)):          # one byte at a time
            frames += dec.feed(stream[i:i + 1])
        self.assertEqual(frames, [(r.CMD_DATA, payload), (r.CMD_SF, b"\x09")])

    def test_noise_before_first_frame_is_ignored(self):
        dec = r.KissDecoder()
        self.assertEqual(dec.feed(b"boot text\r\n" + kiss(r.CMD_CR, b"\x05")), [(r.CMD_CR, b"\x05")])


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.m = r.Monitor(chan_interval=60)

    def test_radio_and_firmware_from_setup(self):
        events = self.m.feed(SETUP, 100.0)
        self.assertIn(("radio", {"freq_hz": 925875000, "bw_hz": 250000, "sf": 9, "cr": 5}), events)
        self.assertEqual(self.m.firmware(), "RNode 1.86 ESP32")
        self.assertTrue(self.m.radio_on)
        # The same settings again are not a change
        self.assertNotIn("radio", [e[0] for e in self.m.feed(SETUP, 101.0)])

    def test_packet_with_signal(self):
        self.m.feed(SETUP, 100.0)
        pkt = bytes([0x08, 0x00]) + bytes(range(40))
        stream = kiss(r.CMD_STAT_RSSI, bytes([-95 + r.RSSI_OFFSET])) + \
            kiss(r.CMD_STAT_SNR, bytes([(-6 * 4) & 0xFF])) + kiss(r.CMD_DATA, pkt)
        rx = [e for e in self.m.feed(stream, 200.0) if e[0] == "rx"]
        self.assertEqual(rx, [("rx", pkt, -95, -6.0, 200.0)])

    def test_stale_signal_is_not_used(self):
        self.m.feed(kiss(r.CMD_STAT_RSSI, bytes([60])) + kiss(r.CMD_STAT_SNR, bytes([20])), 200.0)
        rx = [e for e in self.m.feed(kiss(r.CMD_DATA, b"\x00\x00abc"), 201.0) if e[0] == "rx"]
        self.assertEqual(rx[0][2:4], (None, None))

    def test_channel_reports_averaged(self):
        self.m.feed(SETUP, 0.0)
        out = []
        for i in range(61):                   # one a second, as an RNode sends them
            util = 10.0 if i < 30 else 20.0
            out += [e for e in self.m.feed(chtm(util, -110 if i % 2 else -112, -90 if i == 5 else None,
                                                 airtime_s=0.5), float(i)) if e[0] == "chan"]
        self.assertEqual(len(out), 1)
        rep = out[0][1]
        self.assertAlmostEqual(rep["util_s"], (30 * 10 + 31 * 20) / 61, places=2)
        self.assertAlmostEqual(rep["noise_floor"], -111.0, delta=0.1)
        self.assertEqual(rep["interference"], -90.0)
        self.assertEqual(rep["airtime_s"], 0.5)
        self.assertEqual(rep["util_l"], 1.5)

    def test_no_channel_reports_while_the_radio_is_off(self):
        out = [e for i in range(70) for e in self.m.feed(chtm(5, -110), float(i)) if e[0] == "chan"]
        self.assertEqual(out, [])

    def test_reset(self):
        self.m.feed(SETUP, 0.0)
        events = self.m.feed(kiss(r.CMD_RESET, b"\xf8"), 1.0)
        self.assertIn(("reset",), events)
        self.assertFalse(self.m.radio_on)


class FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, json.loads(payload), qos, retain))

        class Info:
            rc = 0
        return Info()


class PublisherTest(unittest.TestCase):
    def setUp(self):
        cfg = {"prefix": "rradmon/v1", "uri": "mqtt://localhost", "lat": -33.5, "lon": 151.25}
        self.p = r.Publisher(cfg, "rnsd-test", lambda: {"host": True})
        self.p.network = "925875000-250000-9-5"
        self.p.radio = {"freq_hz": 925875000, "bw_hz": 250000, "sf": 9, "cr": 5}
        self.p.fw = "RNode 1.86 ESP32"
        self.p.client = FakeClient()
        self.p.connected = True

    def test_rx_message(self):
        self.p.rx(b"\x08\x00hello", -95, -6.0, 0.0)
        self.p.rx(b"\x08\x00again", None, None, 1.0)
        (t1, m1, q1, _), (_, m2, _, _) = self.p.client.sent
        self.assertEqual(t1, "rradmon/v1/925875000-250000-9-5/rnsd-test/rx")
        self.assertEqual(q1, 1)
        self.assertEqual(m1["raw"], False)
        self.assertEqual((m1["seq"], m1["rssi"], m1["snr"], m1["len"]), (1, -95.0, -6.0, 7))
        self.assertEqual(m1["t_obs"], "1970-01-01T00:00:00.000Z")
        self.assertNotIn("rssi", m2)
        self.assertNotIn("snr", m2)
        self.assertEqual(m2["seq"], 2)

    def test_status(self):
        self.p.status()
        topic, m, qos, retain = self.p.client.sent[0]
        self.assertEqual(topic, "rradmon/v1/925875000-250000-9-5/rnsd-test/status")
        self.assertTrue(retain)
        self.assertEqual((m["online"], m["fw"], m["lat"], m["host"]), (True, "RNode 1.86 ESP32", -33.5, True))
        self.assertEqual(m["radio"]["sync"], 0x12)

    def test_not_connected_counts_dropped(self):
        self.p.connected = False
        self.p.rx(b"\x08\x00x", -90, 5.0, 0.0)
        self.assertEqual((self.p.dropped, self.p.client.sent), (1, []))


if __name__ == "__main__":
    unittest.main()
