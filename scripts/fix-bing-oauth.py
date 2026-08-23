#!/usr/bin/env python3
"""Provision the Microsoft Advertising service principal, then mint a refresh token.

Background: get-bing-refresh-token.py fails with AADSTS650052 because the
blackhilltx.com tenant has no service principal for the Microsoft Advertising API
(app id d42ffc93-c136-491d-b4fd-6f18168c68fd). A Global Administrator granting
admin consent creates that service principal as a side effect.

This runs both steps against the tenant-specific endpoint (not /common), so the
sign-in is forced to the work account rather than the Google-federated one.

    python3 scripts/fix-bing-oauth.py

Stdlib only. Step 1 needs Global Administrator. Step 2 needs an account that is a
Super Admin on Microsoft Advertising account 187239855.
"""
import http.server, json, os, sys, urllib.error, urllib.parse, urllib.request, webbrowser

TENANT = "f6bc761a-9636-4ea3-a9f8-8c41c38d8794"
ADS_API_APP_ID = "d42ffc93-c136-491d-b4fd-6f18168c68fd"
CFG = os.path.expanduser("~/.config/bing-ads/config.json")

cfg = json.load(open(CFG))
redirect = cfg["redirect_uri"]
port = int(urllib.parse.urlparse(redirect).port or 8400)
result = {}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        result.clear()
        result.update({k: v[0] for k, v in q.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        ok = "error" not in result
        self.wfile.write(
            b"<h2>Done. Close this tab and return to the terminal.</h2>" if ok
            else b"<h2>Something went wrong. Check the terminal.</h2>")

    def log_message(self, *a):
        pass


def listen():
    httpd = http.server.HTTPServer(("localhost", port), H)
    httpd.handle_request()
    httpd.server_close()


def step(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


# --- Step 1: admin consent (creates the missing service principal) ---
step("STEP 1 of 2 - Admin consent (sign in as a Global Administrator)")
consent = f"https://login.microsoftonline.com/{TENANT}/v2.0/adminconsent?" + urllib.parse.urlencode({
    "client_id": cfg["client_id"],
    "scope": cfg["scope"],
    "redirect_uri": redirect,
})
print("Opening your browser. If it does not open, paste this:\n\n" + consent + "\n")
webbrowser.open(consent)
print(f"Waiting for the consent redirect on {redirect} ...")
listen()

if "error" in result:
    desc = result.get("error_description", result["error"])
    print("\nAdmin consent FAILED:\n  " + desc[:600])
    if "650052" in desc or "service principal" in desc.lower():
        print(
            "\nThe tenant still cannot create the Microsoft Advertising service principal.\n"
            "Fallback, run as Global Administrator:\n\n"
            "  pwsh -Command 'Install-Module Microsoft.Graph.Applications -Scope CurrentUser -Force;"
            f" Connect-MgGraph -Scopes \"Application.ReadWrite.All\"; New-MgServicePrincipal -AppId {ADS_API_APP_ID}'\n")
    raise SystemExit(1)

print("Admin consent granted. Service principal should now exist.")

# --- Step 2: authorization code -> refresh token ---
step("STEP 2 of 2 - Sign in as a Super Admin on the ad account")
auth = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?" + urllib.parse.urlencode({
    "client_id": cfg["client_id"],
    "scope": cfg["scope"],
    "response_type": "code",
    "redirect_uri": redirect,
    "response_mode": "query",
    "prompt": "select_account",
})
print("Opening your browser. If it does not open, paste this:\n\n" + auth + "\n")
webbrowser.open(auth)
print(f"Waiting for the sign-in redirect on {redirect} ...")
listen()

if "error" in result:
    raise SystemExit("Authorization failed: " + result.get("error_description", result["error"])[:600])
if "code" not in result:
    raise SystemExit("No authorization code returned: " + json.dumps(result)[:300])

data = urllib.parse.urlencode({
    "client_id": cfg["client_id"],
    "client_secret": cfg["client_secret"],
    "code": result["code"],
    "redirect_uri": redirect,
    "grant_type": "authorization_code",
    "scope": cfg["scope"],
}).encode()
token_url = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
try:
    resp = json.loads(urllib.request.urlopen(urllib.request.Request(token_url, data=data)).read())
except urllib.error.HTTPError as e:
    raise SystemExit("Token exchange failed: " + e.read().decode()[:600])

if "refresh_token" not in resp:
    raise SystemExit("No refresh_token returned: " + json.dumps(resp)[:400])

cfg["refresh_token"] = resp["refresh_token"]
json.dump(cfg, open(CFG, "w"), indent=2)
os.chmod(CFG, 0o600)
print("\nSUCCESS: refresh token saved to " + CFG)
print("Tell Claude it's done. The weekly report and offline conversion import can now run.")
