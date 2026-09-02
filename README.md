# URL QC — Santander / Frontier / TDBANK offers

Daily automation that checks every offer's redemption URL and **stores a screenshot of the
final landing page**, so you can confirm each offer link works and lands where it should.

## What it does

For every offer in the **`BrandName_Export`** tab of the
[offers sheet](https://docs.google.com/spreadsheets/d/1bkDvHQ1NdzvTjsBVsvAQRuJWWNw8tZjD7lVK7et7dc8/edit):

1. Resolves the redirect chain over HTTP/1.1 (`requests`) to find the **true final landing URL**.
   This deliberately avoids opening the Capillary `redeem/redirect` endpoint in the browser,
   which throws `ERR_HTTP2_PROTOCOL_ERROR` in Chromium.
2. Opens that final landing page in headless Chromium and saves a **full-page screenshot**.
3. Records the final URL, HTTP status, and a PASS / WARN / FAIL result.

### Result meanings
| Result | Meaning |
|--------|---------|
| **PASS** | Landed on a real page (HTTP 2xx, or the merchant's own homepage 3xx redirect). |
| **WARN** | Reached the right merchant but it's guarded — HTTP 401/403/429 (bot-block or rate-limit). **Eyeball the screenshot** to confirm. |
| **FAIL** | Broken: no response, a junk landing host (`null.com`, empty), or a hard 4xx/5xx. |

## Outputs (all inside this folder)

```
screenshots\<YYYY-MM-DD>\<Brand>\<offer-id>.png   one screenshot per offer, per day
reports\qc_<YYYY-MM-DD>.csv                        full machine-readable report
```
Plus a color-coded **`QC Run <YYYY-MM-DD>`** tab added to the Google Sheet
(FAIL/WARN sorted to the top). Nothing existing in the sheet is overwritten.

## Run it manually

```powershell
cd "C:\Users\gowtham.km\Documents\URL QC ALL 3 BRANDS"
python url_qc.py                 # full daily run (~all 400+ offers)
python url_qc.py --limit 10      # quick test on the first 10 offers
python url_qc.py --brand Frontier   # one brand only
python url_qc.py --no-sheet      # local screenshots + CSV only, don't touch the Sheet
python url_qc.py --no-proxy      # ignore any configured proxy for this run
```

The **first run** opens a Google sign-in in your browser to authorize access; after that the
token is cached in `token.pkl` and runs are unattended.

## Schedule it to run every day (7:00 AM)

Run this once in PowerShell (adjust `/ST` for a different time):

```powershell
schtasks /Create /SC DAILY /TN "URL QC Daily" ^
  /TR "\"C:\Users\gowtham.km\Documents\URL QC ALL 3 BRANDS\run_daily.bat\"" ^
  /ST 07:00 /F
```

- Check it:   `schtasks /Query /TN "URL QC Daily"`
- Run it now: `schtasks /Run /TN "URL QC Daily"`
- Remove it:  `schtasks /Delete /TN "URL QC Daily" /F`

Logs are appended to `logs\run.log`.

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
`HTTP 401/403` message.

## Tuning (top of `url_qc.py`)
- `CONCURRENCY` / `REDEEM_CONCURRENCY` — parallel page loads.
- `REDEEM_MIN_GAP` — min seconds between Capillary redeem calls (rate-limit guard).
- `NAV_TIMEOUT_MS`, `SETTLE_MS` — per-page navigation timeout and render-settle wait.
