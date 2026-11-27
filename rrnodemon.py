#!/usr/bin/env python3
"""Rrnodemon: watch an RNode that rnsd is using, and report what it hears to
Rradmon over MQTT.

Rrnodemon opens the RNode's serial port and offers rnsd a TCP port in its
place (RNS's `port = tcp://127.0.0.1` for RNode interfaces, port 7633).
Everything passes through unchanged in both directions, so rnsd uses the
RNode as before. Rrnodemon never sends anything to the RNode itself.

From the RNode's side it takes:
  - each received packet with its RSSI and SNR. An RNode in normal mode
    joins split packets itself, so these are whole Reticulum packets
    (Rradmon's "raw": false); split halves are not seen.
  - the channel reports (noise floor, utilisation, airtime), averaged over
    an interval
  - the radio settings and firmware version, from its replies to rnsd

and publishes them as Rradmon observer messages v1 (Rradmon docs/spec-v0.md
section 5). Packets rnsd transmits are not published.

Needs Python 3.7+, pyserial and paho-mqtt (1.x or 2.x).
"""

import argparse
import base64
import configparser
import datetime
import json
import logging
import os
import re
import socket
import ssl
import sys
import threading
import time
from urllib.parse import urlparse

VERSION = "0.1.0"

log = logging.getLogger("rrnodemon")

# ─── RNode host protocol (KISS framing) ──────────────────────────────────────
FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD

CMD_DATA = 0x00
CMD_FREQUENCY = 0x01
CMD_BANDWIDTH = 0x02
CMD_SF = 0x04
CMD_CR = 0x05
CMD_RADIO_STATE = 0x06
CMD_STAT_RSSI = 0x23
CMD_STAT_SNR = 0x24
CMD_STAT_CHTM = 0x25
CMD_PLATFORM = 0x48
CMD_MCU = 0x49
CMD_FW_VERSION = 0x50
CMD_RESET = 0x55

RSSI_OFFSET = 157
RNS_TCP_PORT = 7633         # fixed in RNS for RNodes over TCP
SIGNAL_MAX_AGE = 0.5        # s: RSSI and SNR arrive just before their packet
PLATFORMS = {0x90: "AVR", 0x80: "ESP32", 0x70: "nRF52"}


class KissDecoder:
    """Turns a byte stream into (command, data) frames."""

    MAX_FRAME = 1024

    def __init__(self):
        self.in_frame = False
        self.escaped = False
        self.buf = bytearray()

    def feed(self, data):
        frames = []
        for b in data:
            if b == FEND:
                if self.in_frame and len(self.buf) > 0:
                    frames.append((self.buf[0], bytes(self.buf[1:])))
                # A FEND both ends a frame and may start the next
                self.in_frame, self.escaped, self.buf = True, False, bytearray()
            elif not self.in_frame:
                continue
            elif self.escaped:
                self.buf.append(FEND if b == TFEND else FESC if b == TFESC else b)
                self.escaped = False
            elif b == FESC:
                self.escaped = True
            elif len(self.buf) < self.MAX_FRAME:
                self.buf.append(b)
            else:
                self.in_frame = False   # runaway frame: wait for the next FEND
        return frames


def utc_now_iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ─── What the RNode says ─────────────────────────────────────────────────────
class ChannelAverager:
    """One channel report per interval: utilisation and noise floor averaged,
    interference the strongest seen, the other fields from the last report."""

    def __init__(self, interval):
        self.interval = interval
        self.start = None
        self.reset()

    def reset(self):
        self.n, self.sum_util, self.sum_nf, self.interference, self.last = 0, 0.0, 0.0, None, None

    def add(self, report, t):
        if self.start is None:
            self.start = t
        self.n += 1
        self.sum_util += report["util_s"]
        self.sum_nf += report["noise_floor"]
        if report.get("interference") is not None:
            if self.interference is None or report["interference"] > self.interference:
                self.interference = report["interference"]
        self.last = report
        if t - self.start < self.interval:
            return None
        out = dict(self.last)
        out["util_s"] = round(self.sum_util / self.n, 2)
        out["noise_floor"] = round(self.sum_nf / self.n, 1)
        out["interference"] = self.interference
        if out["interference"] is None:
            del out["interference"]
        self.start = t
        self.reset()
        return out


