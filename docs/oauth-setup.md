# Connect Google and Microsoft

Create app registrations you control. Registration credentials identify your installation; each calendar owner still signs in and consents through the provider's own screen. Never enter an account password into this dashboard.

For local setup, `BASE_URL=http://localhost:8000`. For HTTPS hosting, use your origin without a trailing slash, for example `https://calendar.example.com`.

## Google

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a project and enable the **Google Calendar API**.
2. Configure the OAuth consent screen in Google Auth Platform. Use **External** if connecting accounts across Workspace organizations or Gmail. In Testing mode, add each intended account as a test user.
3. Create an OAuth **Web application** client. Add this exact authorized redirect URI:
   - Local: `http://localhost:8000/oauth/google/callback`
   - Hosted: `https://calendar.example.com/oauth/google/callback` (replace the hostname)
4. Put the client ID and secret into `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env` and restart the server.
5. Sign into the dashboard, choose **Connect Google**, select an account and review the permissions. Repeat for every Google account.

Requested scopes: `openid`, `email`, `https://www.googleapis.com/auth/calendar.events` and `https://www.googleapis.com/auth/calendar.calendarlist.readonly`. No Gmail, contacts or Drive permissions are requested. Workspace administrators may need to allow the app.

Google external apps in **Testing** mode generally receive refresh tokens that expire after seven days for calendar scopes. For unattended use, complete the appropriate publishing/verification setup. Do not represent an unverified app as verified or bypass organization restrictions. See Google's [OAuth web-server flow](https://developers.google.com/identity/protocols/oauth2/web-server) and [refresh-token expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration).

## Microsoft 365 / Outlook

1. In the [Microsoft Entra admin center](https://entra.microsoft.com/), create an **App registration**.
2. For calendars across organizations, choose accounts in any organizational directory; include personal Microsoft accounts if you need Outlook.com support. Set `MICROSOFT_TENANT=organizations` for work/school accounts only, `common` for work and personal accounts, or the tenant ID for a single-tenant app.
3. Under Authentication, add a **Web** platform callback:
   - Local: `http://localhost:8000/oauth/microsoft/callback`
   - Hosted: `https://calendar.example.com/oauth/microsoft/callback`
4. Add Microsoft Graph **delegated** permissions `User.Read` and `Calendars.ReadWrite`. The authorization flow also requests `openid` and `offline_access`. Do not use application-wide permissions for this tool.
5. Create a client secret. Store the **secret value**, not its identifier, as `MICROSOFT_CLIENT_SECRET`; store the application/client ID as `MICROSOFT_CLIENT_ID`. Record the expiry in your own operations system and rotate it before it expires.
6. Restart, then choose **Connect Microsoft** in the dashboard. Organization consent policies may require an administrator's approval.

Reference: [Microsoft authorization-code flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow).

## First sync

1. Connect the desired accounts. Only their primary calendars are supported in this version.
2. Select **Sync** for each account to include. This pauses the engine while you change the group.
3. Choose **Preview busy blocks**. This only reads the calendars and reports the desired number of copies.
4. Choose **Start automatic sync**. Confirm the activity result and inspect the private Busy copies in each provider.
5. On disposable test events, check creation, moving, cancellation, all-day dates and a recurring exception before depending on the tool. Do not use a production meeting with attendees as your first test.

If authorization expires, reconnect the same provider account: its stable account ID preserves copy ownership. If the destination is inaccessible, restore access before disconnecting so the app can clean up its copies.
