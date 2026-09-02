#!/usr/bin/env python3
"""
url_qc.py — Daily URL QC for offers across 3 brands (Santander / Frontier / TDBANK).

For every offer in the "QC Worklist" tab of the Google Sheet, this:
  1. Opens the redemption_url in headless Chromium (follows all redirects).
  2. Captures a screenshot of the FINAL landing page.
  3. Records final URL, HTTP status, PASS/FAIL result and a note.

Outputs (all under this script's folder):
  screenshots/<YYYY-MM-DD>/<Brand>/<offer-id>.png   one screenshot per offer
  reports/qc_<YYYY-MM-DD>.csv                        machine-readable report
  a dated "QC Run <YYYY-MM-DD>" tab in the Sheet     (unless --no-sheet)

Usage:
  python url_qc.py                 # full daily run
  python url_qc.py --limit 10      # quick test on first 10 offers
  python url_qc.py --brand Frontier
  python url_qc.py --no-sheet      # local screenshots + CSV only
"""
import os, sys, csv, time, argparse, asyncio, re, random, uuid
from datetime import datetime
from urllib.parse import urlparse

import pickle
import requests
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
import gspread
from playwright.async_api import async_playwright

# ── Config ─────────────────────────────────────────────────────────────────────
SPREADSHEET_ID = "1bkDvHQ1NdzvTjsBVsvAQRuJWWNw8tZjD7lVK7et7dc8"
SCOPES         = ["https://www.googleapis.com/auth/spreadsheets"]
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
CREDS_PATH     = os.path.join(SCRIPT_DIR, "oauth_credentials.json")
TOKEN_PATH     = os.path.join(SCRIPT_DIR, "token.pkl")
SCREENSHOT_ROOT= os.path.join(SCRIPT_DIR, "screenshots")
REPORT_DIR     = os.path.join(SCRIPT_DIR, "reports")
CHROME_PROFILE = os.path.join(SCRIPT_DIR, ".chrome_profile")  # persistent real-Chrome profile (final tier)

CONCURRENCY       = 4        # parallel pages for normal merchant/affiliate links
REDEEM_CONCURRENCY= 2        # parallel pages for the Capillary redeem/redirect endpoint
REDEEM_MIN_GAP    = 1.2      # min seconds between two redeem calls (rate-limit guard)
NAV_TIMEOUT_MS = 30000       # per-page navigation timeout
SETTLE_MS      = 2000        # extra wait after load so redirects/JS settle

# Blank-screenshot guard: a 200 page can still paint late (heavy SPA). Before/after
# the screenshot we wait for real content and re-take if the image comes out blank.
BLANK_STD_THRESH = 3.0       # mean per-channel pixel std below this = near-uniform (blank)
BLANK_MAX_TRIES  = 3         # how many times to wait+retake before giving up
BLANK_WAIT_MS    = 3000      # extra wait between content checks / blank re-takes
CONTENT_MIN      = 30        # min "content score" (visible text length + media*50) to proceed
VIEWPORT       = {"width": 1366, "height": 900}
# Coherent UA per engine — a Chrome UA on the Firefox engine is itself a bot tell, so the
# reconfirm (Firefox) pass sends a real Firefox UA. This gives two *consistent*, different
# fingerprints, which is what gets a bot-blocked (403) offer through on the second look.
CHROME_UA  = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0"
WEBKIT_UA  = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.4 Safari/605.1.15")
USER_AGENT = CHROME_UA
RECHECK_UA = {"Firefox": FIREFOX_UA, "WebKit": WEBKIT_UA, "Chromium(2nd)": CHROME_UA}

# Final landing hosts that mean the offer is misconfigured (200 but junk target)
JUNK_HOSTS = {"", "null.com", "www.null.com", ".com", "www..com"}

# Template placeholders in redemption URLs get replaced with a real test value BEFORE
# checking (e.g. TDBANK URLs carry ?user-id={CAP-UID}). Fill in a valid test user-id to
# actually exercise those redemptions; leave a token out and its URLs are marked TEMPLATE.
SUBSTITUTIONS = {
    # "{CAP-UID}": "REPLACE_WITH_TEST_USER_ID",
}

WL_HEADER_KEY = "redemption_url"   # column that must exist to identify the worklist tab

# ── Bifrost affiliate-bff (source/offerSource = BI / BIFROST) ────────────────────
# BI/BIFROST offers have no ready redemption_url in the sheet. For those we call the
# affiliate-bff filter-offer API with offerId = the sheet's external_offer_id, and use
# the response's translations[].activatedUrl as the redemption_url to QC.
BIFROST_API   = ("https://api-eu-west-1.rewardsplus.capillarytech.com"
                 "/affiliate-bff/api/v1/offers/filter-offer")
