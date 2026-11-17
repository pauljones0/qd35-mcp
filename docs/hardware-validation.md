# Hardware validation

Validation date: 2026-11-17 UTC. Platform: Linux/BlueZ with Bleak. Target: Edifier QD35, firmware 1.3.0.

## Discovery and identity

- Live Edifier advertisement: company ID `0x07E0`, service UUID `a400`.
- GATT service and both command characteristics matched the QD35 app profile.
- `C9` returned `EDIFIER QD35`; `C6` returned `1.3.0`.
- The BLE control connection worked without pairing.

The advertisement has no local name, which explains why a name-based initial scan appeared negative. A broad UUID/manufacturer scan ruled out that measurement error.

## Read validation

Successful queries: capability block, firmware, name, Classic address, volume, input, EQ preset, six DIY bands, lighting group/effects, LDAC, playback status, audio status, and custom-light status. An app media-info event also arrived unsolicited, confirming the request matcher must ignore unrelated opcodes.

## Reversible write validation

Current values were written back and then queried:

| Operation | Result |
|---|---|
| Volume `67` | Reply matched; `66` readback unchanged |
| Input `62` | Success reply; `61` readback unchanged |
| EQ preset `C4` | Reply matched; `D5` readback unchanged |
| EQ band `44` | Success reply; `43` preserved the band record |
| Lighting `6B` | Full applied record echoed; `6A` readback unchanged |
| LDAC `49` | BLE session dropped and the advertiser remained absent during a 45-second broad scan; power-cycle/reconnect required |

Setter responses use `BB` on this firmware, not only `CC`, so reply prefix alone is not an acknowledgement verdict.

The LDAC negative was investigated with an unfiltered Bleak scan, BlueZ cache inspection, and a direct cached-address connection attempt. This ruled out the earlier name/UUID-filter pitfall. The MCP now skips an LDAC write when the requested mode already matches and reports an unknown outcome plus manual recovery after a real change.

## Safety and remaining validation

The user explicitly authorized destructive tests. Their final observed results are added here after the release validation run. Firmware update remains excluded because a failed OTA can brick hardware and is not needed for ordinary agent control.

Run unit/simulator checks with `uv run pytest`. Hardware tests are intentionally manual because they operate a real nearby speaker.