class Monitor:
    """Follows the RNode's output (to rnsd) and turns it into events:
    ("rx", packet, rssi, snr, t), ("chan", report, t), ("radio", settings)
    when the radio settings change, ("reset",) when the RNode restarts."""

    def __init__(self, chan_interval=60.0):
        self.dec = KissDecoder()
        self.freq = self.bw = self.sf = self.cr = None
        self.radio_on = False
        self.fw = None
        self.platform = None
        self.rssi = self.snr = None
        self.rssi_t = self.snr_t = 0.0
        self.chan = ChannelAverager(chan_interval)

    def radio(self):
        """The radio settings, once rnsd has set all of them."""
        if None in (self.freq, self.bw, self.sf, self.cr):
            return None
        return {"freq_hz": self.freq, "bw_hz": self.bw, "sf": self.sf, "cr": self.cr}

    def firmware(self):
        if self.fw is None:
            return "RNode"
        s = "RNode %d.%d" % self.fw
        if self.platform is not None:
            s += " " + PLATFORMS.get(self.platform, "0x%02x" % self.platform)
        return s

    def feed(self, data, t):
        events = []
        for cmd, d in self.dec.feed(data):
            events.extend(self._frame(cmd, d, t))
        return events

    def _frame(self, cmd, d, t):
        before = self.radio()
        out = []
        if cmd == CMD_STAT_RSSI and len(d) >= 1:
            self.rssi, self.rssi_t = d[0] - RSSI_OFFSET, t
        elif cmd == CMD_STAT_SNR and len(d) >= 1:
            self.snr, self.snr_t = (d[0] - 256 if d[0] > 127 else d[0]) * 0.25, t
        elif cmd == CMD_DATA:
            fresh = self.rssi is not None and self.snr is not None and \
                t - self.rssi_t < SIGNAL_MAX_AGE and t - self.snr_t < SIGNAL_MAX_AGE
            if len(d) >= 2:   # a Reticulum header at least
                out.append(("rx", d, self.rssi if fresh else None, self.snr if fresh else None, t))
            self.rssi = self.snr = None
        elif cmd == CMD_STAT_CHTM and len(d) >= 11:
            u16 = lambda i: (d[i] << 8 | d[i + 1]) / 100.0
            report = {
                "airtime_s": u16(0), "airtime_l": u16(2), "util_s": u16(4), "util_l": u16(6),
                "rssi": float(d[8] - RSSI_OFFSET), "noise_floor": float(d[9] - RSSI_OFFSET),
                "interference": None if d[10] == 0xFF else float(d[10] - RSSI_OFFSET),
            }
            avg = self.chan.add(report, t) if self.radio_on else None
            if avg:
                out.append(("chan", avg, t))
        elif cmd == CMD_FREQUENCY and len(d) >= 4:
            self.freq = int.from_bytes(d[:4], "big")
        elif cmd == CMD_BANDWIDTH and len(d) >= 4:
            self.bw = int.from_bytes(d[:4], "big")
        elif cmd == CMD_SF and len(d) >= 1:
            self.sf = d[0]
        elif cmd == CMD_CR and len(d) >= 1:
            self.cr = d[0]
        elif cmd == CMD_RADIO_STATE and len(d) >= 1:
            self.radio_on = d[0] == 0x01
        elif cmd == CMD_FW_VERSION and len(d) >= 2:
            self.fw = (d[0], d[1])
        elif cmd == CMD_PLATFORM and len(d) >= 1:
            self.platform = d[0]
        elif cmd == CMD_RESET and len(d) >= 1 and d[0] == 0xF8:
            self.radio_on = False
            self.chan.start = None
            self.chan.reset()
            out.append(("reset",))
        after = self.radio()
        if after is not None and after != before:
            out.append(("radio", after))
        return out