BIFROST_SOURCES = {"BI", "BIFROST"}     # source/offerSource values that use this API
BIFROST_LANG_PREF = "en"                # preferred translation language for activatedUrl
# Bearer token for the API — it EXPIRES (~10h). Paste a fresh one (from bifrost-ui:
# DevTools > Network > filter-offer > Request Headers > authorization) into the file
# bifrost_token.txt next to this script (preferred), or into BIFROST_TOKEN below.
BIFROST_TOKEN      = ""
BIFROST_TOKEN_FILE = os.path.join(SCRIPT_DIR, "bifrost_token.txt")

def get_bifrost_token():
    """Return the Bearer token (file wins over the constant); '' if none. A leading
    'Bearer ' and surrounding whitespace are stripped so either form can be pasted."""
    tok = ""
    if os.path.exists(BIFROST_TOKEN_FILE):
        with open(BIFROST_TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
    tok = tok or BIFROST_TOKEN.strip()
    return re.sub(r"(?i)^bearer\s+", "", tok).strip()

def resolve_bifrost_activated_url(external_offer_id, token):
    """Look up one BI/BIFROST offer and return (activated_url, error).
    error == 'AUTH' signals an expired/invalid token (the whole run should stop)."""
    raw = str(external_offer_id or "").strip()
    if not raw:
        return "", "no external_offer_id"
    try:
        oid = int(raw)
    except ValueError:
        return "", f"external_offer_id '{raw}' is not numeric"
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "origin":  "https://bifrost-ui.rewardsplus.capillarytech.com",
        "referer": "https://bifrost-ui.rewardsplus.capillarytech.com/",
        "user-agent": CHROME_UA,
        "x-correlation-id": str(uuid.uuid4()),
    }
    body = {"page": 0, "size": 10, "offerId": oid}
    try:
        r = requests.post(BIFROST_API, headers=headers, json=body, timeout=(10, 20))
    except Exception as e:
        return "", f"{type(e).__name__}: {str(e).splitlines()[0][:100]}"
    if r.status_code in (401, 403):
        return "", "AUTH"
    if r.status_code >= 400:
        return "", f"Bifrost HTTP {r.status_code}"
    try:
        offers = (r.json().get("data") or {}).get("offers") or []
    except Exception:
        return "", "Bifrost returned non-JSON"
    if not offers:
        return "", f"offerId {oid} not found in Bifrost"
    trans = offers[0].get("translations") or []
    url = next((t.get("activatedUrl") for t in trans
                if t.get("languageCode") == BIFROST_LANG_PREF and t.get("activatedUrl")), "")
    if not url:
        url = next((t.get("activatedUrl") for t in trans if t.get("activatedUrl")), "")
    if not url:
        return "", "no activatedUrl in Bifrost response"
    return url, ""

def resolve_bifrost_offers(offers, worksheet=None):
    """For every BI/BIFROST offer, replace its url with the Bifrost activatedUrl.
    Offers that can't be resolved get a 'bifrost_error' note (reported FAIL later).
    If `worksheet` is given, resolved urls are written back into its redemption_url column."""
    todo = [o for o in offers if o.get("source", "").strip().upper() in BIFROST_SOURCES]
    if not todo:
        return
    token = get_bifrost_token()
    if not token:
        sys.exit(f"ERROR: {len(todo)} BI/BIFROST offers need a token. "
                 f"Paste one into {BIFROST_TOKEN_FILE}.")
    print(f"Resolving activatedUrl for {len(todo)} BI/BIFROST offers via Bifrost API...")
    ok = 0
    for i, o in enumerate(todo, 1):
        url, err = resolve_bifrost_activated_url(o.get("external_offer_id"), token)
        if err == "AUTH":
            sys.exit("ERROR: Bifrost token expired/invalid (HTTP 401/403). "
                     f"Refresh it in {BIFROST_TOKEN_FILE} and re-run.")
        if url:
            o["url"] = url
            ok += 1
        else:
            o["url"] = o.get("url") or ""
            o["bifrost_error"] = err
        if i % 25 == 0 or i == len(todo):
            print(f"  ...resolved {i}/{len(todo)} ({ok} ok)")
        time.sleep(0.15)   # gentle throttle against the shared backend
    print(f"Bifrost resolve done: {ok}/{len(todo)} got an activatedUrl.")
    if worksheet is not None:
        _write_bifrost_urls_back(worksheet, todo)
    print()

def _write_bifrost_urls_back(worksheet, todo):
    """Write each resolved activatedUrl into the worklist's redemption_url column."""
    from gspread.utils import rowcol_to_a1
    updates = [{"range": rowcol_to_a1(o["_row"], o["_rdm_col"]), "values": [[o["url"]]]}
               for o in todo if o.get("url") and not o.get("bifrost_error")]
    if not updates:
        return
    try:
        for j in range(0, len(updates), 200):            # chunk to keep each request small
            worksheet.batch_update(updates[j:j + 200], value_input_option="RAW")
        print(f"Wrote {len(updates)} activatedUrl(s) back to '{worksheet.title}' redemption_url.")
    except Exception as e:
        print(f"  (writing urls back to the sheet failed: {e})")

