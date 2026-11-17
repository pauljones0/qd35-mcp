# Changelog

## 0.1.0 - 2026-11-17

- Initial local stdio MCP server.
- QD35 discovery, status, volume, input, EQ, lighting, media, clock, LDAC, rename, disconnect, shutdown, and reset operations.
- Deterministic simulator and protocol fixtures from QD35 firmware 1.3.0.
- Readback verification and separate safety gates for ordinary and disruptive writes.
- Live changed-value verification for volume, all inputs, EQ, lighting, LDAC, rename,
  clock sync, audio disconnect, factory reset, and power off.
- In-process UUID filtering to avoid the BlueZ 5.87 filtered-discovery crash.
- Bounded input readback polling and one-way AVRCP media-command handling.
- Adversarial frame/parser fuzzing, bounded notification buffering, serialized lifecycle cleanup,
  and unknown-outcome handling for every ambiguous write failure.
- Effect-specific QD35 lighting controls, including named modes and global brightness behavior.
- Live changed-value/readback/restoration across every EQ band, every lighting effect, and LDAC
  96 kHz; clock and media results now distinguish acknowledgement from independent readback.

