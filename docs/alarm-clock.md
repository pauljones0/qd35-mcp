# Sunrise alarm and audio

The alarm controller deliberately separates the QD35's two Bluetooth roles:

- BLE carries lighting, volume, input, and clock commands.
- Classic A2DP carries music or internet radio from the Linux host.

The speaker must be paired and trusted once for A2DP. `qd35_set_alarm_audio_keepalive(true)` installs and enables a user service that checks the paired Classic endpoint every 30 seconds and reconnects it if needed. It never issues opcode `CD`, never removes the pairing, and never powers off the speaker.

On the tested QD35/BlueZ combination, a deliberate host-side disconnect changed BlueZ's `Paired` field back to `no` even though the trusted device record remained. The keepalive therefore attempts `connect` whenever the endpoint is disconnected instead of incorrectly requiring `Paired: yes`; BlueZ and the speaker re-established the ephemeral pairing and A2DP sink in 34 seconds during the live resilience check.

## Persistent scheduling

`qd35_create_sunrise_alarm` stores a mode-0600 JSON plan beneath the XDG state directory and installs a systemd user service/timer beneath the XDG user-unit directory. The timer uses the alarm's IANA timezone and starts at `wake_time - sunrise_minutes`. `Persistent=true` catches a missed firing after a reboot. The standalone runner works even after the MCP client that created the alarm exits.

During a run it:

1. Synchronizes the speaker clock and requested 12/24-hour display.
2. Selects Bluetooth input without disconnecting Classic audio.
3. Ramps a validated static RGB light from the start to end brightness.
4. Connects the already-paired Classic endpoint when audio is due.
5. Starts `mpv` on the QD35 PipeWire/PulseAudio sink.
6. Ramps the speaker's hardware volume from zero to the requested target.
7. Leaves a radio stream running until `qd35_stop_alarm` is called.

Lighting-only alarms exit after the ramp. Audio alarms remain active while the player runs. Stopping an alarm terminates its service/player but does not send a Bluetooth disconnect command.

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
Start a 30-minute warm-orange sunrise at brightness 1, finish at 80,
start Groove Salad during the final 10 minutes, and fade speaker volume to 8.
```

The clock protocol returns only a success acknowledgement; it has no time readback. Audio requires the host to remain powered on with the user systemd manager, BlueZ, PipeWire, network, and `mpv` available.

The MCP persists the alarm's IANA timezone separately from the host process timezone. Ordinary `qd35_sync_clock` calls reuse that persisted zone, so a UTC container cannot overwrite a speaker configured for `America/Regina`. `QD35_TIMEZONE` remains an explicit operator override.