# ── Outbound proxy (optional) ────────────────────────────────────────────────────
# A residential/UK proxy is the one lever that clears IP/geo-scored WAF blocks — the
# 403/429 WARNs from Expedia/Oakley/Argos etc. that stay blocked no matter the browser
# fingerprint. It is applied to the merchant-facing traffic ONLY (browser navigation +
# the redirect-resolution requests), NOT the Capillary Bifrost API call (internal API).
# Set it via (first found wins): env QC_PROXY, the file proxy.txt, or PROXY below.
# Format: scheme://[user:pass@]host:port
#   http://user:pass@gw.provider.com:7000   or   socks5://user:pass@host:1080
PROXY      = ""
PROXY_FILE = os.path.join(SCRIPT_DIR, "proxy.txt")
HTTP_PROXIES = None   # set in main(): {"http": url, "https": url} for the requests calls

def get_proxy():
    """Return (playwright_proxy | None, requests_proxies | None, masked_str) from
    env QC_PROXY / proxy.txt / PROXY. Password is masked in the returned display string."""
    from urllib.parse import urlsplit
    url = os.environ.get("QC_PROXY", "").strip()
    if not url and os.path.exists(PROXY_FILE):
        with open(PROXY_FILE, encoding="utf-8") as f:
            url = f.read().strip()
    url = url or PROXY.strip()
    if not url:
        return None, None, ""
    u = urlsplit(url)
    server = f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
    pw = {"server": server}
    if u.username: pw["username"] = u.username
    if u.password: pw["password"] = u.password
    masked = server + (f"  (user {u.username})" if u.username else "")
    return pw, {"http": url, "https": url}, masked

# ── Auth (matches the pattern used by the other automations in this account) ─────
def get_credentials():
    creds = None
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow  = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)
    return creds

# ── Read the worklist from the Sheet ────────────────────────────────────────────
WORKLIST_TAB = "BrandName_Export"   # master 3-brand worklist; auto-detected if renamed
RESULTS_TAB  = "URL QC"             # fixed tab that results are overwritten into each run

def load_worklist(spreadsheet):
    """Return offers from the master worklist tab (has both BrandName + redemption_url).

    Prefers the tab named WORKLIST_TAB; otherwise picks the tab having both a
    'brandname' and 'redemption_url' column with the most data rows.
    """
    best = None  # (num_rows, worksheet, values, header_low)
    for ws in spreadsheet.worksheets():
        try:
            values = ws.get_all_values()
        except Exception:
            continue
        if not values:
            continue
        low = [h.strip().lower() for h in values[0]]
        if "brandname" in low and WL_HEADER_KEY in low:
            if ws.title == WORKLIST_TAB:
                best = (len(values), ws, values, low)
                break
            if best is None or len(values) > best[0]:
                best = (len(values), ws, values, low)
    if best is None:
        raise RuntimeError("No worklist tab with both 'BrandName' and 'redemption_url'.")

    _, ws, values, low = best
    idx = {name: i for i, name in enumerate(low)}
    def col(row, key):
        i = idx.get(key)
        return row[i].strip() if (i is not None and i < len(row)) else ""
    rdm_col = (idx.get("redemption_url", 0)) + 1   # 1-based column for writing url back
    offers = []
    for ridx, row in enumerate(values[1:], start=2):   # start=2: header is row 1
        url    = col(row, "redemption_url")
        source = col(row, "source/offersource") or col(row, "source") or col(row, "offersource")
        # BI/BIFROST offers carry no redemption_url in the sheet; their url is resolved
        # later from the Bifrost API, so don't skip them for having a blank url.
        if not url and source.strip().upper() not in BIFROST_SOURCES:
            continue
        offers.append({
            "brand":    col(row, "brandname") or "Unknown",
            "sku":      col(row, "sku/offer id") or col(row, "offer id") or col(row, "sku"),
            "merchant": col(row, "merchant_name"),
            "otype":    col(row, "offer_type"),
            "url":      url,
            "external_offer_id": col(row, "external_offer_id"),
            "source":   source,
            "_row":     ridx,          # sheet row, for writing resolved urls back
            "_rdm_col": rdm_col,       # redemption_url column (1-based)
        })
    n_bi = sum(1 for o in offers if o["source"].strip().upper() in BIFROST_SOURCES)
    print(f"Worklist tab: '{ws.title}'  ->  {len(offers)} offers "
          f"({n_bi} BI/BIFROST resolved via API, {len(offers)-n_bi} with a direct redemption_url)")
    return offers, ws

# ── Helpers ─────────────────────────────────────────────────────────────────────
def safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s.strip())
    return s[:80] or "offer"

def classify(status, final_url, error):
    """Return (result, note). result in {PASS, WARN, FAIL}.
      FAIL  = broken: no response, junk landing host, or a hard 4xx/5xx.
      WARN  = reached the right place but guarded (bot-block / rate-limit) — eyeball the screenshot.
      PASS  = landed on a real page (2xx, or the merchant's own 3xx homepage redirect).
    """
    host = urlparse(final_url or "").netloc.lower()
    if error:
        return "FAIL", error
    if host in JUNK_HOSTS or final_url.startswith(("http://.com", "https://.com")):
        return "FAIL", f"junk landing host '{host or final_url}'"
    if status is None:
        return "FAIL", "no HTTP response"
    if status in (401, 403, 418, 429):
        return "WARN", f"HTTP {status} (bot-block/rate-limit - check screenshot)"
    if status >= 400:
        return "FAIL", f"HTTP {status}"
    return "PASS", f"HTTP {status}"

