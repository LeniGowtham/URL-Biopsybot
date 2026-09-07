# Running URL QC on a schedule in GitHub Actions

This runs `url_qc.py` unattended in GitHub's cloud on a daily cron (see
`.github/workflows/url-qc.yml`). No machine of yours needs to be on.

The Google login is supplied as an **encrypted secret** — `oauth_credentials.json` and
`token.pkl` are **never** committed. The workflow refreshes the token silently; it never
opens a browser.

Because cloud runners have no `G:` shared drive, screenshots and the report CSV are saved
as **downloadable run artifacts** (Actions tab → the run → Artifacts) instead of the shared
drive. The report is still emailed via the Gmail API using the same login.

---

## One-time setup

### 1. Mint the token locally

Log in once on your machine so `token.pkl` exists and covers all scopes (Sheets + Gmail):

```powershell
python url_qc.py --limit 1     # completes the browser consent, writes token.pkl
```

Then export it as JSON:

```powershell
python scheduler/export_token.py > token.json
```

`token.json` contains a long-lived **refresh token** — treat it like a password. It is
already gitignored via the `token.*` / secrets rules; delete it once pasted.

### 2. Add the secret to GitHub

Repo → **Settings → Secrets and variables → Actions**:

| Type       | Name                  | Value                                             | Required |
| ---------- | --------------------- | ------------------------------------------------- | -------- |
| **Secret** | `GOOGLE_OAUTH_TOKEN`  | the entire contents of `token.json`               | ✅ yes   |
| **Secret** | `QC_PROXY`            | outbound proxy URL, e.g. `http://user:pass@host:port` | optional |
| Variable   | `QC_EMAIL_TO`         | report recipient(s)                               | optional |
| Variable   | `QC_EMAIL_CC`         | report CC                                         | optional |

If `QC_EMAIL_TO` is unset it defaults to the address baked into `url_qc.py`.
If `QC_PROXY` is unset the run goes out over GitHub's IP directly.

### 3. Enable the Google APIs

In the Google Cloud project that owns the OAuth client, make sure **Google Sheets API**
and **Gmail API** are enabled.

### 4. Run it

Actions tab → **URL QC (daily)** → **Run workflow** to test on demand (you can pass a
`limit` like `5` for a quick check). After that it runs automatically on the cron.

---

## ⚠️ Important: keep the refresh token alive

If your OAuth consent screen is in **"Testing"** publishing status, Google **expires
refresh tokens after 7 days** — the scheduled run will then start failing with an
`invalid_grant` error every week.

**Fix:** in Google Cloud Console → OAuth consent screen, set the publishing status to
**"In production"** (for an internal Workspace app this is immediate; no verification
needed for internal apps). Then re-mint the token (steps 1–2) once.

If a run ever fails with `invalid_grant` / `Token has been expired or revoked`, re-run
steps 1–2 to refresh the `GOOGLE_OAUTH_TOKEN` secret.

---

## Notes / tradeoffs vs. the local Windows run

- **Screenshots** live as run artifacts (14-day retention), not on the `G:` shared drive.
  To restore team-visible screenshots, they'd need to be uploaded to the shared drive via
  the Google Drive API — ask and this can be added.
- **`url_cache.json`** (last-known-URL memory) is not persisted between cloud runs, so the
  last-known-URL fallback starts empty each run. Can be persisted with `actions/cache` if
  wanted.
- **Bot-blocking:** datacenter IPs get more 403s than a residential machine. Set `QC_PROXY`
  if you see soft-blocks.
- **Adjust the schedule** in `.github/workflows/url-qc.yml` (`cron` is UTC).
