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

## Media control

`C2` is a one-way AVRCP command: firmware does not emit a `C2` response frame. Waiting
for one caused a false timeout, so the MCP now uses a fire-and-forget transport write.
All four action payloads were sent live. During the play/pause test, `C3` playback state
changed from `03` to `0D` after play. Next/previous were transmitted, but a multi-track
source was not available for independent track-title verification.

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