# ── QC one offer ────────────────────────────────────────────────────────────────
TRANSIENT = ("err_http2", "err_connection", "err_network", "err_aborted",
             "err_timed_out", "timeout", "err_socket", "err_empty_response")

REQ_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-GB,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Bot-block / rate-limit statuses. These get a LONGER cooldown before each retry tier,
# since a 5-10s wait rarely clears a 429/418 rate-limit window.
BOTBLOCK = {401, 403, 418, 429, 503}

def is_transient(status, error):
    if status in (429, 503):
        return True
    e = (error or "").lower()
    return any(t in e for t in TRANSIENT)

def is_redeem(url):
    return "bifrost/redeem" in url or "capillary-bff/api/v1/bifrost/redemption" in url

def apply_subs(url):
    """Replace known template placeholders; return (effective_url, leftover_tokens)."""
    for token, value in SUBSTITUTIONS.items():
        url = url.replace(token, value)
    leftover = re.findall(r"\{[^}]+\}", url)
    return url, leftover

def resolve_redirects(url, max_hops=12):
    """Follow server redirects over HTTP/1.1 (header-only, no body download) to the
    final landing URL. Returns (status, final_url, error).

    This sidesteps Chromium's ERR_HTTP2_PROTOCOL_ERROR against the Capillary redeem
    endpoint and some affiliate hosts, and never stalls on a slow merchant body.
    """
    cur, status, err = url, None, ""
    try:
        for _ in range(max_hops):
            r = requests.get(cur, headers=REQ_HEADERS, timeout=(10, 12),
                             allow_redirects=False, stream=True, proxies=HTTP_PROXIES)
            status = r.status_code
            loc = r.headers.get("location")
            r.close()
            if 300 <= status < 400 and loc:
                cur = requests.compat.urljoin(cur, loc)
                continue
            break
    except Exception as e:
        err = f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"
    return status, cur, err

# JS that scores how much visible content a page has rendered (text length + media count)
CONTENT_JS = """() => {
  const b = document.body; if (!b) return 0;
  const text = (b.innerText || '').trim().length;
  let media = 0;
  for (const el of document.querySelectorAll('img,svg,canvas,video,picture')) {
    const r = el.getBoundingClientRect();
    if (r.width * r.height > 2500) media++;
  }
  return text + media * 50;
}"""

def _is_blank_image(path):
    """True if the screenshot is essentially one flat colour (white/black/unrendered)."""
    try:
        from PIL import Image
        import numpy as np
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((200, 200))          # downsample: fast + ignores tiny specks
            arr = np.asarray(im, dtype="float32").reshape(-1, 3)
        return float(arr.std(axis=0).mean()) < BLANK_STD_THRESH
    except Exception:
        return False   # can't analyse -> don't loop, keep whatever we have

async def _capture_nonblank(page, shot_path):
    """Screenshot the page, waiting a few extra seconds and retrying if it renders blank.
    Handles heavy SPAs that return 200 but paint their content late."""
    async def score():
        try:
            return await page.evaluate(CONTENT_JS)
        except Exception:
            return 0

    # 1) Wait for the DOM to actually have visible content before shooting.
    for _ in range(BLANK_MAX_TRIES):
        if await score() >= CONTENT_MIN:
            break
        try:                                   # nudge lazy-loaded / on-scroll content
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        await page.wait_for_timeout(BLANK_WAIT_MS)

    # 2) Take the shot; if the pixels come out blank, wait and re-take.
    for _ in range(BLANK_MAX_TRIES):
        try:
            await page.screenshot(path=shot_path, full_page=True)
        except Exception:
            try:
                await page.screenshot(path=shot_path)   # viewport fallback
            except Exception:
                return
        if not _is_blank_image(shot_path):
            return
        await page.wait_for_timeout(BLANK_WAIT_MS)

async def _humanize(page):
    """A few human-like mouse moves + a small scroll before the screenshot. Softens the
    behavioural signals (no pointer motion, instant paint) that DataDome/Akamai score on."""
    try:
        for _ in range(2):
            await page.mouse.move(random.randint(50, VIEWPORT["width"] - 50),
                                  random.randint(50, VIEWPORT["height"] - 50),
                                  steps=random.randint(4, 9))
            await page.wait_for_timeout(random.randint(90, 260))
        await page.mouse.wheel(0, random.randint(200, 550))
        await page.wait_for_timeout(random.randint(150, 350))
    except Exception:
        pass

