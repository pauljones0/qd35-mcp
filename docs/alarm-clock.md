# Sunrise alarm and audio

The alarm controller deliberately separates the QD35's two Bluetooth roles:

- BLE carries lighting, volume, input, and clock commands.
- Classic A2DP carries music or internet radio from the Linux host.

The speaker must be paired and trusted once for A2DP. Bedtime preparation hands the
keepalive an audio lease for that alarm and speaker. The worker reconnects only while
an unexpired lease calls for audio, or while an operator has explicitly enabled
`qd35_set_alarm_audio_keepalive(true)` for the current user session. The worker
checks for expiry during its normal polling loop. If an alarm process crashes,
its lease still expires and the worker releases its Classic connection.

Normal cleanup disconnects Classic only if the finishing run still owns its token.
Snooze extends the lease for the retry, and a newer run gets a new token. An explicit
manual keepalive takes precedence over automatic release. Stopping or deleting an
alarm cancels its lease without sending a disconnect. The worker stays idle when
there are no claims. No pairing or speaker power changes are part of this workflow.

Two simultaneous runs targeting the same speaker are rejected at runtime. This also
covers overlapping manual starts and snoozes. Schedule creation can still save
overlapping plans. Physical BLE transactions lock one speaker through write and
readback, so other same-user MCP processes can work between alarm steps.

On the tested QD35/BlueZ combination, a deliberate host-side disconnect changed BlueZ's `Paired` field back to `no` even though the trusted device record remained. The keepalive therefore attempts `connect` whenever the endpoint is disconnected instead of incorrectly requiring `Paired: yes`; BlueZ and the speaker re-established the ephemeral pairing and A2DP sink in 34 seconds during the live resilience check.

## Persistent scheduling

`qd35_create_sunrise_alarm` stores a mode-0600 JSON plan beneath the XDG state directory and installs systemd user services/timers beneath the XDG user-unit directory. The sunrise timer uses the alarm's IANA timezone and starts at `wake_time - sunrise_minutes`. When `bedtime` is supplied, a separate preparation timer uses the last occurrence of that clock time before each sunrise starts. For a Monday–Friday 07:00 wake alarm with bedtime 22:00, it fires Sunday–Thursday at 22:00.

Persistent timers can fire after a missed start. On systemd 252 and newer, the runner uses the timer's identity to catch up to the current point in the scheduled sunrise and keep the original audio cutoff. An expired sunrise is skipped, and the bedtime job only runs during its overnight window. `qd35_run_alarm_now` still starts a fresh ramp. The standalone runners work after the MCP client exits.

Bedtime preparation synchronizes the clock, sets the 37-node RGB chain to zero, and,
when the optional display patch is configured, powers the complete front display
off. The CMS1640 driver's minimum nonzero level (`0`) remained too bright at bedside
distance, so the night state deliberately blanks the clock, EDIFIER logo, and other
symbols. It does not select the audio input, change volume, launch a player, or send
the speaker's BLE disconnect command. For alarms with audio it enables the host-side
Classic keepalive at bedtime, so any unavoidable firmware connection prompt happens
before sleep rather than when the sunrise begins. Neither inspected Edifier Connect
version exposes a prompt-tone disable command, so preventing a morning reconnect is
the only verified silent approach.

During a run it:

1. Attempts to synchronize the speaker clock and requested 12/24-hour display.
2. Ramps a validated static RGB light from 0 to the configured end brightness using
   a gamma-2.2 perceptual curve. Elapsed time remains linear, but physical output
   rises slowly at first so the apparent brightness does not jump at the beginning.
3. Applies the same perceptual curve to the patched front display's eight hardware
   levels (0–7), restoring the logo and status symbols at the beginning of sunrise.
4. Checks the Classic endpoint when audio is due, mutes hardware volume, and selects
   Bluetooth input if needed. It starts `mpv` at 50% software gain and checks that
   an audio stream is actually linked to the named QD35 PipeWire sink before fading in.
   If playback fails to start or exits during sunrise, the runner makes up to three
   attempts in total.
5. Ramps the speaker's hardware volume linearly from zero to the requested target;
   this is intentionally separate because the speaker's volume steps already use
   an audio-style response. The target is
   capped at 8/16; new alarms default to a bedside-safe 4/16.
6. Leaves a radio stream running until `qd35_stop_alarm` or
   `qd35_snooze_alarm` is called, or until the configured post-wake window expires.
7. By default, 30 minutes after wake time it stops the player and releases the host's
   Classic connection if the finishing alarm still owns it.

Lighting-only alarms exit after the ramp. The runner logs clock, lighting, and audio
failures separately. Audio still requires a verified mute and Bluetooth input.
Stopping an alarm terminates its player without sending a Bluetooth disconnect command.

## Snooze

`qd35_snooze_alarm(alarm_id, minutes=10)` stops the active systemd service and its
`mpv` child immediately, returns RGB to zero, and restores the fully dark night
state. It installs a one-shot user timer for 1–60 minutes. During the sunrise ramp,
an unsolicited volume, input, EQ, or lighting notification is checked against a fresh
speaker read. A confirmed change invokes the same flow with the configured snooze
duration (ten minutes by default). Playback-only changes and button presses that
leave the setting unchanged are ignored because they cannot be distinguished from
automatic or duplicate notifications. Each snooze rearms the timer. When it fires,
a dedicated snooze service performs a short two-minute RGB/front-display
and radio-volume fade instead of replaying the entire 30-minute sunrise. The regular
recurring alarm timer is unchanged, and neither snooze nor stop sends the speaker's
Bluetooth-disconnect command.

## Audio sources

Local files must resolve beneath `QD35_ALARM_MEDIA_DIR` when the alarm is created.
Remote streams require `QD35_ALARM_ALLOW_REMOTE_AUDIO=1` and an exact hostname in
`QD35_ALARM_ALLOWED_HOSTS`. The URL is checked for HTTP(S), absence of credentials,
and public DNS results when saved. The player resolves the URL again at runtime
and can follow redirects, so allow only trusted station hosts.

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
30-minute warm-orange sunrise that reaches 100% at 07:00. Start Groove Salad during
the final 10 minutes and fade the speaker only to bedside volume 4.
```

The clock protocol returns only a success acknowledgement; it has no time readback.
Physical-control snooze uses the same unsolicited state notifications consumed by
Edifier Connect. Volume-dial and lighting-dial events were captured live from the
tested speaker. Input and EQ button events have yet to be confirmed live. Audio
requires the host to remain powered on with
the user systemd manager, BlueZ, PipeWire, network, and `mpv` available.

The MCP persists the alarm's IANA timezone separately from the host process timezone. Ordinary `qd35_sync_clock` calls reuse that persisted zone, so a UTC container cannot overwrite a speaker configured for `America/Regina`. `QD35_TIMEZONE` remains an explicit operator override.
