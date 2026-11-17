# Hardware validation

Validation date: 2026-11-17 UTC. Platform: Linux/BlueZ with Bleak. Target: Edifier QD35, firmware 1.3.0.

## Discovery and identity

- Live Edifier advertisement: company ID `0x07E0`, service UUID `a400`.
- GATT service and both command characteristics matched the QD35 app profile.
- `C9` returned `EDIFIER QD35`; `C6` returned `1.3.0`.
- The BLE control connection worked without pairing.

The advertisement has no local name, which explains why a name-based initial scan appeared negative. A broad UUID/manufacturer scan ruled out that measurement error.

## Read validation

Successful queries: capability block, firmware, name, Classic address, volume, input, EQ preset, six DIY bands, lighting group/effects, LDAC, playback status, audio status, and custom-light status. An unsolicited media-info event (`50 00 00 00`) arrived after another query, proving that unrelated opcodes must be retained separately rather than discarded by request matching.

## Changed-value write validation

The following operations were changed to a different value, queried, and restored on
the physical speaker:

| Operation | Changed-value result |
|---|---|
| Volume `67` | 10 → 11 → 10; both readbacks matched |
| Input `62` | Bluetooth → AUX → USB → Bluetooth; every delayed readback matched |
| EQ preset `C4` | Music, Game, Movie, and DIY all selected; final Music restoration matched |
| EQ band `44` | All six bands changed to -0.5 dB, read back with DIY active, and restored to 0.0 dB |
| EQ reset `45` | Six modified bands reset to defaults; original bands and Music preset were restored |
| Lighting `6B` | All seven effects exercised with their real RGB/named-mode shape, brightness, and supported speeds; 18 changed/restored readbacks matched |
| Lighting reset `72` | Modified static parameters changed after reset; original parameters and selected breathing effect were restored |
| Rename `CA` | `EDIFIER QD35` → an exact 35-ASCII-byte boundary name → `EDIFIER QD35`; both readbacks matched |
| Clock `77` | Preserve, 12-hour, and 24-hour synchronization returned `01`; no independent clock readback exists |
| LDAC `49` | 48/44.1 kHz and 96 kHz were each confirmed by post-restart `48` readback, then restored off |

The final status confirmed volume 10, Bluetooth input, Music EQ, the original six EQ
gains, breathing effect, every original lighting color/mode and speed, 100% brightness,
the original device name, and LDAC off. Classic audio was connected afterward.

## Extended/custom-light transfer

The transfer implementation was exercised with two official product-223/firmware-1.3.0 assets:

| Asset | Live result |
|---|---|
| Spiral | 150 bytes, one acknowledged chunk; `78` readback returned compatibility asset ID `01` and palette `01`; Extended activated successfully |
| Gradient | 14,550 bytes, 97 acknowledged chunks; `78` readback returned asset ID `17` hex (catalog ID 23); Extended activated successfully |

`79` returned transfer limit 155 and capacity 15,360 bytes. The resulting 150-byte data chunks are wrapped in 163-byte v2 frames. Live `7A` acknowledgements used zero-based detail indexes: chunk sequences 1 and 2 returned `00 00` and `00 01`, and the repeated Gradient validation ended at detail 96 for sequence 97. The first live attempt exposed a real transport bug: Bleak's characteristic write limit remained cached at 20 bytes even after BlueZ negotiated MTU 512, so the frame was split and the speaker never acknowledged it. A deep dive checked the negotiated MTU, characteristic properties, raw write geometry, and app behavior before accepting the timeout. The transport now negotiates and verifies enough ATT capacity before `79`, sends the 163-byte frame in one write, correlates every acknowledgement, and refuses an unvalidated fragmented fallback; both transfers then completed.

Lost-ack retry behavior was also tested directly rather than inferred from the simulator. The exact Spiral sequence-1 frame was deliberately submitted twice. Firmware 1.3.0 returned the same successful `00 00` acknowledgement both times, accepted `7B 01`, and `78` reported asset `01` with palette `01`. This confirms that retransmitting an already accepted sequence is idempotent on the tested QD35. Gradient was then uploaded again and breathing restored.

The original custom slot was empty. After validation, the visible lighting effect and its parameters were restored to breathing. Gradient remains installed but inactive because `78` identifies an asset plus a detail/palette byte; it cannot read back an arbitrary prior binary for backup.