async def _screenshot_page(context, url, shot_path, max_attempts=2):
    """Navigate to a (already-resolved) URL and screenshot it.
    Returns (browser_status, browser_final_url, browser_error)."""
    b_status, b_final, b_err = None, "", ""
    for attempt in range(1, max_attempts + 1):
        page = await context.new_page()
        try:
            resp = await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
            b_status = resp.status if resp else None
            for state in ("domcontentloaded", "load", "networkidle"):
                try:
                    await page.wait_for_load_state(state, timeout=8000)
                except Exception:
                    break
            await page.wait_for_timeout(SETTLE_MS)
            await _humanize(page)
            b_final, b_err = page.url, ""
            await _capture_nonblank(page, shot_path)
            await page.close()
            return b_status, b_final, b_err
        except Exception as e:
            b_err = f"{type(e).__name__}: {str(e).splitlines()[0][:140]}"
            b_final = page.url if page.url and page.url != "about:blank" else ""
            try:
                await page.screenshot(path=shot_path)
            except Exception:
                pass
            await page.close()
            if attempt == max_attempts or not is_transient(b_status, b_err):
                break
            await asyncio.sleep(2.5 * attempt)
    return b_status, b_final, b_err

# Injected before any page script runs: erase the obvious "I'm an automated browser"
# tells that bot walls (Akamai / DataDome / Cloudflare) fingerprint. Covers the same
# ground as the playwright-stealth package (webdriver, plugins, chrome object,
# permissions, WebGL vendor/renderer, hardwareConcurrency/deviceMemory, userAgentData)
# without taking on that version-fragile dependency.
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-GB', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};
window.chrome.app = window.chrome.app || { isInstalled: false };
try {
  // userAgentData: headless Chrome often exposes an empty/typo'd brand list.
  if (navigator.userAgentData) {
    Object.defineProperty(navigator, 'userAgentData', {get: () => ({
      brands: [{brand: 'Chromium', version: '124'},
               {brand: 'Google Chrome', version: '124'},
               {brand: 'Not-A.Brand', version: '99'}],
      mobile: false, platform: 'Windows',
    })});
  }
} catch (e) {}
try {
  const _q = navigator.permissions && navigator.permissions.query;
  if (_q) navigator.permissions.query = (p) =>
    (p && p.name === 'notifications')
      ? Promise.resolve({ state: Notification.permission })
      : _q(p);
} catch (e) {}
try {
  // WebGL vendor/renderer: headless reports 'Google SwiftShader' / 'Google Inc.' —
  // a strong bot tell. Spoof a real Intel GPU string.
  const spoof = (proto) => {
    const _gp = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 37445) return 'Intel Inc.';                       // UNMASKED_VENDOR_WEBGL
      if (p === 37446) return 'Intel Iris OpenGL Engine';          // UNMASKED_RENDERER_WEBGL
      return _gp.apply(this, [p]);
    };
  };
  if (window.WebGLRenderingContext)  spoof(WebGLRenderingContext.prototype);
  if (window.WebGL2RenderingContext) spoof(WebGL2RenderingContext.prototype);
} catch (e) {}
"""

async def new_hardened_context(browser, user_agent):
    """A context that looks like a real user's browser: coherent UA, realistic headers,
    a fixed locale/timezone, and the webdriver tells scrubbed. Reduces 403 bot-blocks."""
    ctx = await browser.new_context(
        viewport=VIEWPORT,
        user_agent=user_agent,
        locale="en-GB",
        timezone_id="Europe/London",
        ignore_https_errors=True,
        extra_http_headers={
            "Accept-Language": "en-GB,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    await ctx.add_init_script(STEALTH_JS)
    return ctx

async def _probe(context, eff_url, shot_path, throttle):
    """One full check with a given browser context: resolve the redirect chain over
    HTTP/1.1 (throttling the redeem endpoint), then load + screenshot the final landing
    page. Returns (status, final_url, error) — prefers the browser's signals, falls back
    to the requests-level ones."""
    if throttle is not None and is_redeem(eff_url):
        await throttle()
    r_status, landing, r_err = await asyncio.to_thread(resolve_redirects, eff_url)
    target = landing or eff_url

    b_status, b_final, b_err = await _screenshot_page(context, target, shot_path)

    final_url = b_final or landing or ""
    status    = b_status if b_status is not None else r_status
    error     = ""
    if status is None:
        error = b_err or r_err or "no HTTP response"
    return status, final_url, error

async def qc_offer(context, recheck_context, recheck_engine, chrome_context,
                   offer, shot_dir, sem, throttle, i, total):
    async with sem:
        # 0) A BI/BIFROST offer whose activatedUrl couldn't be resolved has nothing to test.
        if offer.get("bifrost_error"):
            note = f"Bifrost: {offer['bifrost_error']}"
            print(f"  [{i}/{total}] XX {offer['brand']:<9} {offer['sku'][:22]:<22} FAIL ({note})")
            return {**offer, "final_url": "", "http_status": "", "result": "FAIL",
                    "note": note, "screenshot": ""}

        # 0b) Substitute template placeholders; if any remain unresolved, we can't test it
        eff_url, leftover = apply_subs(offer["url"])
        if leftover:
            note = f"unresolved placeholder {' '.join(sorted(set(leftover)))} - set it in SUBSTITUTIONS"
            print(f"  [{i}/{total}] -- {offer['brand']:<9} {offer['sku'][:22]:<22} TEMPLATE ({note})")
            return {**offer, "final_url": "", "http_status": "", "result": "TEMPLATE",
                    "note": note, "screenshot": ""}

        # 1) First pass in the primary browser (Chromium)
        fname = f"{safe_name(offer['sku'] or offer['merchant'] or str(i))}.png"
        shot_path = os.path.join(shot_dir, fname)
        status, final_url, error = await _probe(context, eff_url, shot_path, throttle)
        result, note = classify(status, final_url, error)
        reconfirmed = ""

        # 2) Anything that isn't a clean HTTP 200 is treated as possibly flaky/bot-blocked.
        #    Escalate through stronger fingerprints, pausing 5-10s before each, and STOP the
        #    moment one returns 200. Each pass overwrites the screenshot and becomes the result.
        #      tier 1: a DIFFERENT engine (Firefox/WebKit) — coherent, alternative fingerprint
        #      tier 2: the REAL installed Chrome (channel=chrome) — strongest, most human
        escalations = [(recheck_context, recheck_engine), (chrome_context, "Chrome(real)")]
        first_status, chain = None, []
        for stage_ctx, stage_name in escalations:
            if stage_ctx is None or status == 200:
                break
            if first_status is None:
                first_status = status if status is not None else "no-response"
            chain.append(stage_name)
            # 429 rate-limits need a real cooldown to clear the limit window; other
            # bot-blocks (403/418/503) get a medium wait; plain non-200s a short settle.
            if status == 429:
                wait = random.uniform(35, 55)
            elif status in BOTBLOCK:
                wait = random.uniform(15, 25)
            else:
                wait = random.uniform(5, 10)
            await asyncio.sleep(wait)
            status, final_url, error = await _probe(stage_ctx, eff_url, shot_path, throttle)
            result, note = classify(status, final_url, error)
        if chain:
            reconfirmed = f"{chain[-1]} (was {first_status})"
            note = (f"{note} [reconfirmed via {' -> '.join(chain)}; "
                    f"Chromium first pass was HTTP {first_status}]")

        mark = {"PASS": "OK ", "WARN": ".. ", "FAIL": "XX "}[result]
        print(f"  [{i}/{total}] {mark}{offer['brand']:<9} {offer['sku'][:22]:<22} "
              f"{result} ({note})")
        return {
            **offer,
            "final_url": final_url,
            "http_status": status if status is not None else "",
            "result": result,
            "reconfirmed": reconfirmed,
            "note": note,
            "screenshot": shot_path if os.path.exists(shot_path) else "",
        }

async def run_qc(offers, run_date, on_result, proxy=None):
    """Check every offer; call on_result(dict) as each finishes (streaming, so a kill
    mid-run still keeps everything completed so far).
    `proxy` (a Playwright proxy dict or None) is applied to every browser engine."""
    shot_base = os.path.join(SCREENSHOT_ROOT, run_date)
    sem_default = asyncio.Semaphore(CONCURRENCY)       # normal merchant/affiliate links
    sem_redeem  = asyncio.Semaphore(REDEEM_CONCURRENCY) # rate-limited Capillary redeem host

    # min-interval throttle shared across all redeem calls
    _last = {"t": 0.0}
    _lock = asyncio.Lock()
    async def throttle():
        async with _lock:
            wait = REDEEM_MIN_GAP - (time.monotonic() - _last["t"])
            if wait > 0:
                await asyncio.sleep(wait)
            _last["t"] = time.monotonic()

    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy=proxy, args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ])
        context = await new_hardened_context(browser, CHROME_UA)

        # Second, DIFFERENT browser engine, used only to reconfirm non-200 results.
        # Prefer Firefox, then WebKit; fall back to a separate Chromium instance if
        # neither is installed (run `playwright install firefox webkit` to enable them).
        recheck_browser, recheck_engine = None, None
        for name, btype in (("Firefox", p.firefox), ("WebKit", p.webkit)):
            try:
                recheck_browser = await btype.launch(headless=True, proxy=proxy)
                recheck_engine = name
                break
            except Exception as e:
                print(f"  (reconfirm browser {name} unavailable: {str(e).splitlines()[0][:80]})")
        if recheck_browser is None:
            recheck_browser = await p.chromium.launch(headless=True, proxy=proxy, args=[
                "--no-sandbox", "--disable-blink-features=AutomationControlled"])
            recheck_engine = "Chromium(2nd)"
        recheck_context = await new_hardened_context(
            recheck_browser, RECHECK_UA.get(recheck_engine, CHROME_UA))
        print(f"Reconfirm engine for non-200 results: {recheck_engine}")

        # Final tier: the REAL installed Chrome (channel='chrome') — strongest, most human
        # fingerprint — used only for offers still blocked after the engine swap (e.g. 403).
        # Two upgrades over a plain launch for beating enterprise WAFs:
        #   * NEW headless (--headless=new, enabled by launching headed) — the old headless
        #     mode is itself a giant bot tell; the new mode fingerprints like real Chrome.
        #   * a PERSISTENT profile (user-data-dir) — cookies / TLS session / device signals
        #     carry across the run, so later hits on the same WAF look like a returning user.
        chrome_browser, chrome_context = None, None   # persistent context => no separate browser
        try:
            os.makedirs(CHROME_PROFILE, exist_ok=True)
            chrome_context = await p.chromium.launch_persistent_context(
                CHROME_PROFILE,
                channel="chrome",
                headless=False,                        # + --headless=new below = new headless, no window
                proxy=proxy,
                args=["--headless=new", "--no-sandbox",
                      "--disable-blink-features=AutomationControlled"],
                viewport=VIEWPORT,
                user_agent=CHROME_UA,
                locale="en-GB",
                timezone_id="Europe/London",
                ignore_https_errors=True,
                extra_http_headers={"Accept-Language": "en-GB,en;q=0.9",
                                    "Upgrade-Insecure-Requests": "1"},
            )
            chrome_context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            await chrome_context.add_init_script(STEALTH_JS)
            print("Final-tier reconfirm for stubborn blocks: real Chrome (new headless + persistent profile)\n")
        except Exception as e:
            chrome_context = None
            print(f"  (real Chrome unavailable, skipping final tier: {str(e).splitlines()[0][:80]})\n")

        for o in offers:
            os.makedirs(os.path.join(shot_base, safe_name(o["brand"])), exist_ok=True)
        total = len(offers)
        tasks = []
        for i, o in enumerate(offers, 1):
            shot_dir = os.path.join(shot_base, safe_name(o["brand"]))
            sem = sem_redeem if is_redeem(o["url"]) else sem_default
            tasks.append(qc_offer(context, recheck_context, recheck_engine, chrome_context,
                                  o, shot_dir, sem, throttle, i, total))
        for coro in asyncio.as_completed(tasks):
            r = await coro
            on_result(r)
            results.append(r)
        await context.close()
        await browser.close()
        await recheck_context.close()
        await recheck_browser.close()
        if chrome_context is not None:
            await chrome_context.close()
        if chrome_browser is not None:
            await chrome_browser.close()
    return results

# ── Outputs ─────────────────────────────────────────────────────────────────────
COLS = ["checked_at", "brand", "sku", "merchant", "offer_type", "redemption_url",
        "final_url", "http_status", "result", "reconfirmed", "note", "screenshot"]

def csv_path_for(run_date):
    return os.path.join(REPORT_DIR, f"qc_{run_date}.csv")

def result_to_row(r, checked_at):
    return [checked_at, r["brand"], r["sku"], r["merchant"], r["otype"],
            r["url"], r["final_url"], r["http_status"], r["result"],
            r.get("reconfirmed", ""), r["note"], r["screenshot"]]

def load_done(path):
    """Return set of (brand, sku) already recorded in today's CSV (for --resume)."""
    done = set()
    if not os.path.exists(path):
        return done
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            done.add((row.get("brand", ""), row.get("sku", "")))
    return done