# ─── MQTT ────────────────────────────────────────────────────────────────────
class Publisher:
    """Rradmon observer messages v1. The topic carries the radio network, which
    is only known once rnsd has set the radio up, so the client connects then
    (the last will needs the topic), and again if the settings change."""

    def __init__(self, cfg, observer, status_extra):
        self.cfg = cfg
        self.observer = observer
        self.status_extra = status_extra   # callable: dict of live fields
        self.client = None
        self.network = None
        self.radio = None
        self.fw = "RNode"
        self.connected = False
        self.seq = 0
        self.published = 0
        self.dropped = 0
        self.started = time.time()
        self.lock = threading.Lock()

    def topic(self, kind, network=None):
        return "%s/%s/%s/%s" % (self.cfg["prefix"], network or self.network, self.observer, kind)

    def set_radio(self, radio, fw):
        network = "%d-%d-%d-%d" % (radio["freq_hz"], radio["bw_hz"], radio["sf"], radio["cr"])
        with self.lock:
            if network == self.network:
                return
            old = self.network
            if self.client:
                # Say so on the old network's topic before leaving it
                self._publish_status(old, False)
                self.client.disconnect()
                self.client.loop_stop()
                self.client = None
            self.network, self.radio, self.fw = network, radio, fw
            self._connect()
        log.info("radio %s: publishing under %s/%s/%s/", network, self.cfg["prefix"], network, self.observer)

    def _connect(self):
        import paho.mqtt.client as mqtt
        u = urlparse(self.cfg["uri"])
        transport = "websockets" if u.scheme in ("ws", "wss") else "tcp"
        client_id = "rrnodemon-" + self.observer
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, transport=transport)
        except AttributeError:    # paho-mqtt 1.x
            c = mqtt.Client(client_id=client_id, transport=transport)
        if u.scheme in ("mqtts", "wss", "ssl"):
            c.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        if transport == "websockets":
            c.ws_set_options(path=u.path or "/mqtt")
        if self.cfg.get("user"):
            c.username_pw_set(self.cfg["user"], self.cfg.get("password") or None)
        c.will_set(self.topic("status"), json.dumps({"v": 1, "observer": self.observer, "online": False}),
                   qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.reconnect_delay_set(1, 30)
        port = u.port or {"mqtt": 1883, "mqtts": 8883, "ssl": 8883, "ws": 80, "wss": 443}.get(u.scheme, 1883)
        c.connect_async(u.hostname, port, keepalive=30)
        c.loop_start()
        self.client = c

    def _on_connect(self, client, userdata, flags, rc, *args):
        ok = (rc == 0) if isinstance(rc, int) else not rc.is_failure
        if ok:
            self.connected = True
            log.info("MQTT: connected to the broker")
            self.status()
        else:
            log.warning("MQTT: the broker refused the connection (%s): check user and password", rc)

    def _on_disconnect(self, client, userdata, *args):
        if self.connected:
            log.warning("MQTT: disconnected, retrying")
        self.connected = False

    def _publish(self, topic, payload, qos, retain=False):
        if not self.client or not self.connected:
            self.dropped += 1
            return
        info = self.client.publish(topic, payload, qos=qos, retain=retain)
        if info.rc == 0:
            self.published += 1
        else:
            self.dropped += 1

    def rx(self, packet, rssi, snr, t):
        self.seq += 1    # counts every packet, so lost ones show as gaps
        msg = {"v": 1, "observer": self.observer, "seq": self.seq, "t_obs": utc_now_iso(t),
               "len": len(packet), "frame": base64.b64encode(packet).decode(), "raw": False}
        if rssi is not None:
            msg["rssi"] = float(rssi)
            msg["snr"] = float(snr)
        if self.network:
            self._publish(self.topic("rx"), json.dumps(msg, separators=(",", ":")), 1)
        else:
            self.dropped += 1

    def chan(self, report, t):
        if not self.network:
            return
        msg = {"v": 1, "observer": self.observer, "t_obs": utc_now_iso(t)}
        msg.update(report)
        self._publish(self.topic("chan"), json.dumps(msg, separators=(",", ":")), 0)

    def _publish_status(self, network, online):
        msg = {"v": 1, "observer": self.observer, "online": online, "fw": self.fw,
               "sw": "rrnodemon " + VERSION, "uptime_s": int(time.time() - self.started),
               "t_obs": utc_now_iso(time.time())}
        if self.radio:
            msg["radio"] = dict(self.radio, sync=0x12)
        if self.cfg.get("lat") is not None and self.cfg.get("lon") is not None:
            msg["lat"], msg["lon"] = self.cfg["lat"], self.cfg["lon"]
        msg.update(self.status_extra())
        msg["mqtt_dropped"] = self.dropped
        self._publish(self.topic("status", network), json.dumps(msg, separators=(",", ":")), 1, retain=True)

    def status(self):
        if self.network:
            self._publish_status(self.network, True)

    def close(self):
        with self.lock:
            if self.client:
                self._publish_status(self.network, False)
                time.sleep(0.5)
                self.client.disconnect()
                self.client.loop_stop()


# ─── The pass-through ────────────────────────────────────────────────────────
class Bridge:
    """The RNode's serial port on one side, rnsd's TCP connection on the
    other. One rnsd at a time; a new connection replaces the old one."""

    REOPEN_WAIT = 5.0

    def __init__(self, port, speed, listen, monitor, on_events):
        self.port, self.speed, self.listen = port, speed, listen
        self.monitor = monitor
        self.on_events = on_events
        self.serial = None
        self.client = None
        self.client_lock = threading.Lock()
        self.serial_lock = threading.Lock()
        self.stopping = False
        self.rx_bytes = self.tx_bytes = 0

    def host_connected(self):
        return self.client is not None

    def rnode_open(self):
        return self.serial is not None

    def drop_client(self, why):
        with self.client_lock:
            if self.client:
                log.info("rnsd: connection closed (%s)", why)
                try:
                    self.client.close()
                except OSError:
                    pass
                self.client = None

    def serial_loop(self):
        import serial
        while not self.stopping:
            try:
                s = serial.Serial(self.port, self.speed, timeout=0.1, rtscts=False, dsrdtr=False, xonxoff=False)
            except (serial.SerialException, OSError) as e:
                log.warning("RNode: cannot open %s (%s); retrying in %d s", self.port, e, self.REOPEN_WAIT)
                time.sleep(self.REOPEN_WAIT)
                continue
            log.info("RNode: %s open", self.port)
            self.serial = s
            # rnsd must set the RNode up again: make it reconnect
            self.drop_client("RNode (re)opened")
            try:
                while not self.stopping:
                    data = s.read(4096)
                    if not data:
                        continue
                    self.rx_bytes += len(data)
                    with self.client_lock:
                        c = self.client
                    if c:
                        try:
                            c.sendall(data)
                        except OSError as e:
                            self.drop_client("send failed: %s" % e)
                    self.on_events(self.monitor.feed(data, time.time()))
            except (serial.SerialException, OSError) as e:
                log.warning("RNode: %s lost (%s)", self.port, e)
            finally:
                self.serial = None
                try:
                    s.close()
                except Exception:
                    pass
                self.drop_client("RNode port closed")
            time.sleep(self.REOPEN_WAIT)

    def accept_loop(self):
        host = self.listen
        srv = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, RNS_TCP_PORT))
        srv.listen(2)
        log.info("rnsd: listening on %s:%d (in rnsd's RNode interface: port = tcp://%s)",
                 host, RNS_TCP_PORT, host)
        while not self.stopping:
            conn, addr = srv.accept()
            if self.serial is None:
                # Nothing to pass to yet; rnsd tries again in a few seconds
                log.info("rnsd: connection from %s refused, the RNode is not open", addr[0])
                conn.close()
                continue
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.drop_client("replaced by a new connection")
            with self.client_lock:
                self.client = conn
            log.info("rnsd: connected from %s", addr[0])
            threading.Thread(target=self.client_loop, args=(conn,), daemon=True).start()

    def client_loop(self, conn):
        # Only to log what rnsd sends (with -v); nothing from it is published
        host_dec = KissDecoder()
        while not self.stopping:
            try:
                data = conn.recv(4096)
            except OSError:
                data = b""
            if not data:
                with self.client_lock:
                    mine = self.client is conn
                if mine:
                    self.drop_client("rnsd disconnected")
                return
            s = self.serial
            if s is None:
                continue
            try:
                with self.serial_lock:
                    s.write(data)
                self.tx_bytes += len(data)
                if log.isEnabledFor(logging.DEBUG):
                    for cmd, d in host_dec.feed(data):
                        if cmd == CMD_DATA:
                            log.debug("tx %dB from rnsd", len(d))
            except Exception as e:
                log.warning("RNode: write failed (%s)", e)

    def start(self):
        threading.Thread(target=self.serial_loop, daemon=True).start()
        threading.Thread(target=self.accept_loop, daemon=True).start()