## Animation video calibration

Four generated type-1 animations were uploaded at low global brightness and recorded from the physical speaker:

- The 37-frame white address chase, using control value 100 for every frame, repeated in about 114 frames of a 30 fps recording: approximately 3.80 seconds per loop.
- RGB bars showed the expected channel order. Addresses 0–11 lit red along the lower-left run, 12–23 lit green through the lower-right/right-side run, and 24–36 lit blue through the remaining upper/left structures. Yellow/cyan boundary seams were optical diffusion between adjacent address blocks, not swapped bytes.
- The timing sweep was captured at a metadata-confirmed 240 fps. Its eight control values (5, 10, 20, 40, 80, 100, 125, and 640) repeated every 251 captured frames, or 1.046 seconds. Their nominal sum is 1.020 seconds. Together with the chase result, this supports milliseconds plus approximately 3 ms of fixed processing overhead per frame.
- The fourth component was swept through 0, 1, 16, 64, 128, and 255 with constant low red. The owner saw no change, and stabilized frame analysis found no repeatable cycle in brightness, RGB balance, or white/red ratio at the expected period. On firmware 1.3.0 it has no observable effect; it remains unnamed and authored assets force it to zero.

Because the original chase can begin mid-loop in a recording, a synchronized mapper was added for absolute indexing: long blue start marker, dim-white baseline, addresses 0–36 one at a time with a repeating six-color marker, and a long yellow end marker.

The synchronized mapper was then recorded for 48.98 seconds at 30 fps while the camera moved around the speaker. Four blue start intervals and five yellow boundary intervals were detected; consecutive blue starts were exactly 365 frames apart. The first near-front loop resolved all 37 addresses without wrap ambiguity. Its calibrated visible-emission centroids are published in `docs/address-map.md` and in the diagnostic catalog.

A structure-from-motion cross-check extracted 98 geometry views from the same recording. All 98 registered into one model with 2,803 points, mean track length 10.10, and 0.64-pixel mean reprojection error. A second registration included all 148 exact address-center observations and reached 246 registered images and 7,494 points, but singular bundle-adjustment warnings collapsed several added poses onto the same camera center. That second result was rejected for coordinates. The sound 98-view model establishes useful parallax, but the transparent light guides, occlusions, saturation, and moving visible centroid make hidden LED-package coordinates underdetermined from this recording. Public data therefore contains the reliable front-view emission map, not false-precision 3D points. A 3D refinement should record isolated addresses with all other lights off, include scale/fiducials, and cover deliberate fixed-angle arcs.

The generated `spiral_color_probe` was also transferred live as one 150-byte type-3 chunk. The speaker acknowledged sequence 1, completion succeeded, `78` read back Spiral's compatibility asset ID `01`, and Extended activated at 5% brightness. That first low-brightness observation made the motion look ring-only and produced an informal estimate of roughly 11 positions. After raising global brightness to 20%, the lower strip was plainly visible too, matching Edifier's manual: the center travels clockwise while the lower guide travels right-to-left. The earlier estimate is rejected as a measurement artifact, not recorded as an LED count.

