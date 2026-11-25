# Hardware validation

Validation date: 2026-11-17 UTC. Platform: Linux/BlueZ with Bleak. Target: Edifier QD35, firmware 1.3.0.

## Discovery and identity

- Live Edifier advertisement: company ID `0x07E0`, service UUID `a400`.
- GATT service and both command characteristics matched the QD35 app profile.
- `C9` returned `EDIFIER QD35`; `C6` returned `1.3.0`.
- The BLE control connection worked without pairing.

The advertisement has no local name, which explains why a name-based initial scan appeared negative. A broad UUID/manufacturer scan ruled out that measurement error.

## Optional display-patch OTA and controls

### Separator-preserving revision

The 40,932-byte image with SHA-256
`b2354c561dd02e73e8d8f25599b956d69e58742421f8edc13101a3f7f0786c8a` transferred in
320 chunks. The speaker answered the new `D4` revision probe after Classic audio
reconnected. Firmware lineage, Bluetooth input, and the existing Extended asset
identity were unchanged. Classic reconnection again raised volume to 4/16,
which was restored to 1/16 and verified. Emulation checked the actual display
frame for clock on, with the API's `logo` and `symbols` masks off.

The owner observed the following states during a stepped live test on
2026-11-19 UTC. The RGB lighting was off and front-display brightness
was fixed at 2 of 7.

| State | Observed | Evidence |
|---|---|---|
| Driver off | Clock and logo both dark | Live observation |
| Driver on, `C0`–`C7` all zero | Logo only | Live observation |
| Raw digits-only frame | Clock and logo | Live observation |
| `60 40 40 40 40 40 40 40` | Horizontal line in the clock area and logo | Owner photo |
| `00 00 00 00 00 00 80 80` | Small striped indicator and logo | Owner photo |
| `FF FF FF FF FF FF FF FF` | All six digits, both separators, line, status icons and logo lit | Owner close-up of the clock and direct observation of the logo |

In a second live test, the raw `C0`–`C7` frame stayed at zero while the display
remained enabled. The owner observed a very dim logo at brightness 0 and a
bright logo at brightness 7. The clock stayed blank at both levels. This
establishes that the wordmark follows the display driver's brightness setting.
The logo also stayed lit with all 64 exposed bits set at brightness 2. No
single exposed bit can be a simple inverted logo switch, since the logo stayed
lit with all exposed bits clear and with all exposed bits set.

After the owner switched the speaker off at the rear and powered it back on,
the wordmark was still visible with `C0`–`C7` zeroed at brightness 0. Offline
replay of the installed revision's startup display routine showed it sending
16 zero bytes followed by redraws of only `C0`–`C7`. That makes stale data in
`C8`–`CF` an unlikely explanation. The live test had no bus capture, so it cannot
establish the electrical connection of the logo.

The field named `logo` masks the horizontal-line group. No tested segment
pattern independently extinguished the physical wordmark. The runner exited
successfully after restoring the prior RGB settings and resetting the display to
firmware defaults. The BLE raw commands did not access `C8`–`CF` directly, and
the tests did not measure PCB voltages.

### Stock rollback, 2026-11-19 UTC

The stock 1.3.0 OTA sent 40,471 bytes in 317 blocks. The device requested no
retries and sent no terminal reply during the three-second observation window.
The same `D4` payload returned `01 06 FF 6A 20 0A FF` before rollback and
`01 06 FF 6A 20 00 FF` after rollback. A fresh `6A` query confirmed the latter
reply reflected a brightness change from 10 to 0. Emulation reproduced both
branches: the patch only sets the reply flag and stock stores the payload's
brightness field before replying. This verifies that the stock command handler
is active. The transport cannot read back the installed flash or rule out
unreferenced bytes from the old patch in its last sector.

The original brightness of 10 and volume of 4/16 were verified after testing.
The full lighting readback, input, LDAC setting, EQ preset, speaker name, version
and Extended asset identity matched the pre-rollback snapshot.
The host's Classic audio connection dropped. BLE controls remained reachable.
Classic reconnect and pairing attempts timed out or failed to reach the speaker,
and a targeted Classic inquiry did not find it.

### Patch reinstallation through MCP, 2026-11-19 UTC

A live stdio MCP client called `qd35_install_display_firmware` on the stock
1.3.0 speaker. The server fetched and verified Edifier's pinned image and sent
the 40,932-byte patch build in 320 blocks with zero retries. On reconnect,
`D4` acknowledged a brightness marker of 9 while an independent `6A` query
still showed the original brightness 10. The unsupported `D5` control elicited
no reply while the link was up. A fresh connection then read ordinary status
successfully. The before and after settings snapshots matched.

### Masked-update revision, 2026-11-18 UTC

The 40,936-byte image with SHA-256
`02eb94086be44eb36e7bb5514bdcb25fce9b4726de92950646cebce7605f3394` was sent in
320 OTA chunks. The Classic audio link dropped during the update. A 30-second
BLE scan found no advertisement until the host reconnected Classic audio, after
which the speaker returned its expected identity and firmware lineage 1.3.0.

