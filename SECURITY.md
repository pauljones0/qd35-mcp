# Security policy

Report vulnerabilities privately through GitHub's security advisory feature rather than a public issue.

QD35 MCP is local-first and exposes stdio only. Keep write access disabled unless the MCP host and its users are trusted. Configure `QD35_DEVICE` when other QD35 speakers may be nearby. Dangerous operations are separately gated.

The server never needs cloud credentials, Edifier account credentials, pairing PINs, or arbitrary shell access. It does not expose raw GATT writes or firmware update commands. The custom-light tool is the sole networked operation: it permits only the fixed Edifier asset host, rejects off-host redirects, bounds response size, and verifies pinned hashes before Bluetooth transfer. Bluetooth identifiers may be sensitive; avoid publishing logs without redaction.

Supported security updates currently target the latest `0.1.x` release line.

