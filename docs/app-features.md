# Edifier Connect feature inventory for QD35

This inventory combines the model-specific Edifier Connect operation manual, the QD35 user manual, official product information, app definitions, and live firmware 1.3.0 readback.

| App function | Options/behavior | MCP status |
|---|---|---|
| Volume | Linked app/phone/speaker slider; live device range 0–16 | Changed value and restoration verified live |
| Input | Bluetooth, AUX, USB audio | All three selections and restoration verified live with delayed readback |
| EQ preset | Music, Game, Movie, DIY | Music/Game change and restoration verified live |
| DIY EQ | Six bands: 62, 250, 1k, 4k, 8k, 16k Hz; ±3 dB | Changed band value, DIY activation, and restoration verified live |
| Lighting | Effect, RGB/palette, brightness; speed on supported effects | Effect, RGB, brightness, speed, and restoration verified live |
| Media | Play, pause, previous, next | All payloads sent live; play changed playback-state readback; `C2` is one-way |
| Built-in BGM | 32 app audio tracks across 10 categories, repeat/selection | Not bundled; these are app-local copyrighted media, not BLE-resident tracks |
| Clock | Sync to host, 12-hour or 24-hour format | 24-hour synchronization acknowledged live |
| LDAC | 96 kHz, 48/44.1 kHz, disabled | off ↔ 48/44.1 kHz verified live; change restarts Bluetooth and required re-pairing on firmware 1.3.0 |
| Device name/info | Rename; show address and firmware | Changed name and restoration verified live |
| Bluetooth disconnect | Disconnect Classic audio | Live-verified behind dangerous-operation gate |
| Power off | Remote shutdown; no remote power-on | Live-verified behind dangerous-operation gate |
| Factory reset | Clear settings and pairing information | Live-verified with marked settings and fresh pairing |
| Firmware update | Conditional OTA flow | Deliberately excluded from the agent surface |
| Manual/store/profile | App navigation and web content | Not speaker controls |

The 2023 app manual presents ten visual labels/modes. Firmware 1.3.0 directly reports seven selectable effect records in lighting group 1. The MCP uses live-reported IDs instead of inventing three additional writable IDs.

Primary references:

- [QD35 Edifier Connect operation manual](https://www.princeton.co.jp/sites/default/files/support/manual/QD35_APPmanual.pdf)
- [QD35 official product page](https://www.edifier.com/p/wireless-speakers/qd35)
- [QD35 official support page](https://www.edifier.com/s/wireless-speakers/qd35)
- [QD35 user manual](https://usa-oss.edifier.com/Uploads/images/2023/03/21/2023032109390916793627498265.pdf)

The user-supplied `EC_v8.5.5-jiagu_1794042055.apk` was identified through Drive metadata but not downloaded because its 288,639,520-byte size exceeds the connector's 256 MiB limit and the direct URL requires authentication. Protocol research used official Edifier Connect 8.4.33 plus a separately obtained 8.3.43 package and live hardware validation.

