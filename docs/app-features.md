# Edifier Connect feature inventory for QD35

This inventory combines the model-specific Edifier Connect operation manual, the QD35 user manual, official product information, app definitions, and live firmware 1.3.0 readback.

| App function | Options/behavior | MCP status |
|---|---|---|
| Volume | Linked app/phone/speaker slider; live device range 0–16 | Implemented and live-verified |
| Input | Bluetooth, AUX, USB audio | Implemented and live-verified |
| EQ preset | Music, Game, Movie, DIY | Implemented and live-verified |
| DIY EQ | Six bands: 62, 250, 1k, 4k, 8k, 16k Hz; ±3 dB | Implemented and live-verified |
| Lighting | Effect, RGB/palette, brightness; speed on supported effects | Implemented against the seven effects reported by firmware 1.3.0 |
| Media | Play, pause, previous, next | Implemented; affects the active Bluetooth player |
| Built-in BGM | 32 app audio tracks across 10 categories, repeat/selection | Not bundled; these are app-local copyrighted media, not BLE-resident tracks |
| Clock | Sync to host, 12-hour or 24-hour format | Implemented from app command definition |
| LDAC | 96 kHz, 48/44.1 kHz, disabled | Implemented; Bluetooth restart observed live |
| Device name/info | Rename; show address and firmware | Implemented and live-verified |
| Bluetooth disconnect | Disconnect Classic audio | Implemented behind dangerous-operation gate |
| Power off | Remote shutdown; no remote power-on | Implemented behind dangerous-operation gate |
| Factory reset | Clear settings and pairing information | Implemented behind dangerous-operation gate |
| Firmware update | Conditional OTA flow | Deliberately excluded from the agent surface |
| Manual/store/profile | App navigation and web content | Not speaker controls |

The 2023 app manual presents ten visual labels/modes. Firmware 1.3.0 directly reports seven selectable effect records in lighting group 1. The MCP uses live-reported IDs instead of inventing three additional writable IDs.

Primary references:

- [QD35 Edifier Connect operation manual](https://www.princeton.co.jp/sites/default/files/support/manual/QD35_APPmanual.pdf)
- [QD35 official product page](https://www.edifier.com/p/wireless-speakers/qd35)
- [QD35 official support page](https://www.edifier.com/s/wireless-speakers/qd35)
- [QD35 user manual](https://usa-oss.edifier.com/Uploads/images/2023/03/21/2023032109390916793627498265.pdf)

The user-supplied `EC_v8.5.5-jiagu_1794042055.apk` was identified through Drive metadata but not downloaded because its 288,639,520-byte size exceeds the connector's 256 MiB limit and the direct URL requires authentication. Protocol research used official Edifier Connect 8.4.33 plus a separately obtained 8.3.43 package and live hardware validation.

