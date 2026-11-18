# Contributing

Use the [architecture and evidence index](docs/architecture.md) to find the right module
and distinguish public behavior, protocol notes, and live hardware evidence.

1. Open an issue describing the model, firmware, command, and evidence.
2. Create a focused branch and add tests for protocol or behavior changes.
3. Run `uv run ruff check .`, `uv run pytest`, and `uv build`.
4. Keep hardware tests opt-in and restore the user's state when practical.

## Hardware checks

Physical checks are opt-in and never run in CI:

```bash
QD35_HARDWARE=1 QD35_DEVICE=<ble-address> uv run pytest tests/hardware
QD35_HARDWARE=1 QD35_HARDWARE_WRITES=1 QD35_ENABLE_WRITES=1 \
  QD35_DEVICE=<ble-address> uv run pytest tests/hardware
```

Custom-light hardware validation is separately gated because it replaces the installed Extended asset (the visible ordinary effect is restored afterward):

```bash
QD35_HARDWARE=1 QD35_HARDWARE_WRITES=1 QD35_HARDWARE_CUSTOM_LIGHTS=1 \
  QD35_ENABLE_WRITES=1 QD35_DEVICE=<ble-address> \
  uv run pytest tests/hardware -k custom_light
```

Protocol fixtures must record their provenance, model, firmware, transport, and whether they were captured from real hardware. Redact Bluetooth addresses unless the address is intentionally synthetic.

Do not commit Edifier APKs, firmware images, audio tracks, artwork, signing material,
or decompiled source. Original patch source and a deterministic patch payload are
acceptable only when the tool validates the exact official input image by hash
and the repository cannot reconstruct that image on its own. Implement
protocol facts independently and cite the observation or public documentation that
supports them.

