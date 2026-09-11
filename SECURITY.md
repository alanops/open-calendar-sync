# Security and privacy

Open Calendar Sync v0.1 is a single-owner self-hosted preview, not a security-audited multi-user service.

## Data access

Google Calendar and Microsoft Graph read/write calendar permissions are necessary to manage busy copies. The provider APIs can return full event details even though the engine only uses identity, times, availability, ownership markers and version identifiers. **Busy-only describes the copies; it does not mean the app only has free/busy API access.** The implementation does not persist meeting titles, descriptions, attendees, locations or meeting links.

SQLite stores connected account identifiers and email addresses, encrypted OAuth tokens, hashed sessions, short-lived OAuth handshakes, managed event IDs and count-only run logs. The Fernet key is supplied separately through server configuration. Anyone who can read the key and database can decrypt tokens. Restrict access and back up both securely.

The dashboard access key grants control of the whole instance. Use a long randomly generated key; never reuse an account password. OAuth uses Authlib's authorization-code flow with PKCE S256, short-lived single-use state tied to the authenticated session, and fixed callback origins. Sessions are server-side, HttpOnly and SameSite=Lax; HTTPS enables Secure cookies. Write requests require a matching Origin and a session CSRF token.

No third-party analytics, font requests or hosted sync vendor are used. Disable query-string logging at your reverse proxy: OAuth callback URLs contain short-lived authorization codes. The supplied Caddy config has no access logging; the Uvicorn commands disable it too.

## Safe deployment

- Keep HTTPS enabled for every non-loopback deployment.
- Run one process per installation and one instance per set of calendars. The application takes an OS file lock to reject multiple local workers.
- Restrict dashboard exposure with a firewall/VPN if appropriate. Do not use this shared key as authentication for a public SaaS.
- Keep `.env`, database files and backups out of Git. The repository's Docker context excludes them.
- Do not pass OAuth client secrets, tokens or calendar data in GitHub issues.
- Use the latest patched dependency lockfile; review dependency updates before deployment.
- A network failure can leave part of a cycle applied; the next successful cycle reconciles it. No remote multi-calendar transaction exists.
- ETag conditions protect writes when the provider supplies an event version. If a managed copy has acquired guests or lost its ownership marker, syncing stops rather than modifying that event.

## Reporting

Report vulnerabilities privately through this repository's GitHub **Security → Report a vulnerability** feature, if enabled. If unavailable, open an issue requesting a private reporting channel **without** vulnerability details or credentials. This project has no guaranteed response SLA.
