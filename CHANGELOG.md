# Changelog

## Unreleased

- Confirm physical snooze with fresh speaker readback and rearm the snooze timer on every retry.
- Verify PipeWire audio routing before the speaker volume rises. Retry failed playback, preserve
  the scheduled cutoff after a late start, and skip expired bedtime and wake windows.
- Coordinate BLE transactions across processes and give every alarm an expiring Classic-audio
  lease. Expired owners stop reconnecting, and an old run cannot disconnect a newer one.
- Keep the existing 39 MCP tool schemas, organize the adapters by function, and add an offline
  `qd35://guide` resource. Separate the alarm runner from the MCP adapter.

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
- Persistent IANA timezone resolution prevents UTC containers from shifting the speaker clock;
  alarms reinforce their named zone and clock results report the zone actually serialized.
- Optional bedtime preparation uses a separate preceding-night timer to apply the low overnight
  light state without selecting input, changing volume, starting playback, or touching Classic audio.
- Bedtime and snooze now blank the complete patched front display after live bedside observation
  found the CMS1640 minimum nonzero level too bright.
- Optional exact-hash display firmware adds clock-digit, logo-grid, status-symbol,
  whole-display, brightness 0–7, right-side-lighting-control, and raw CMS1640 control.
- Bundled original 400-byte patch, deterministic stock-hash patcher, exact-stock rollback,
  recovered OTA v2 transport, and fixed-purpose gated MCP install/restore tools.
- Live 320-chunk patched-image OTA plus semantic, brightness, raw-grid, reset, dial-follow,
  normal lighting readback, and ordinary status-continuity validation on firmware 1.3.0.
- Bedtime alarms now blank RGB and the complete front display; sunrise restores and ramps
  both RGB 0–100 and the patched display 0–7.
- Bedside radio defaults to hardware volume 4/16 with 50% player gain and is capped at 8/16.
- Added persistent systemd-backed snooze with immediate darkening and a short two-minute
  light/audio fade after the requested 1–60 minute delay.
- Corrected the patch's clock-off mask and forced a true digits-only framebuffer when logo and
  symbols are disabled. A live raw-frame test proved the physical EDIFIER wordmark still catches
  light from clock digits, so MCP results disclose that only whole-display off provides verified
  logo extinction.
- Sunrise RGB and patched-display brightness now use a gamma-2.2 perceptual curve while radio
  volume retains its separate linear device-step fade.
- Fixed alarm radio routing by selecting the exact QD35 native PipeWire device instead of a
  `PULSE_SINK` hint ignored by `mpv`; added physical-control snooze, redundant-input suppression,
  bedtime keepalive activation, and a default 30-minute post-wake player/Classic release.

