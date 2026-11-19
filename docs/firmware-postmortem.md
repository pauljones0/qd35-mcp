# QD35 firmware postmortem and capability audit

Audit date: 2026-11-19 UTC.

## Findings

The QD35 lighting/control image can add features that use the existing clock,
buttons, RGB chain, display, and amplifier-control bus. The useful next steps are
clock and display readback, a hardware-verified chime control, and local light
scheduling. The current patch has **28 bytes of contiguous space** before animation
storage. A larger feature set needs a deliberate application layout and a tested
recovery path.

The audited image is the fixed Edifier QD35 MCU 1.3.0 build. The patched image is
40,932 bytes, SHA-256
`b2354c561dd02e73e8d8f25599b956d69e58742421f8edc13101a3f7f0786c8a`.
The owner's speaker exhibited the stock `D4` behavior after rollback and the
patched behavior again after reinstallation. Both revisions acknowledge the probe.
[Stock provenance](firmware-analysis.md#provenance-and-update-status) ·
[Hardware validation](hardware-validation.md#separator-preserving-revision)

## What the patch can reach

| Subsystem | Evidence | Feature boundary |
|---|---|---|
| Lighting and control MCU | The related NEW-X teardown identifies the Artery AT32F421F8P7. Artery lists Cortex-M4, 64 KiB flash, 16 KiB SRAM, timers, DMA, serial buses and ADC for that part. The international QD35 image uses the matching kind of control architecture. | The teardown identifies the domestic board. The exact SKU and pin routing on the owner's unit remain unmeasured. The patch's Cortex-M3 assembler target selects a Thumb instruction subset compatible with the M4. |
| Clock | The stock parser writes time via `0x08007AAC`. The active main loop refreshes the external clock via `0x08007BE0` and renders it via `0x08006C1E`. The I²C bus on PA0/PA1 matches multiple WAVE RTC variants. | Clock readback and an on-device light timer are plausible. Alarm interrupts, retained state and extra telemetry depend on the RTC variant and wiring. |
| Front display | The CMS1640 bus is on PF0/PF1. The firmware clears all sixteen RAM bytes and normally uses the first eight. Digits, separators, the wordmark-init group and status symbols have separate bits. | Visibility masks and numeric overlays are available. The driver's PWM setting covers the whole panel. The wordmark's residual visibility remains unexplained. |
| Buttons and knobs | The ADC at PA4 sees three press bands. Sol emulated lighting encoder events from PA5/PA6 and volume events from PA13/PA14. | Local snooze and preset shortcuts are plausible. Check the existing press, hold, double-press and rotate behavior before assigning new gestures. |
| RGB lighting | PB1 carries the timer/DMA waveform for 37 RGB words. Stock type 2 receives ten audio-analysis values from the audio processor. | Existing uploads cover literal and sound-reactive patterns. Local scheduling and per-zone policies are firmware candidates. The analysis values follow different stock processing paths for wired AUX and digital inputs. |
| Audio amplifier | The MCU writes state and gain registers over I²C at address `0x2D`. The domestic board report and register behavior point to a TI 5805M-family amplifier. | Prompt attenuation is plausible after verifying timing and restoration. Read-only amplifier fault reporting is another candidate. |
| Bluetooth and USB audio | The related board has a separate BES Bluetooth/audio SoC. The QD35 image uses USART1 on PA9/PA10 and has USB/AUX detection messages. Artery lists no USB peripheral for this MCU. | The lighting patch can observe or act on local policy. Bluetooth profiles, codecs, USB descriptors and network services depend on the other processor and board wiring. |
| USB charging | The related teardown shows a separate GaN charging board and USB charging controllers. The QD35 manual documents charging in standby. | A usable telemetry or control connection from the lighting MCU to the charger has not been established. |

[NEW-X teardown](https://www.52audio.com/archives/167663.html) ·
[Artery AT32F421 specifications](https://arterytek.com/en/product/AT32F421.jsp) ·
[Artery migration guide, USB peripheral comparison](https://www.arterytek.com/download/MG/MG0017_Migrating_from_AT32F415_to_AT32F421_EN_V2.0.1.pdf) ·
[CMS1640 datasheet](https://www.mcu.com.cn/en/support-download/171.html) ·
[WAVE RTC family specifications](https://www.whwave.com.cn/cpzs_szic) ·
[TI amplifier datasheet](https://www.ti.com/lit/ds/symlink/tas5805m.pdf) ·
[Edifier QD35 manual](https://usa-oss.edifier.com/Uploads/images/2023/03/21/2023032109390916793627498265.pdf)

### The RTC is the strongest new peripheral lead

The firmware emits I²C address bytes `64` and `65` for this device at
`0x08007780`, corresponding to 7-bit address `0x32`. Its time-write helper at
`0x08007942` uses the paired write-enable fields in registers `0F` and `10`.
The BCD time layout and 24-hour flag are shared by the SD2059 and SD3078.
This establishes a register-compatible family. The exact part remains unknown.
WAVE lists 44 user SRAM bytes for the SD2059, 70 for the SD3078, and 12 for the
SD2078. Temperature and battery measurements are variant-specific. Check the
chip marking and backup supply before designing around those features.

The stock time writer sends seven bytes starting with seconds. After hour it
writes the same calendar sequence `06 01 01 00` on every sync. The replay
of 23:34:56 in 24-hour mode emitted `56 34 A3 06 01 01 00`. Thus there is
no reliable stock weekday or date to use for scheduling. A daily light timer
can compare time of day while powered. A weekday schedule needs calendar
handling, and a standby hardware alarm needs the RTC alarm line traced.
[WAVE SD3078 specifications](https://www.whwave.com.cn/SD3078) ·
[SD2059 register map](https://atta.szlcsc.com/upload/public/pdf/source/20220401/14F5F8DC88674048F8466FB460745D5A.pdf) ·
[SD3078 reference register map](https://www.iclib.cn/uploads/pdf/20210610/9c6284b9aef94c054be092a18afff1c5.pdf)

## Display incident

| Revision | What happened | What the evidence established |
|---|---|---|
| Original patched display | User observed a visible EDIFIER wordmark with the logo command off. Whole-display shutdown did extinguish it. | The stock-style BLE acknowledgement carried the current ordinary lighting state. It could establish that the firmware responded, while the user's physical observation established the visible outcome. |
| 40,880-byte revision | The test requested a numeric-only frame. The wordmark remained visible. | Replaying the GPIO transaction confirmed that the requested eight bytes contained just numeric segments. The old writer also emitted `0x8F` before the requested minimum `0x88` during redraws. The test could not identify the source of the residual. |
| 40,936-byte revision | The patch intercepted wake and redraw writes, added atomic `D3` masked updates, and reserved state above the adjusted stack. | Emulation and a live `D3` probe verified those paths. The combined logo/symbol mask still zeroed the clock separator bits in byte 0. |
| 40,932-byte revision | The current mask preserves the separators. The patched `D4` handler acknowledges without changing RGB brightness. | The live rollback test and offline replay reproduced both handlers' behavior. In the live isolation test, the wordmark stayed lit with `C0`–`C7` zeroed and went dark with the driver off. |

The main verification mistake was interpreting a residual visual observation as
proof of optical coupling. A second mistake was treating a generic lighting reply
as evidence of a changed display. Future display changes should have separate
checks for the requested state, MCU execution, the physical electrical output,
and the visible result.
[Hardware results](hardware-validation.md#optional-display-patch-ota-and-controls) ·
[Controlled optical comparison](display-firmware.md#visual-isolation-check)

## Remaining engineering constraints

| Constraint | Evidence and mitigation |
|---|---|
| Flash layout | The official app starts at `0x08002000` and its image ends at `0x0800BE17` exclusive. The patch occupies `0x0800BE20` through `0x0800BFE3`. The stock animation erase loop at `0x08004ADC` erases fifteen 1 KiB sectors from `0x0800C000` up to `0x0800FC00`. The settings functions at `0x08004ACE` and `0x08004B2A` use `0x0800FC00`. Keep code out of both regions. |
| Boot and rollback | The stock OTA-ready path enters a separate address below the app at `0x08000800`. Stock rollback was verified on the speaker by its command behavior. The shorter stock image shares the final application sector with the patch. Flash-tail erasure and interrupted-update recovery still need bench verification. |
| RAM and interrupt timing | The patch reserves 24 bytes at `0x20002718` and changes initial MSP from `0x20002730`. Tracing the stock C initializer observed writes to application data below `0x2000172C` plus its stack frames. Sol independently measured per-entry stack use of 56 bytes for the ordinary redraw and 72 for the alternate writer. Runtime high-water marks and interrupt timing still need checking before growing the reservation. |
| Real-time paths | The main loop services RTC, display and animations. The power-transition handler at `0x0800AA4E` can reach display work and then requests a system reset. Sol found no returning interrupt path that redraws. Preserve the guards and keep new periodic display work in the foreground. |
| Reply semantics | Stock and patched `D4` acknowledgements use ordinary lighting state. The stock handler changes brightness. The patched handler leaves it unchanged. An accepted `D3` can update SRAM and acknowledge while the display-write guard blocks output. Any state readback needs an end-to-end response that the BES bridge actually forwards. |
| Single animation slot | The type-2 asset consumes 15,000 of the 15,360 animation bytes. The device returns identity over BLE without a binary backup. Program and test alternate assets only when the existing asset can be replaced or reconstructed. |

## Candidate changes in order

1. **Readback and front-panel controls.** Give the patched protocol a
   revisioned reply carrying actual flags, brightness and RTC state. Verify that
   the audio-control bridge forwards the response before relying on it in MCP.
   Clear and capture all 16 CMS1640 RAM bytes while leaving the driver on. If
   the wordmark stays lit, trace its power and control lines on the panel PCB
   before planning a clock-only preset.
2. **Quiet reconnect.** The existing prompt-start parser selects a fixed amp gain
   and sends the play-state register write in Bluetooth mode. CPU replay produced
   identical writes at media volumes 0, 10 and 16. Test a bounded prompt-only
   attenuation policy against acoustic recordings, music already playing,
   back-to-back prompt events and a missing prompt-end event. Include the
   source-switch and wake paths and keep normal playback restoration explicit.
   Establish that the start notification arrives early enough to catch the
   beginning of the audible tone.
3. **Local light schedule and a dial gesture.** The stock clock poll and RGB
   pipeline can support an active-mode night window, sunrise or light timeout.
   Foreground tick handlers at `0x0800860C` and `0x0800864A` are potential
   integration points. Use elapsed time so skipped or merged ticks cannot
   extend a timeout.
   Keep the first schedule volatile and validate midnight, 12/24-hour mode, clock
   resync, and standby behavior. Check the RTC part and backup RAM before
   considering retained settings that avoid repeated MCU flash writes.
   Internet radio still needs a source such as the host alarm service.
4. **Wired-input events and audio visualization.** Capture USB and AUX insertion
   and removal, then test whether the bridge emits useful status. Measure the
   real audio analysis channels with quiet test tones. Expose meaningful bands
   only after measuring the mapping. Authoring and the Spectrum Meter already
   work in the stock type-2 renderer under emulation.
5. **Expanded firmware image.** Reproduce build output and establish recovery
   before a change that outgrows the current gap. A true per-area dimmer needs
   timed plane changes or board-level separation. Verify flicker, duty cycle,
   display-controller behavior, and effects on the main loop. Separate Bluetooth
   firmware and charging-controller work from this image.

## Reproduction

- Reassembled the patch with Clang, extracted the original code section with
  `llvm-objcopy`, confirmed zero relocations and byte-for-byte equality with the
  bundled patch, and independently decoded all six Thumb branch hooks.
- Recomputed both firmware hashes and verified the final address against the
  animation erase boundary.
- Ran the Unicorn checks against the exact stock source image. They passed
  1,536 redraw cases, 128 masked updates, isolated display frames, guard/wake/reset
  cases, and all 100 Spectrum Meter input cases.
- Replayed the stock RTC write path with the I²C primitives intercepted and
  compared the emitted register data in 12- and 24-hour modes.
- Ran Ruff and pytest. **226 passed** and three hardware tests were skipped in
  this audit run.
- Sol independently replayed the bit-banged GPIO path and checked 12,928
  additional board-frame and control combinations. The existing repository
  checker should verify full frame contents on both writers and cover an
  accepted guarded update followed by the first permitted redraw. CI should
  also check compiled versus embedded patch bytes.

[Assembly](../firmware/qd35_display_patch.S) ·
[Exact-image builder](../src/qd35_mcp/firmware_patch.py) ·
[Firmware checks](../tools/check_display_firmware.py) ·
[BLE installer](../src/qd35_mcp/transport/bleak.py)