def load_rows(path):
    """Load all recorded rows from today's CSV as dicts (for rebuilding the Sheet tab)."""
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

RESULT_COLORS = {
    "PASS":     {"red": 0.80, "green": 0.92, "blue": 0.80},
    "WARN":     {"red": 1.00, "green": 0.95, "blue": 0.70},
    "FAIL":     {"red": 0.96, "green": 0.80, "blue": 0.80},
    "TEMPLATE": {"red": 0.87, "green": 0.87, "blue": 0.90},
}

def write_sheet_tab(spreadsheet, run_date):
    """Overwrite the fixed RESULTS_TAB with the color-coded results from today's CSV."""
    rows_in = load_rows(csv_path_for(run_date))
    if not rows_in:
        return None
    title = RESULTS_TAB
    order = {"FAIL": 0, "TEMPLATE": 1, "WARN": 2, "PASS": 3}  # problems first
    rows_in.sort(key=lambda r: (order.get(r.get("result", ""), 3), r.get("brand", "")))
    try:
        ws = spreadsheet.worksheet(title)
        ws.clear()
        ws.resize(rows=len(rows_in) + 10, cols=len(COLS))
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=len(rows_in) + 10, cols=len(COLS))
    grid = [COLS] + [[r.get(c, "") for c in COLS] for r in rows_in]
    ws.update(grid)

    sid = ws.id
    res_col = COLS.index("result")
    reqs = [{
        "repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }
    }, {
        "updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }
    }]
    for i, r in enumerate(rows_in, start=1):
        color = RESULT_COLORS.get(r.get("result", ""))
        if not color:
            continue
        reqs.append({
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": i, "endRowIndex": i + 1,
                          "startColumnIndex": res_col, "endColumnIndex": res_col + 1},
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })
    try:
        spreadsheet.batch_update({"requests": reqs})
    except Exception as e:
        print(f"  (sheet formatting skipped: {e})")
    return title

