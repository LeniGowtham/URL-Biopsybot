# URL QC — Santander / Frontier / TDBANK offers

Daily automation that checks every offer's redemption URL and **stores a screenshot of the
final landing page**, so you can confirm each offer link works and lands where it should.

## What it does

For every offer in the **`BrandName_Export`** tab of the
[offers sheet](https://docs.google.com/spreadsheets/d/1bkDvHQ1NdzvTjsBVsvAQRuJWWNw8tZjD7lVK7et7dc8/edit):

1. Resolves the redirect chain over HTTP/1.1 (`requests`) to find the **true final landing URL**.
   This deliberately avoids opening the Capillary `redeem/redirect` endpoint in the browser,
   which throws `ERR_HTTP2_PROTOCOL_ERROR` in Chromium.
2. Opens that final landing page in headless Chromium and saves a **landing-page screenshot**
   (the visible viewport at 1920×1080 desktop, not the full scrolled page).
3. Records the final URL, HTTP status, and a PASS / WARN / FAIL result.

### Result meanings
| Result | Meaning |
|--------|---------|
| **PASS** | Landed on a real page (HTTP 2xx, or the merchant's own homepage 3xx redirect). |
| **WARN** | Reached the right merchant but it's guarded — HTTP 401/403/429 (bot-block or rate-limit). **Eyeball the screenshot** to confirm. |
| **FAIL** | Broken: no response, a junk landing host (`null.com`, empty), or a hard 4xx/5xx. |

## Outputs

```
G:\Shared drives\content-ops-team\URL QC\<YYYY-MM-DD>\<Brand>\<offer-id>.png   screenshots (team shared drive)
reports\qc_<YYYY-MM-DD>.csv                                                    full machine-readable report (local)
```
Screenshots are saved to the **content-ops team shared drive** so the whole team can view
them. Override the location with env `QC_SCREENSHOT_ROOT`; if the shared drive isn't mounted
at run time, the script warns and falls back to a local `screenshots\` folder so the run
still completes. Plus a color-coded **`QC Run <YYYY-MM-DD>`** tab added to the Google Sheet
(FAIL/WARN sorted to the top). Nothing existing in the sheet is overwritten.

## Run it manually

```powershell
cd "C:\Users\gowtham.km\Documents\URL QC ALL 3 BRANDS"
python url_qc.py                 # full daily run (~all 400+ offers)
python url_qc.py --limit 10      # quick test on the first 10 offers
python url_qc.py --brand Frontier   # one brand only
python url_qc.py --no-sheet      # local screenshots + CSV only, don't touch the Sheet
python url_qc.py --no-proxy      # ignore any configured proxy for this run
python url_qc.py --direct-only   # only the ~141 direct-url offers; skip BI/BIFROST (no token needed)
python url_qc.py --bifrost-optional  # skip BI/BIFROST if the token is stale, don't stop the run
```

The **first run** opens a Google sign-in in your browser to authorize access; after that the
token is cached in `token.pkl` and runs are unattended.

## Schedule it to run every day (7:00 AM)

A Windows scheduled task **`URL QC Daily`** is already registered — it runs `run_daily.bat`
at 07:00 every day and logs to `logs\run.log`. `run_daily.bat` runs the QC with
**`--bifrost-optional`**, so if the Bifrost token is stale at run time it **skips the BI/BIFROST
offers and still QC's the ~141 direct-url offers** instead of failing the whole run (see the
token note below).

Manage it in PowerShell:

```powershell
Get-ScheduledTaskInfo  -TaskName "URL QC Daily"    # status + next run time
Start-ScheduledTask    -TaskName "URL QC Daily"    # run it now
Unregister-ScheduledTask -TaskName "URL QC Daily"  # remove it
```

To (re)create it from scratch:

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\Users\gowtham.km\Documents\URL QC ALL 3 BRANDS\run_daily.bat" -WorkingDirectory "C:\Users\gowtham.km\Documents\URL QC ALL 3 BRANDS"
$trigger = New-ScheduledTaskTrigger -Daily -At 7:00AM
Register-ScheduledTask -TaskName "URL QC Daily" -Action $action -Trigger $trigger -Force
```

> **Token caveat for the daily run:** the 141 direct-url offers QC fully unattended. The 239
> BI/BIFROST offers only run if a fresh Bifrost token is in `bifrost_token.txt` at 07:00 — the
> token expires ~10h, so on most days you must paste a fresh one (or trigger a run right after
> refreshing it) to include them. Without it, they're skipped with a clear warning in the log.

> **Note on unattended auth:** if the Google OAuth consent screen for project `tdconfiqqc`
> is still in *Testing* mode, refresh tokens can expire after ~7 days and a run will re-prompt
> for sign-in. If that happens, either run `python url_qc.py` once manually to re-auth, or set
> the OAuth consent screen to *In production* in Google Cloud Console.

## Template placeholders (`{CAP-UID}`)

Some redemption URLs carry a placeholder that must be filled with a real value before the
link works — notably the **76 TDBANK** URLs shaped like
`.../bifrost/redemption?user-id={CAP-UID}&offer-id=...`. Left literal, the endpoint returns
HTTP 400. Put the real test user-id in the `SUBSTITUTIONS` dict at the top of `url_qc.py`:

```python
SUBSTITUTIONS = {
    "{CAP-UID}": "211410781",   # a valid test user-id for the US/TDBANK tenant
}
```

Any URL that still contains an unresolved `{...}` after substitution is reported as
**TEMPLATE** (grey) — "can't QC until a real value is provided" — instead of a misleading FAIL.

## Reducing WARN bot-blocks — optional proxy

Most **WARN** results are the merchant's own anti-bot wall (Akamai / DataDome / Cloudflare)
returning 403/429 on real, working pages. The script already tries hard to look human (new
headless real Chrome, a persistent profile, stealth patches, mouse/scroll jitter, engine
swaps) — but the stubborn ones are scored on **IP / geolocation**, which only a **UK
residential proxy** can clear.

To enable one, copy `proxy.txt.example` → **`proxy.txt`** and put a single proxy URL on the
first line:

```
http://user:pass@gw.provider.com:7000
```

It's applied to the merchant-facing traffic only (browser navigation + redirect resolution);
the Capillary Bifrost API call is always sent direct. Precedence: env `QC_PROXY` > `proxy.txt`
> the `PROXY` constant. Use `--no-proxy` to skip it for one run. Datacenter proxies rarely
help; use a residential pool.

## BI / BIFROST offers (auto-resolved redemption URLs)

Offers whose `source/offerSource` is **`BI`** or **`BIFROST`** have no redemption URL in the
sheet. For each, the script calls the affiliate-bff `filter-offer` API (`offerId` =
`external_offer_id`), takes the response's `activatedUrl`, **writes it back into the
`redemption_url` column**, then QCs it. This needs a bearer token (it expires ~10h): paste a
fresh one — from bifrost-ui DevTools → Network → `filter-offer` → Request Headers →
`authorization` — into **`bifrost_token.txt`**. If it's expired, the run stops with a clear
`HTTP 401/403` message (or, with `--bifrost-optional`, skips these offers and continues).

**Daily task uses `--no-bifrost-api` instead (no token).** The scheduled `URL QC Daily` run
does **not** call the Bifrost API. It QCs the `redemption_url` already present in the sheet for
every offer — you fill the BI/BIFROST redemption URLs into the sheet manually, and any BI/BIFROST
row still blank is skipped. This removes the token dependency from the unattended run. (Switch
`run_daily.bat` back to `--bifrost-optional` if you'd rather auto-resolve them via the API.)

## Tuning (top of `url_qc.py`)
- `CONCURRENCY` / `REDEEM_CONCURRENCY` — parallel page loads.
- `REDEEM_MIN_GAP` — min seconds between Capillary redeem calls (rate-limit guard).
- `NAV_TIMEOUT_MS`, `SETTLE_MS` — per-page navigation timeout and render-settle wait.
- `VIEWPORT` — browser window size; set to `1920×1080` (full desktop). Screenshots are
  **viewport-only** (the visible landing page), so each image is `1920×1080`. To capture the
  full scrolled page instead, add `full_page=True` back to the `page.screenshot(...)` call in
  `_capture_nonblank`.