# ─── Configuration and main ──────────────────────────────────────────────────
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def load_config(path):
    cp = configparser.ConfigParser()
    if not cp.read(path):
        raise SystemExit("rrnodemon: cannot read %s" % path)
    get = lambda s, k, d=None: cp.get(s, k, fallback=d).strip() if cp.get(s, k, fallback=d) is not None else None
    cfg = {
        "port": get("rnode", "port"),
        "speed": int(get("rnode", "speed", "115200")),
        "listen": get("rnsd", "listen", "127.0.0.1"),
        "uri": get("mqtt", "uri"),
        "user": get("mqtt", "user", ""),
        "prefix": get("mqtt", "prefix", "rradmon/v1").strip("/"),
        "id": get("observer", "id", "") or "rrnodemon-" + re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname())[:54],
        "chan_interval": int(get("observer", "chan_interval", "60")),
        "lat": None, "lon": None,
    }
    if not cfg["port"]:
        raise SystemExit("rrnodemon: [rnode] port is required")
    if not cfg["uri"]:
        raise SystemExit("rrnodemon: [mqtt] uri is required")
    if urlparse(cfg["uri"]).scheme not in ("mqtt", "mqtts", "ssl", "ws", "wss"):
        raise SystemExit("rrnodemon: [mqtt] uri must start with mqtt://, mqtts://, ws:// or wss://")
    if not ID_RE.match(cfg["id"]):
        raise SystemExit("rrnodemon: [observer] id: 1 to 64 letters, digits, . _ or -")
    if not 60 <= cfg["chan_interval"] <= 600:
        raise SystemExit("rrnodemon: [observer] chan_interval: 60 to 600 seconds")
    lat, lon = get("observer", "lat"), get("observer", "lon")
    if lat and lon:
        cfg["lat"], cfg["lon"] = float(lat), float(lon)
        if abs(cfg["lat"]) > 90 or abs(cfg["lon"]) > 180:
            raise SystemExit("rrnodemon: [observer] lat/lon out of range")
    # The broker password: from a file, or the environment; never the config
    pw_file = get("mqtt", "password_file", "")
    cfg["password"] = os.environ.get("RRNODEMON_MQTT_PASSWORD", "")
    if pw_file:
        with open(pw_file, encoding="utf-8") as f:
            cfg["password"] = f.read().strip()
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description="Watch an RNode that rnsd uses and report to Rradmon over MQTT.")
    ap.add_argument("-c", "--config", default="/etc/rrnodemon/rrnodemon.conf", help="config file")
    ap.add_argument("-v", "--verbose", action="store_true", help="log each packet")
    ap.add_argument("--version", action="version", version="rrnodemon " + VERSION)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    log.info("rrnodemon %s, observer %s", VERSION, cfg["id"])

    monitor = Monitor(cfg["chan_interval"])
    counts = {"rx": 0}
    bridge = None
    pub = Publisher(cfg, cfg["id"], lambda: {
        "rx_frames": counts["rx"],
        "host": bridge.host_connected() if bridge else False,
        "rnode": bridge.rnode_open() if bridge else False,
    })

    def on_events(events):
        for e in events:
            if e[0] == "rx":
                counts["rx"] += 1
                _, packet, rssi, snr, t = e
                if args.verbose:
                    log.debug("rx %dB %s dBm %s dB", len(packet), rssi, snr)
                pub.rx(packet, rssi, snr, t)
            elif e[0] == "chan":
                pub.chan(e[1], e[2])
            elif e[0] == "radio":
                pub.set_radio(e[1], monitor.firmware())
            elif e[0] == "reset":
                log.info("RNode: reset")

    bridge = Bridge(cfg["port"], cfg["speed"], cfg["listen"], monitor, on_events)
    bridge.start()
    try:
        while True:
            time.sleep(60)
            pub.status()
            log.info("status: rnsd %s, %d packets, %d published, %d dropped",
                     "connected" if bridge.host_connected() else "not connected",
                     counts["rx"], pub.published, pub.dropped)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stopping = True
        pub.close()


if __name__ == "__main__":
    main()
