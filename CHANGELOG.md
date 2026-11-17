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
- Length-aware D8 capability decoding, device-fingerprint verification before writes, and the
  firmware-advertised 35-byte rename limit.
- Retained, bounded notification history with strict media/title/artist event parsing.
- Audio-decoding and custom-light status, custom EQ reset, per-effect lighting reset, and a
  capability-gated lighting timer surface.
- Hash-pinned official Extended-animation transfers with palette support, negotiated MTU and
  chunk geometry, acknowledgements, bounded retries, abort handling, and identity readback.
- Live one-chunk Spiral and 97-chunk Gradient transfers, exact-boundary rename, EQ reset, and
  lighting reset validation against firmware 1.3.0.
- Live duplicate-sequence validation confirming custom-light lost-ack retries are idempotent on
  firmware 1.3.0, followed by successful Gradient and visible-light restoration.
- Validated diagnostic-animation compiler with 37-address chase, RGB bars, timing sweep, and
  fourth-component sweep using the type-1 frame-sequence interpreter rather than Spiral type 3.
- Persistent timezone-aware sunrise alarms backed by systemd user timers, gradual light and
  speaker-volume ramps, local/public-stream audio through `mpv`, and an explicit overnight
  Classic-audio reconnect service that never sends the speaker disconnect command.
- Live Saskatoon 12-hour clock sync, Classic pairing/PipeWire sink validation, systemd alarm
  runner smoke test, and low-volume official SomaFM stream playback.
- Live keepalive fault injection uncovered BlueZ's ephemeral `Paired: no` transition after a
  host disconnect; the reconnect loop now ignores that misleading gate and restored A2DP in 34s.

