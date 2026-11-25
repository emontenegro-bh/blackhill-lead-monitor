"""Create the Aspire opportunity for a new Fort Worth irrigation request.

Called by fw-irrigation-monitor.py when a City quote request arrives. What
the API can do is done here; what only the Aspire screens can do is queued:

  API (here):  POST /Opportunities from the "FW Assessment & Repair -
               [Address]" template (status New, start = day received, end =
               one month later, the City's dispatch as Proposal Description),
               then POST /Attachments for each City photo with ExposeToCrew
               on, so the tech sees them on mobile.
  Queued:      the API does not copy the template's services, billing
               company or payment schedule. Those come from the UI steps
               Create Estimate (from template) -> Complete -> $100 in the
               end month -> Won. Each new opportunity is appended to the
               Supabase key fw-opps-to-finish for the session that runs them.
               Emergencies (EMG / EXPEDITE) are flagged there: they get
               emergency pricing and wait for the owner's review before Won.

Tested by hand on 2026-11-24 (nine requests, including one emergency).
"""
import base64
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TEMPLATE_OPPORTUNITY_ID = 3881   # "FW Assessment & Repair - [Address]"
FW_PROPERTY_ID = 1096
DIVISION_ID = 15                 # Irrigation
BRANCH_ID = 2                    # Main
SALES_REP_ID = 6
STATUS_NEW = 9                   # the API only creates opportunities in New
PHOTO_TYPE_ID = 3                # AttachmentTypes: Photo
PHOTO_MAX_PX = 2400              # City photos run to 11 MB; Aspire is happier small
FINISH_QUEUE = "fw-opps-to-finish"
CONFIG_FILE = os.path.expanduser("~/.config/aspire/config.json")

EMERGENCY_RE = re.compile(r"\b(EMG|EMERGENCY|EXPEDITE[DS]?)\b", re.IGNORECASE)
# Sign-off lines that add nothing to the crew's description.
_SIGNOFF_RE = re.compile(r"^(thank(s| you)\b|have a (great|good|nice)|regards\b|sincerely\b)", re.IGNORECASE)

try:
    from zoneinfo import ZoneInfo
    CENTRAL = ZoneInfo("America/Chicago")
except Exception:                          # pragma: no cover
    CENTRAL = timezone(timedelta(hours=-6))


