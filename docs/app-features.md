# Edifier Connect feature inventory for QD35

This inventory combines the model-specific Edifier Connect operation manual, the QD35 user manual, official product information, app definitions, and live firmware 1.3.0 readback.

| App function | Options/behavior | MCP status |
|---|---|---|
| Volume | Linked app/phone/speaker slider; live device range 0–16 | Changed value and restoration verified live |
| Input | Bluetooth, AUX, USB audio | All three selections and restoration verified live with delayed readback |
| EQ preset | Music, Game, Movie, DIY | All four selections and restoration verified live |
| DIY EQ | Six bands: 62, 250, 1k, 4k, 8k, 16k Hz; ±3 dB | Every band changed, read back, activated DIY, and restored live |
| DIY EQ reset | Restore custom bands to defaults | `45` changed all six modified bands to defaults live; original values and preset were restored |
| Lighting | Seven directly selectable modes; RGB on four, named modes on three; global brightness; speed where reported | Every effect and control shape changed, read back, and restored live |
| Lighting reset | Reset one effect's parameters | `72` changed a modified static effect live; original parameters and selected effect were restored |
| Extended/custom lighting | Gradient, Water-drop, Spiral, Multicolored; palette selection on compatible assets | Recovered from app 8.4.33/8.5.5; Spiral and the full 97-chunk Gradient were transferred, acknowledged, and identity-read back live |
| Audio decoding status | Active decoder/Hi-Res status | Raw `68` value plus app-confirmed QCM, Hi-Res, line Hi-Res, 192 kHz, and HWA logo combinations; idle hardware returned `00` |
| Media | Play, pause, previous, next | All payloads sent live; play changed playback-state readback; `C2` is one-way |
| Media metadata/events | Playback, title, and artist notifications | Retained, bounded event history with strict parsing; live `50 00 00 00` event captured |
| Built-in BGM | 32 app audio tracks across 10 categories, repeat/selection | Not bundled; these are app-local copyrighted media, not BLE-resident tracks |
| Clock | Sync to host, 12-hour or 24-hour format | Preserve, 12h, and 24h payloads acknowledged live; no clock readback exists |
| Clock/logo illumination | Stock firmware keeps both on at fixed maximum display-driver brightness while active | The optional patch can dim or blank the driver while Bluetooth stays active. A digits-only raw frame proved the physical wordmark remains illuminated by clock light; only whole-display off fully extinguishes it. |
| LDAC | 96 kHz, 48/44.1 kHz, disabled | 48/44.1 kHz and 96 kHz both verified by post-restart readback, then restored off |
| Device name/info | Rename; show address and firmware | Changed name, exact 35-byte D8 boundary, and restoration verified live |
| Bluetooth disconnect | Disconnect Classic audio | Live-verified behind dangerous-operation gate |
| Power off | Remote shutdown; no remote power-on | Live-verified behind dangerous-operation gate |
| Factory reset | Clear settings and pairing information | Live-verified with marked settings and fresh pairing |
| Firmware update | Signed model/version lookup, download, and BLE OTA | General updates are outside the MCP surface. Separately gated tools install the optional patch or restore the exact pinned 1.3.0 image. See [display firmware](display-firmware.md). |
| Manual/store/profile | App navigation and web content | Not speaker controls |

The 2023 app manual presents ten visual labels/modes. Firmware 1.3.0 directly reports seven selectable effect records in lighting group 1. Four downloadable animations live inside effect 7 (Extended), rather than being additional `6B` effect IDs. The MCP preserves that distinction and uses live-reported IDs.

Firmware disassembly separates the 37-node RGB chain from the CMS1640-driven clock,
status icons, and EDIFIER wordmark. The wordmark has its own grid bit and the clock
digits can be blanked internally, but no stock Bluetooth command reaches either
operation. Electrical separation does not provide optical isolation: the transparent
front assembly carries clock light into the wordmark. See
[firmware-analysis.md](firmware-analysis.md).

Primary references:

- [QD35 Edifier Connect operation manual](https://www.princeton.co.jp/sites/default/files/support/manual/QD35_APPmanual.pdf)
- [QD35 official product page](https://www.edifier.com/p/wireless-speakers/qd35)
- [QD35 official support page](https://www.edifier.com/s/wireless-speakers/qd35)
- [QD35 user manual](https://usa-oss.edifier.com/Uploads/images/2023/03/21/2023032109390916793627498265.pdf)

The user-supplied Edifier Connect 8.5.5 APK was obtained and inspected locally. Its archive and all nine embedded DEX SHA-1 values verified; the APK SHA-256 is `8e57d68ea33325428755adb13a5f2dd90bb1b3cbe45bd9777158eb9a4fa17933`. Targeted comparison with official 8.4.33 confirmed the relevant command builders and parsers, including `68`, `72`, `78`–`7B`, `C3`, media events, D8 capabilities, and rename modes. The project independently implements protocol facts; it does not redistribute proprietary APK code or media.