A [published NEW-X teardown](https://www.ednchina.com/technews/22905.html) shows the QD35-equivalent lighting board and explicitly counts 20 physical RGB LED packages. The upload format nevertheless contains 37 RGBX entries. Physical packages, uploaded entries, and visible light-guide positions are therefore tracked as separate quantities. Exact type-3 payload sampling remains pending a stable recording of the unique-color `spiral_index_probe`.

## Media control

`C2` is a one-way AVRCP command: firmware does not emit a `C2` response frame. Waiting
for one caused a false timeout, so the MCP now uses a fire-and-forget transport write.
All four action payloads were sent live. During the play/pause test, `C3` playback state
changed from `03` to `0D` after play. Next/previous were transmitted, but a multi-track
source was not available for independent track-title verification.

## Alarm/audio host integration

The Classic QD35 endpoint was paired and trusted, and PipeWire exposed its 48 kHz stereo A2DP sink. A low-volume eight-second `mpv` smoke test played SomaFM's official Groove Salad MP3 stream through that sink and restored the original speaker volume afterward. A disposable systemd user timer launched the standalone alarm runner, synchronized the 12-hour `America/Regina` clock, selected Bluetooth input, applied its light step, exited successfully, and was removed.

The overnight keepalive was then tested by deliberately dropping only the host's Classic connection. BlueZ changed the device's ephemeral `Paired` state to `no`; an initial implementation incorrectly treated that as a reason not to reconnect. After removing that assumption, the enabled service reconnected, re-established pairing, and restored the A2DP endpoint in 34 seconds without opcode `CD`, pairing-record removal, or speaker power cycling.

A subsequent requirements audit found that the original sunrise service applied its low starting brightness only when the pre-wake ramp began. Optional bedtime preparation is now a distinct oneshot service/timer scheduled on the night preceding each wake day. Unit-generation and runner tests verify that this phase changes only clock/light state over BLE and does not touch input, volume, playback, or Classic audio. A daily live schedule was installed for a 22:00 bedtime, 07:30 light-ramp start, 07:50 audio-fade start, and 08:00 wake in `America/Regina`; its state and timezone files are mode 0600. The physical speaker accepted 12-hour clock synchronization and static `#FF6A20` at 3% brightness before the subsequent type-3 diagnostic.

Clock validation also exposed a host-context error: the coding container used UTC while the configured speaker zone is `America/Regina`. Timezone resolution is now persistent and IANA-based rather than inherited blindly from the process. A fresh process synchronized the physical speaker with 12-hour format; firmware returned success `01`.

## Disruptive-operation validation

The user explicitly authorized these tests:

| Operation | Live result |
|---|---|
| Audio disconnect `CD` | BLE dropped, Classic changed to disconnected, and its pairing record remained. BLE advertising returned after Classic reconnected. |
| Factory reset `07` | Returned `01`, dropped Classic, invalidated the old bond, and required re-pairing. Temporary markers—name `QD35 RESET TEST`, volume 11, Game EQ, and static `#123456` at 40%—all reset to defaults. |
| Power off `CE` | BLE dropped immediately; Classic changed to disconnected after its cached state expired, and an eight-second broad scan found no QD35 advertisement. Remote power-on is unavailable. |

Setter responses use `BB` on this firmware, not only `CC`, so reply prefix alone is not an acknowledgement verdict.

Input switching exposed a verification timing issue rather than a protocol failure.
Firmware acknowledged immediately but applied AUX after 0.75 seconds and USB and
Bluetooth after 1.25 seconds. The MCP now polls bounded readback for up to two seconds.

An LDAC change restarted both Bluetooth links. The first 96 kHz transition left BlueZ's
previous Classic bond unable to restore BLE advertising; removing the stale local bond
and pairing again recovered it, and `48` readback confirmed 96 kHz. Restoring LDAC off
reconnected with the new pairing without removal. No physical intervention was needed.
The MCP skips an LDAC write when the requested mode already matches and reports an
unknown outcome plus recovery guidance after a real change.

The live lighting sweep corrected an earlier modeling assumption: bytes labelled R/G/B
are not RGB for every effect. Ambient and magic-tides use light/deep selectors, extended
uses off/on, and only effects 3–6 use RGB. Brightness is global across all seven records.
The public schema, simulator, parser output, and verification logic now encode that.

An earlier raw same-value LDAC write also made the advertisement disappear, but the
speaker later recovered without any user action. That observation is recorded as
delayed automatic recovery, not a power-cycle.

## BlueZ 5.87 discovery crash

BlueZ 5.87 crashes when a UUID discovery filter matches an advertisement. The failure
was reproduced five times with both Bleak and stock `bluetoothctl`; the fault address
equalled the instruction pointer. This is [upstream BlueZ issue 2282](https://github.com/bluez/bluez/issues/2282), fixed by commit
`82af2be`. The transport deliberately performs an unfiltered BlueZ scan and applies the
`a400` UUID check in-process. The same broad scan found the QD35 without crashing while
Classic A2DP remained active.

## Safety and remaining validation

Firmware 1.3.0 is the version reported by this speaker and the version used by Edifier's current QD35 custom-light catalog. The app exposes OTA only when it has a signed, model-specific image; no newer QD35 image was established during this work. Firmware update remains excluded because the recovered opcode names do not establish image format, integrity validation, rollback, or recovery, and a failed OTA can brick the speaker.

Run unit/simulator checks with `uv run pytest`. Hardware tests are intentionally manual because they operate a real nearby speaker.

