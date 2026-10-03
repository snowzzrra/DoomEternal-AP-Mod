# Reports on Render

The launcher sends reviewed text to this service. The GitHub App creates issues only
in `snowzzrra/DoomEternal-AP-Mod`. Private keys stay on Render; support ZIPs stay on
the player's computer.

## GitHub App

1. In GitHub Settings → Developer settings → GitHub Apps, create an App owned by
   `snowzzrra`. Use the repository URL as its homepage. Disable webhooks.
2. Set repository permission **Issues: Read and write**. Keep the default Metadata
   permission. No other permissions are needed.
3. Install the App on **only** `snowzzrra/DoomEternal-AP-Mod`.
4. Record the App ID and the installation ID (the number in the installation's
   settings URL). Generate and download a private key. Do not commit the PEM.

## Render

1. Merge the reviewed fixes into `main` first.
2. In Render, create a Blueprint from `snowzzrra/DoomEternal-AP-Mod`, branch `main`.
   The root `render.yaml` defines `doometernal-ap-reports`, its Docker build,
   `/health`, and a persistent 1 GB disk at `/data`.
3. This configuration uses a paid Starter service: persistent state is required
   to prevent duplicate issues after restarts. Keep a single service instance.
4. Set `GITHUB_APP_ID` and `GITHUB_INSTALLATION_ID` to the recorded numeric IDs.
5. In the service's Secret Files, add `github_app_key.pem` with the complete PEM
   contents. Its path is `/etc/secrets/github_app_key.pem`.
6. Deploy the current main commit. Automatic deploys are disabled in the Blueprint;
   future updates use **Manual Deploy → Deploy latest commit**.
7. Open `https://<actual-service-host>/health`. Expect HTTP 200 and
   `{"status":"ready"}`. Startup validates the IDs and RSA key. The first report
   verifies GitHub installation permissions.

The Docker startup assigns the mounted `/data` directory to `node`, then runs the
service as that user. `REPORT_TRUSTED_PROXY_HOPS=1` trusts Render's immediate
ingress proxy; direct hosting without a proxy should use `0`. The process honors
Render's `PORT`. Do not delete the disk or its UUID state files.

## Enable the launcher

Edit **`client/data/report_endpoint.json` in the extracted candidate**:

```json
{
  "schema_version": 1,
  "endpoint": "https://<actual-service-host>/v1/reports"
}
```

Use the service's actual HTTPS hostname, without a query or fragment. Restart the
launcher. Its external client configuration takes precedence by being the report
configuration owner; editing this file requires no executable rebuild.

For subsequent distributions, set the same endpoint in the source repository's
`data/report_endpoint.json`, commit it, and rebuild the release through the existing
workflow. The checked-in default remains disabled until the service exists.

## Manual qualification

1. In the launcher, open Report Problem, describe a real problem, inspect the
   sanitized preview, and send it. This creates an actual GitHub issue.
2. Confirm the returned link belongs to `snowzzrra/DoomEternal-AP-Mod/issues/…`.
3. Retry the saved report and confirm it returns the same issue URL.
4. Restart the Render service, retry that same report, and confirm no second issue
   appears. Keep the persistent disk mounted throughout.

Local verification uses `node tools/report_backend/test_server.cjs`; it runs a real
loopback HTTP server with fake GitHub responses and creates no GitHub issues.
