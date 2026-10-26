#!/usr/bin/env python3
"""Print the local Google login as 'authorized user' JSON, for storing as the
GOOGLE_OAUTH_TOKEN GitHub Actions secret (so unattended runs never need a browser).

Prereq: you have already logged in once locally, so token.pkl exists and covers all
SCOPES (Sheets + gmail.send). If not, run `python url_qc.py --limit 1` once and complete
the browser consent first.

Usage:
    python scheduler/export_token.py            # prints the JSON to stdout AND copies it to the clipboard
    python scheduler/export_token.py > token.json   # or save it to paste into the secret
    python scheduler/export_token.py --no-clip  # skip the clipboard copy

The JSON is copied to the clipboard automatically (Windows clip / macOS pbcopy / Linux
xclip|xsel), so you can paste it straight into the GitHub secret without a separate step.

The output contains a long-lived refresh token — treat it like a password. Do NOT commit it.
"""
import os, pickle, subprocess, sys


def copy_to_clipboard(text):
    """Copy text to the OS clipboard. Returns True on success, False otherwise."""
    if sys.platform == "win32":
        candidates = [["clip"]]
    elif sys.platform == "darwin":
        candidates = [["pbcopy"]]
    else:
        candidates = [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for cmd in candidates:
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            continue
    return False

ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_PATH = os.path.join(ROOT, "token.pkl")

if not os.path.exists(TOKEN_PATH):
    sys.exit(f"{TOKEN_PATH} not found. Log in once locally first "
             "(e.g. `python url_qc.py --limit 1`), then re-run this.")

with open(TOKEN_PATH, "rb") as f:
    creds = pickle.load(f)

to_json = getattr(creds, "to_json", None)
if not callable(to_json):
    sys.exit("token.pkl is not a google.oauth2 Credentials object; cannot export.")

if not getattr(creds, "refresh_token", None):
    sys.stderr.write("WARNING: this token has no refresh_token — unattended refresh will "
                     "fail. Revoke access and log in again to mint one with offline access.\n")

# creds.to_json() emits {token, refresh_token, token_uri, client_id, client_secret, scopes}
# — everything needed to refresh headless, with no oauth_credentials.json required in CI.
payload = creds.to_json()
sys.stdout.write(payload)
sys.stdout.write("\n")

# Also drop it on the clipboard so it can be pasted straight into the GitHub secret.
# Notes go to stderr to keep stdout clean for `> token.json` redirection.
if "--no-clip" not in sys.argv[1:]:
    if copy_to_clipboard(payload):
        sys.stderr.write("Copied token JSON to the clipboard.\n")
    else:
        sys.stderr.write("Could not copy to clipboard (no clip/pbcopy/xclip/xsel found); "
                         "the JSON above is still valid.\n")
