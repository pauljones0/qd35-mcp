# Sunrise alarm and audio

The alarm controller deliberately separates the QD35's two Bluetooth roles:

- BLE carries lighting, volume, input, and clock commands.
- Classic A2DP carries music or internet radio from the Linux host.

The speaker must be paired and trusted once for A2DP. `qd35_set_alarm_audio_keepalive(true)` installs and enables a user service that checks the paired Classic endpoint every 30 seconds and reconnects it if needed. It never issues opcode `CD`, never removes the pairing, and never powers off the speaker.

On the tested QD35/BlueZ combination, a deliberate host-side disconnect changed BlueZ's `Paired` field back to `no` even though the trusted device record remained. The keepalive therefore attempts `connect` whenever the endpoint is disconnected instead of incorrectly requiring `Paired: yes`; BlueZ and the speaker re-established the ephemeral pairing and A2DP sink in 34 seconds during the live resilience check.

## Persistent scheduling

`qd35_create_sunrise_alarm` stores a mode-0600 JSON plan beneath the XDG state directory and installs systemd user services/timers beneath the XDG user-unit directory. The sunrise timer uses the alarm's IANA timezone and starts at `wake_time - sunrise_minutes`. When `bedtime` is supplied, a separate preparation timer runs on the preceding alarm night: for a Monday–Friday 07:00 wake alarm with bedtime 22:00, it fires Sunday–Thursday at 22:00. `Persistent=true` catches a missed firing after a reboot. The standalone runners work even after the MCP client that created the alarm exits.

Bedtime preparation synchronizes the clock, sets the 37-node RGB chain to zero, and,
when the optional display patch is configured, powers the complete front display
off. The CMS1640 driver's minimum nonzero level (`0`) remained too bright at bedside
distance, so the night state deliberately blanks the clock, EDIFIER logo, and other
symbols. It uses
the BLE control channel only: it does not select the audio input, change volume,
launch a player, or send a Classic Bluetooth command. The independently enabled
keepalive maintains the existing Classic connection overnight.

During a run it:

1. Synchronizes the speaker clock and requested 12/24-hour display.
2. Selects Bluetooth input without disconnecting Classic audio.
3. Ramps a validated static RGB light from 0 to the configured end brightness using
   a gamma-2.2 perceptual curve. Elapsed time remains linear, but physical output
   rises slowly at first so the apparent brightness does not jump at the beginning.
4. Applies the same perceptual curve to the patched front display's eight hardware
   levels (0–7), restoring the logo and status symbols at the beginning of sunrise.
5. Connects the already-paired Classic endpoint when audio is due.
6. Starts `mpv` on the QD35 PipeWire/PulseAudio sink at 50% software gain.
7. Ramps the speaker's hardware volume linearly from zero to the requested target;
   this is intentionally separate because the speaker's volume steps already use
   an audio-style response. The target is
   capped at 8/16; new alarms default to a bedside-safe 4/16.
8. Leaves a radio stream running until `qd35_stop_alarm` or
   `qd35_snooze_alarm` is called.

Lighting-only alarms exit after the ramp. Audio alarms remain active while the player runs. Stopping an alarm terminates its service/player but does not send a Bluetooth disconnect command.

## Snooze

`qd35_snooze_alarm(alarm_id, minutes=10)` stops the active systemd service and its
`mpv` child immediately, returns RGB to zero, and restores the fully dark night
state. It installs a one-shot user timer for 1–60 minutes. When the
timer fires, a dedicated snooze service performs a short two-minute RGB/front-display
and radio-volume fade instead of replaying the entire 30-minute sunrise. The regular
recurring alarm timer is unchanged, and neither snooze nor stop sends the speaker's
Bluetooth-disconnect command.

## Audio sources

Local files must resolve beneath `QD35_ALARM_MEDIA_DIR`. Remote streams require `QD35_ALARM_ALLOW_REMOTE_AUDIO=1` and an exact hostname in the comma-separated `QD35_ALARM_ALLOWED_HOSTS`; URLs must use HTTP(S), contain no embedded credentials, and resolve only to public addresses. This blocks loopback, private-network, link-local, multicast, reserved, and unspecified destinations.

An example suitable for personal listening is SomaFM's official Groove Salad MP3 stream:

```text
https://ice5.somafm.com/groovesalad-128-mp3
```

For that example, configure `QD35_ALARM_ALLOWED_HOSTS=ice5.somafm.com`.

Station availability and personal-use terms remain the station operator's responsibility. Prefer stable playlist URLs when a broadcaster provides them.

## Example request

```text
Create “Weekday Wake” for 07:00 America/Regina, Monday through Friday.
Turn RGB and the front display off at 22:00, then start a
30-minute warm-orange sunrise that reaches 100% at 08:00. Start Groove Salad during
the final 10 minutes and fade the speaker only to bedside volume 4.
```

The clock protocol returns only a success acknowledgement; it has no time readback. Audio requires the host to remain powered on with the user systemd manager, BlueZ, PipeWire, network, and `mpv` available.

The MCP persists the alarm's IANA timezone separately from the host process timezone. Ordinary `qd35_sync_clock` calls reuse that persisted zone, so a UTC container cannot overwrite a speaker configured for `America/Regina`. `QD35_TIMEZONE` remains an explicit operator override.