# ── Main ─────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only check N offers (after offset)")
    ap.add_argument("--offset", type=int, default=0, help="skip the first N offers (chunking)")
    ap.add_argument("--brand", default="", help="only this brand (Santander/Frontier/TDBANK)")
    ap.add_argument("--resume", action="store_true",
                    help="keep today's CSV and skip offers already recorded in it")
    ap.add_argument("--no-sheet", action="store_true", help="don't write the results tab to the Sheet")
    ap.add_argument("--sheet-only", action="store_true",
                    help="just push today's existing CSV to the Sheet tab; don't re-check any URLs")
    ap.add_argument("--no-proxy", action="store_true",
                    help="ignore any configured proxy (QC_PROXY / proxy.txt / PROXY) for this run")
    args = ap.parse_args()

    if not os.path.exists(CREDS_PATH):
        sys.exit(f"ERROR: {CREDS_PATH} not found.")

    # Resolve the optional outbound proxy for merchant-facing traffic (browser + redirect
    # resolution). HTTP_PROXIES (module global) is read by resolve_redirects; pw_proxy is
    # passed to every browser engine in run_qc. The Bifrost API call stays direct.
    global HTTP_PROXIES
    pw_proxy = None
    if not args.no_proxy:
        pw_proxy, HTTP_PROXIES, masked = get_proxy()
        if pw_proxy:
            print(f"Proxy: routing merchant traffic via {masked}")

    run_date   = datetime.now().strftime("%Y-%m-%d")
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(REPORT_DIR, exist_ok=True)
    csv_path = csv_path_for(run_date)

    print("Authenticating with Google Sheets...")
    gc = gspread.authorize(get_credentials())
    ss = gc.open_by_key(SPREADSHEET_ID)
    print(f"Connected: {ss.title}")

    if args.sheet_only:
        tab = write_sheet_tab(ss, run_date)
        print(f"Pushed {len(load_rows(csv_path))} rows from {csv_path} to tab '{tab}'.")
        return

    offers, wl_ws = load_worklist(ss)
    if args.brand:
        offers = [o for o in offers if o["brand"].lower() == args.brand.lower()]
        print(f"Filtered to brand '{args.brand}': {len(offers)} offers")
    if args.offset:
        offers = offers[args.offset:]
        print(f"Skipped first {args.offset} offers")
    if args.limit:
        offers = offers[:args.limit]
        print(f"Limited to {len(offers)} offers")

    # BI/BIFROST offers: resolve redemption_url from the Bifrost API (offerId = external_offer_id)
    # and write it back into the worklist's redemption_url column (skipped under --no-sheet).
    resolve_bifrost_offers(offers, None if args.no_sheet else wl_ws)

    # Resume: skip offers already in today's CSV; else start the CSV fresh.
    done = load_done(csv_path) if args.resume else set()
    new_file = not (args.resume and os.path.exists(csv_path))
    if done:
        before = len(offers)
        offers = [o for o in offers if (o["brand"], o["sku"]) not in done]
        print(f"Resume: {before - len(offers)} already done, {len(offers)} remaining")
    if not offers:
        print("Nothing left to check.")
    else:
        print(f"\nChecking {len(offers)} URLs (concurrency={CONCURRENCY})...\n")
        f = open(csv_path, "a" if not new_file else "w", newline="", encoding="utf-8")
        writer = csv.writer(f)
        if new_file:
            writer.writerow(COLS)
            f.flush()
        def on_result(r):                       # stream each row to disk immediately
            writer.writerow(result_to_row(r, checked_at))
            f.flush()
        t0 = time.time()
        try:
            asyncio.run(run_qc(offers, run_date, on_result, proxy=pw_proxy))
        finally:
            f.close()
        print(f"\n(this chunk took {(time.time()-t0)/60:.1f} min)")

    # Summary from the full CSV (all chunks so far)
    from collections import Counter
    all_rows = load_rows(csv_path)
    by_result = Counter(r.get("result", "") for r in all_rows)
    print(f"\n{'='*60}")
    print(f"Recorded so far: {len(all_rows)}   PASS={by_result['PASS']}  "
          f"WARN={by_result['WARN']}  FAIL={by_result['FAIL']}  TEMPLATE={by_result['TEMPLATE']}")
    fails = Counter(r["brand"] for r in all_rows if r.get("result") == "FAIL")
    if fails:
        print("Failures by brand: " + ", ".join(f"{b}:{n}" for b, n in fails.items()))
    print(f"Screenshots: {os.path.join(SCREENSHOT_ROOT, run_date)}")
    print(f"CSV report : {csv_path}")

    if not args.no_sheet:
        try:
            tab = write_sheet_tab(ss, run_date)
            print(f"Sheet tab  : '{tab}' updated")
        except Exception as e:
            print(f"WARNING: could not write results tab: {e}")
    print("="*60)

if __name__ == "__main__":
    main()