The new `D3` command returned the ordinary lighting payload `01 06 FF 6A 20 0A FF`
for brightness-only and power-only updates. A zero-mask `D3` probe got the same
reply and the adjacent unsupported `D4` opcode got no reply. That comparison
confirms the new private command is executing. BLE provides no readback for the
private settings. Brightness changes and display extinction for this revision
still need optical confirmation.

The display was returned to its firmware defaults. Ordinary lighting and input
settings were stable across the command probes. Classic audio reconnection raised
the observed volume from 1/16 to 4/16. The original 1/16 was restored and verified
with a volume query. The live identity and state test passed, and the host
released its Classic audio connection afterward.

### Earlier revisions

The exact official 1.3.0 lighting-MCU image was patched deterministically from
40,471 to 40,880 bytes and transferred over the app-confirmed ordinary OTA v2 path.
The speaker requested 128-byte blocks, producing 320 acknowledged chunks. A short
post-restart scan initially missed the advertisement; BlueZ still held the exact
A400/manufacturer record, and a 30-second broad scan found it again at -61 dBm. This
ruled out a failed boot rather than treating the first negative as dispositive.

The first private effect-zero command returned the ordinary `6B` lighting-state
payload `01 07 01 00 00 14 FF`. That generic response is not independent display
readback and must not by itself be treated as proof that a requested mask was
physically applied. The live matrix exercised clock, logo, symbol, whole-display,
manual-brightness 0/3/7, raw-grid, reset, and lighting-control-follow commands. The
whole-display-off result and subsequent restoration were visually confirmed; the
other commands returned the same valid but generic response.

With follow mode active, ordinary lighting brightness was changed to 0, then 100,
then restored to the original 20; every `6A` readback matched. A final status read
still returned firmware 1.3.0, volume 11/16, Bluetooth input, Extended lighting, and
20% global lighting brightness. No audio-disconnect command was issued.
The RTC was finally resynchronized at 02:04:20 CST (`America/Regina`) in 12-hour
mode, returning `01`; all display groups/dial-follow were reasserted afterward.

A later bedside check found that brightness level 0 was still too bright and that
the EDIFIER wordmark remained visibly illuminated while the clock mask stayed on,
despite a fresh `logo=false` acknowledgement. Repeating the command did not change
that observation. Setting whole-display power false immediately made every front
segment dark, independently confirming that the installed hook executes. The alarm's
night state therefore uses whole-display off rather than claiming a visually verified
clock-only/logo-off state.

That targeted grid test was subsequently completed. Raw mode requested the current six
time digits into grid bytes `1..7` while byte `0` and every non-digit bit remained
zero (`00 1E 19 03 1F 3D 27 1F` for `02:35:51`). The EDIFIER wordmark was still
visible. An offline replay later confirmed the expected RAM transaction through
the bit-banged GPIO and found that the earlier image still sent maximum
brightness between each frame write and the requested minimum. The earlier
visual test did not establish what caused the residual. A corrected
400-byte patch also fixed the clock-off mask inversion and enforced a strict
digits-only framebuffer. It transferred as 320/320 acknowledged 128-byte chunks with
SHA-256 `83ef705bc545db3b6054a48add9e310ace886fbb76312abe7204229c35891b5b`.
After the raw test, RGB brightness zero was independently read back and the complete
front display was turned off for the night.

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
- The reserved byte was swept through 0, 1, 16, 64, 128, and 255 with constant low red. The owner saw no change, and stabilized frame analysis found no repeatable cycle in brightness, RGB balance, or white/red ratio at the expected period. Later firmware analysis proved that the type-1 renderer never loads it; authored assets force it to zero.

Because the original chase can begin mid-loop in a recording, a synchronized mapper was added for absolute indexing: long blue start marker, dim-white baseline, addresses 0–36 one at a time with a repeating six-color marker, and a long yellow end marker.

The synchronized mapper was then recorded for 48.98 seconds at 30 fps while the camera moved around the speaker. Four blue start intervals and five yellow boundary intervals were detected; consecutive blue starts were exactly 365 frames apart. The first near-front loop resolved all 37 addresses without wrap ambiguity. Its calibrated visible-emission centroids are published in `docs/address-map.md` and in the diagnostic catalog.

A structure-from-motion cross-check extracted 98 geometry views from the same recording. All 98 registered into one model with 2,803 points, mean track length 10.10, and 0.64-pixel mean reprojection error. A second registration included all 148 exact address-center observations and reached 246 registered images and 7,494 points, but singular bundle-adjustment warnings collapsed several added poses onto the same camera center. That second result was rejected for coordinates. The sound 98-view model establishes useful parallax, but the transparent light guides, occlusions, saturation, and moving visible centroid make hidden LED-package coordinates underdetermined from this recording. Public data therefore contains the reliable front-view emission map, not false-precision 3D points. A 3D refinement should record isolated addresses with all other lights off, include scale/fiducials, and cover deliberate fixed-angle arcs.

