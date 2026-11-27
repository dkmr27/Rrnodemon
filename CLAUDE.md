# Rrnodemon

Pass-through observer for Rradmon: sits between an RNode's serial port and rnsd
(RNS `port = tcp://127.0.0.1`, TCP 7633), passes every byte unchanged, and
publishes what the RNode hears as Rradmon observer messages v1 over MQTT.

- Message contract: Rradmon `docs/spec-v0.md` §5 (sibling repo `..\Rradmon`).
  Rradmon is the source of truth; change the spec there first.
- One file, `rrnodemon.py`; Python 3.7+, pyserial, paho-mqtt 1.x and 2.x.
- Tests: `python -m unittest -v` (no hardware needed).
- Bench test: run it on the RNode's port with a config in the scratchpad
  (broker details from Rradmon's `secret/mqtt.env`), point an rnsd with
  `share_instance = No` at `tcp://127.0.0.1`, and check the observer in Rradmon.

## Rules

- Rrnodemon never writes to the RNode on its own; only rnsd's bytes go to it.
- Never commit broker credentials, local configs (`*.conf`), real positions or
  captures. Credentials live in Rradmon's git-ignored `secret/` folder.
- Commit locally; push only when the user says (approval is per push).
