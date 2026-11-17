# Security policy

Report vulnerabilities privately through GitHub's security advisory feature rather than a public issue.

QD35 MCP is local-first and exposes stdio only. Keep write access disabled unless the MCP host and its users are trusted. Configure `QD35_DEVICE` when other QD35 speakers may be nearby. Dangerous operations are separately gated.

The server never needs cloud credentials, Edifier account credentials, pairing PINs, or arbitrary shell access. It does not expose raw GATT writes or arbitrary firmware input. Its two fixed-purpose firmware tools require an explicit device address, the ordinary/dangerous/firmware gates, and exact confirmation phrases. They fetch only one hard-coded official HTTPS URL, accept only the exact QD35 1.3.0 source hash, verify original hook bytes and the deterministic patched hash, and include an exact-stock restore path. The official custom-light tool permits only the fixed Edifier asset host, rejects off-host redirects, bounds response size, and verifies pinned hashes before Bluetooth transfer. Diagnostic animations are generated locally from fixed patterns.

Remote alarm audio is disabled by default. When explicitly enabled, it requires an exact operator allowlist, accepts only HTTP(S) URLs without embedded credentials, and rejects any hostname response containing a non-public address. Local audio is confined to an operator-configured media directory. Alarm state is written mode 0600, unit names are strict identifiers, subprocesses use argument arrays rather than a shell, and the keepalive service only reconnects an existing Classic pairing. Bluetooth identifiers may be sensitive; avoid publishing logs or alarm state without redaction.

Supported security updates currently target the latest `0.1.x` release line.