The generated `spiral_color_probe` was also transferred live as one 150-byte type-3 chunk. The speaker acknowledged sequence 1, completion succeeded, `78` read back Spiral's compatibility asset ID `01`, and Extended activated at 5% brightness. That first low-brightness observation made the motion look ring-only and produced an informal estimate of roughly 11 positions. After raising global brightness to 20%, the lower strip was plainly visible too, matching Edifier's manual: the center travels clockwise while the lower guide travels right-to-left. The earlier estimate is rejected as a measurement artifact, not recorded as an LED count.

A [published NEW-X teardown](https://www.52audio.com/archives/167663.html) counts 20 physical RGB packages on the lower display PCB. That count covers one board. The international QD35 firmware sends 37 RGB words and the synchronized recording resolved 37 illuminated positions, 20 below the front panel and 17 around the mid-bass driver. The type-3 firmware path reads only the first RGB color and generates the path on the device.

## Media control

`C2` is a one-way AVRCP command: firmware does not emit a `C2` response frame. Waiting
for one caused a false timeout, so the MCP now uses a fire-and-forget transport write.
All four action payloads were sent live. During the play/pause test, `C3` playback state
changed from `03` to `0D` after play. Next/previous were transmitted, but a multi-track
source was not available for independent track-title verification.

## Alarm/audio host integration

The Classic QD35 endpoint was paired and trusted, and PipeWire exposed its 48 kHz stereo A2DP sink. A low-volume eight-second `mpv` smoke test played SomaFM's official Groove Salad MP3 stream through that sink and restored the original speaker volume afterward. A disposable systemd user timer launched the standalone alarm runner, synchronized the 12-hour `America/Regina` clock, selected Bluetooth input, applied its light step, exited successfully, and was removed.

The overnight keepalive was then tested by deliberately dropping only the host's Classic connection. BlueZ changed the device's ephemeral `Paired` state to `no`; an initial implementation incorrectly treated that as a reason not to reconnect. After removing that assumption, the enabled service reconnected, re-established pairing, and restored the A2DP endpoint in 34 seconds without opcode `CD`, pairing-record removal, or speaker power cycling.

A subsequent requirements audit found that the original sunrise service applied its low starting brightness only when the pre-wake ramp began. Optional bedtime preparation is now a distinct oneshot service/timer scheduled on the night preceding each wake day. It changes clock/light state over BLE without touching input, volume, or playback; for audio alarms it also establishes the host's Classic connection at bedtime and maintains it overnight. The initial daily live schedule used a 22:00 bedtime, 07:30 light-ramp start, 07:50 audio-fade start, and 08:00 wake in `America/Regina`; the wake target was subsequently moved to 09:00 with an 08:30 ramp and 08:50 audio start. Its state and timezone files are mode 0600. The physical speaker accepted 12-hour clock synchronization and static `#FF6A20` at 3% brightness before the subsequent type-3 diagnostic.

The first live 09:00 alarm exposed two host-integration failures. `mpv` honored its
native PipeWire default rather than `PULSE_SINK`, so the PopTron stream was active on
the HDMI sink instead of the QD35. An explicit zero-volume route probe attached the
corrected command to `bluez_output.64_68_76_64_37_0D.1` while the stale process
remained attached to HDMI, independently confirming the cause. The 08:30 startup also
coincided with a Classic reconnect 43 seconds later and an audible device prompt. The
runner now skips a redundant Bluetooth-input write, mutes before any necessary source
selection, establishes Classic at bedtime, and disables its keepalive plus disconnects
the host 30 minutes after wake.

A live initialized BLE listener then captured physical side-control notifications:
the volume dial emitted `66 10 04` and `66 10 00`, while the lighting dial emitted
three `6B` settings at 20%, 10%, and 0%. Pressing the lighting control at zero emitted
another `6B` setting even though the value did not change, confirming button detection
does not depend on a visible brightness change. The current alarm watcher requires
a fresh readback of a changed control value, so a same-value press is ignored.
See [alarm snooze behavior](alarm-clock.md#snooze). The initial uninitialized listener saw only media
status; running the normal D8/state initialization before listening was required to
reproduce the app's event behavior. Input/EQ/playback button notifications remain to
be exercised live, although their app-confirmed state opcodes are included in the
watcher.

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

During the November 20 sunrise, a Bluetooth trace showed gradual RGB brightness
commands and a hardware volume write to 5 during the audio fade. The owner
reported the flash at each volume increase. In firmware replay, `+VOL+005`
replaced the lit 37-pixel static frame with a six-pixel volume indicator and
restored the static frame after the indicator expired. The alarm now fades the
player's software gain while leaving the speaker at its target volume. The
replacement fade passed an mpv test with null audio output and a scheduler
regression test. Its next physical sunrise remains to be observed.

Firmware 1.3.0 is the version reported by the tested speaker. The signed OTA lookup
returned the official 40,471-byte Cortex-M image and offered no newer version at
the time of testing. The MCP exposes only fixed-purpose, gated patch install and
stock restore against the pinned 1.3.0 source. General OTA and power-loss recovery
are outside the supported workflow. See [display firmware](display-firmware.md)
and [firmware analysis](firmware-analysis.md).

Run unit/simulator checks with `uv run pytest`. Hardware tests are intentionally manual because they operate a real nearby speaker.

