# Rrnodemon

An observer for [Rradmon](https://github.com/dkmr27/Rradmon) built from an
RNode that rnsd is already using. The RNode keeps working as rnsd's LoRa
interface; Rrnodemon sits between the two, watches what the RNode hears, and
publishes it to Rradmon over MQTT.

```
RNode ──USB serial── rrnodemon ──TCP 127.0.0.1:7633── rnsd
                         │
                         └── MQTT ──> Rradmon
```

Rrnodemon opens the RNode's serial port and offers rnsd a TCP port in its
place, using RNS's own support for RNodes over TCP. Every byte passes through
unchanged in both directions. Rrnodemon never sends anything to the RNode
itself, and rnsd configures and uses it exactly as before.

## What it reports

- **Packets** the RNode receives, with RSSI and SNR. An RNode used by rnsd joins
  split packets itself, so these are whole Reticulum packets (Rradmon's
  `"raw": false`). Rradmon matches them with what other observers hear.
- **Channel reports** (noise floor, utilisation, airtime), averaged over a
  minute. Airtime is the RNode's own transmissions, so here it shows how busy
  rnsd keeps the radio.
- **Status**: the RNode's firmware and radio settings, read from its replies to
  rnsd, and whether rnsd is connected.

Not reported: the halves of split packets (the RNode never passes them on),
and packets rnsd transmits (the RNode does not hear its own transmissions;
`-v` logs them).

Messages are Rradmon observer messages v1 (Rradmon `docs/spec-v0.md`,
section 5), under `rradmon/v1/<freq>-<bw>-<sf>-<cr>/<observer id>/`. A
Rradmon server 0.3.0 or later is needed.

## No MQTT broker?

If Rradmon runs on the same machine as rnsd, it can be the broker itself
(Rradmon 0.4.0 and later): start it with `rradmon run -mqtt-listen
127.0.0.1:1883 …` and set Rrnodemon's broker to it, with no user or password:

```ini
[mqtt]
uri = mqtt://127.0.0.1:1883
```

Only programs on that machine can connect. To use a Rradmon on another
machine this way, it needs `-mqtt-listen-user` and a password; give Rrnodemon
the same in `user` and `password_file`.

## Install

Needs Python 3.7 or later with pyserial and paho-mqtt (1.x or 2.x). A
machine running rnsd has pyserial already.

From a copy of this repository, as root:

```
sudo ./install.sh
```

It installs:

- `rrnodemon.py` in `/opt/rrnodemon`;
- a config in `/etc/rrnodemon/rrnodemon.conf`, from the example (an existing
  one is kept);
- a user `rrnodemon`, in the group that owns the serial ports;
- a service that starts at boot: systemd where there is one, else the
  Slackware rc script (`/etc/rc.d/rc.rrnodemon`, started from `rc.local`,
  with a logrotate entry).

On Debian and Raspberry Pi OS it installs pyserial and paho-mqtt with apt if
they are missing; elsewhere it says which are missing (`pip3 install
paho-mqtt`, or your distribution's package).

Then:

1. Edit `/etc/rrnodemon/rrnodemon.conf`: the RNode's port, the MQTT broker
   and the observer id.
2. If the broker needs a password, put it in a file of its own and name it
   in `password_file`:

   ```
   sudo sh -c 'umask 077; cat > /etc/rrnodemon/mqtt_password'
   sudo chown rrnodemon /etc/rrnodemon/mqtt_password
   ```

3. Start it: `sudo systemctl start rrnodemon`, or on Slackware
   `sudo /etc/rc.d/rc.rrnodemon start`. Its log: `journalctl -u rrnodemon -f`,
   or `/var/log/rrnodemon.log`.
4. In rnsd's config (`~/.reticulum/config`), point the RNode interface at
   Rrnodemon instead of the serial port, keep its radio settings, and
   restart rnsd:

   ```ini
     [[RNode]]
       type = RNodeInterface
       interface_enabled = true
       port = tcp://127.0.0.1
       frequency = 925875000
       bandwidth = 250000
       txpower = 10
       spreadingfactor = 9
       codingrate = 5
   ```

To update, get the new version and run `sudo ./install.sh` again: it keeps
the config and restarts Rrnodemon. `sudo ./install.sh --uninstall` removes
the program and the service, and leaves `/etc/rrnodemon` and the user.

The Slackware rc script also takes `restart` and `status`, and `chmod 644
/etc/rc.d/rc.rrnodemon` disables it. Like the systemd service, it starts
Rrnodemon again 5 s after it exits.

### Without the installer

The installer does no more than this; with systemd:

```
sudo useradd --system --no-create-home -G dialout rrnodemon
sudo mkdir -p /opt/rrnodemon /etc/rrnodemon
sudo cp rrnodemon.py /opt/rrnodemon/
sudo cp rrnodemon.conf.example /etc/rrnodemon/rrnodemon.conf
sudo cp rrnodemon.service /etc/systemd/system/
sudo systemctl enable --now rrnodemon
```

On Slackware, copy `rc.rrnodemon` to `/etc/rc.d/` (mode 755) instead of the
unit, and add to `/etc/rc.d/rc.local`:

```
if [ -x /etc/rc.d/rc.rrnodemon ]; then /etc/rc.d/rc.rrnodemon start; fi
```

and the same with `stop` to `/etc/rc.d/rc.local_shutdown`. The group that
owns the serial ports there may be `uucp` rather than `dialout`: check with
`ls -l /dev/ttyACM0`.

Start order does not matter. rnsd retries a TCP RNode every few seconds until
Rrnodemon answers, and reconnects by itself if Rrnodemon restarts. If the
RNode is unplugged, Rrnodemon reopens it when it is back and makes rnsd
reconnect, so rnsd sets it up again.

To update the RNode's firmware with `rnodeconf`, stop Rrnodemon first: it
holds the serial port.

## Running it by hand

```
python3 rrnodemon.py -c rrnodemon.conf -v
```

`-v` logs each packet received (`rx`) and each packet rnsd sends (`tx`),
which also helps to see what an RNode is doing on air.

## Security

Anything that can connect to Rrnodemon's TCP port can use the RNode, including
transmitting. It listens on `127.0.0.1` by default; only change `listen` on a
network you trust.

## Tests

```
python3 -m unittest -v
```

They feed recorded RNode byte streams through the parser and check the
published messages; no RNode, rnsd or broker is needed.

## Licence

MIT