class Aspire:
    def __init__(self):
        cid = os.environ.get("ASPIRE_REPORTING_CLIENT_ID")
        secret = os.environ.get("ASPIRE_REPORTING_SECRET")
        base = "https://cloud-api.youraspire.com"
        if not (cid and secret):
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            base = cfg.get("api_base_url", base)
            cid = cfg.get("reporting_client_id") or cfg.get("api_client_id")
            secret = cfg.get("reporting_secret") or cfg.get("api_secret")
        self.base = base.rstrip("/")
        body = json.dumps({"ClientId": cid, "Secret": secret}).encode()
        req = urllib.request.Request(f"{self.base}/Authorization", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            self.token = json.loads(resp.read().decode())["Token"]

    def _call(self, method, path, body=None):
        req = urllib.request.Request(
            f"{self.base}/{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read().decode()
                if not raw.strip():
                    return None
                try:
                    return json.loads(raw)
                except ValueError:          # some endpoints answer in plain text
                    return raw.strip()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300] if e.fp else ""
            raise RuntimeError(f"Aspire {method} {path.split('?')[0]} {e.code}: {detail}") from e

    def get(self, endpoint, odata_filter):
        return self._call("GET", f"{endpoint}?$filter={urllib.parse.quote(odata_filter)}") or []


def is_emergency(subject, dispatch):
    return bool(EMERGENCY_RE.search(subject or "") or EMERGENCY_RE.search(dispatch or ""))


def title_address(addr):
    """'1234 EXAMPLE OAK TRL' -> '1234 Example Oak Trl'. Mixed-case input
    is left alone; the City types some addresses correctly already."""
    addr = " ".join((addr or "").split())
    if addr and addr == addr.upper():
        return " ".join(w if w[:1].isdigit() else w.capitalize() for w in addr.split())
    return addr


def description_lines(detail_text, wo):
    """The dispatch, one paragraph per line, without the sign-off."""
    out = []
    for line in (detail_text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        if _SIGNOFF_RE.match(line):
            break
        out.append(line)
    if wo and not any(wo in l for l in out):
        out.append(f"WO#{wo}")
    return out


def description_html(lines, emergency):
    from html import escape
    if emergency:
        lines = ["EMERGENCY request"] + lines
    return ('<div style="font-size: 10pt;" id="fontFamilySizeSetting"><div style="font-family: Arial,sans-serif;" '
            'id="fontFamilySetting"><p><strong>Work Order:&nbsp;</strong></p>'
            + "".join(f"<p>{escape(l)}</p>" for l in lines) + "</div></div>")


def window(received):
    """Start = the day the request arrived (Central); end = one month later."""
    start = received.astimezone(CENTRAL).date()
    y, m = (start.year + (start.month // 12), start.month % 12 + 1)
    import calendar
    end = start.replace(year=y, month=m, day=min(start.day, calendar.monthrange(y, m)[1]))
    return start, end


def find_existing(api, wo, address):
    """An opportunity already made for this job, by WO in the description or
    house number + first street word in the name. Guards against the City
    re-sending a request and against a retry after a partial failure."""
    since = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%dT00:00:00Z")
    opps = api.get("Opportunities", f"PropertyID eq {FW_PROPERTY_ID} and CreatedDateTime ge {since}")
    m = re.match(r"\s*(\d+)\s+([A-Za-z]+)", address or "")
    for o in opps:
        if o.get("OpportunityStatusName") == "Lost":
            continue
        if wo and wo in (o.get("ProposalDescription1") or ""):
            return o
        name = (o.get("OpportunityName") or "").lower()
        if m and f" {m.group(1)} {m.group(2).lower()}" in f" {name.split(' - ', 1)[-1]}":
            return o
    return None


def create_opportunity(api, address, lines, received, emergency):
    start, end = window(received)
    body = {
        "OpportunityName": f"FW Assessment & Repair - {title_address(address)}",
        "OpportunityType": "Contract",
        "TemplateOpportunityID": TEMPLATE_OPPORTUNITY_ID,
        "PropertyID": FW_PROPERTY_ID,
        "DivisionID": DIVISION_ID,
        "BranchID": BRANCH_ID,
        "SalesRepID": SALES_REP_ID,
        "InvoiceType": "Fixed Payment",
        "OpportunityStatusID": STATUS_NEW,
        "StartDate": f"{start.isoformat()}T00:00:00Z",
        "EndDate": f"{end.isoformat()}T00:00:00Z",
        "ProposalDescription1": description_html(lines, emergency),
    }
    opp_id = int(api._call("POST", "Opportunities", body))
    o = api.get("Opportunities", f"OpportunityID eq {opp_id}")[0]
    return opp_id, o["OpportunityNumber"]


def shrink(data):
    """JPEG bytes no larger than PHOTO_MAX_PX on a side. Falls back to the
    original if Pillow is missing or the file is not an image it reads."""
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        img.thumbnail((PHOTO_MAX_PX, PHOTO_MAX_PX))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=80, optimize=True)
        return out.getvalue()
    except Exception:
        return data


def upload_photo(api, opp_id, name, data):
    if not re.search(r"\.(jpe?g)$", name, re.IGNORECASE):
        name = re.sub(r"\.\w+$", "", name) + ".jpeg"
    api._call("POST", "Attachments", {
        "FileName": name, "FileData": base64.b64encode(shrink(data)).decode(),
        "ObjectId": opp_id, "ObjectCode": "Opportunity",
        "AttachmentTypeId": PHOTO_TYPE_ID, "ExposeToCrew": True, "AttachToInvoice": False,
    })


def queue_for_finishing(db, entry):
    st = db.load_state(FINISH_QUEUE, {"opps": []})
    st.setdefault("opps", [])
    if not any(e.get("opp_id") == entry["opp_id"] for e in st["opps"]):
        st["opps"].append(entry)
        db.save_state(FINISH_QUEUE, st)
