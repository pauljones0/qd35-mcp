# Contributing

1. Open an issue describing the model, firmware, command, and evidence.
2. Create a focused branch and add tests for protocol or behavior changes.
3. Run `uv run ruff check .`, `uv run pytest`, and `uv build`.
4. Keep hardware tests opt-in and restore the user's state when practical.

Protocol fixtures must record their provenance, model, firmware, transport, and whether they were captured from real hardware. Redact Bluetooth addresses unless the address is intentionally synthetic.

Do not commit Edifier APKs, firmware images, audio tracks, artwork, signing material,
or decompiled source. Original patch source and a deterministic patch payload are
acceptable only when the tool validates a user-supplied proprietary image by exact
hash and the repository cannot reconstruct that image on its own. Implement
protocol facts independently and cite the observation or public documentation that
supports them.

