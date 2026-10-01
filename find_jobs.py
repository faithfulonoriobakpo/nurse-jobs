"""Find UK nursing jobs that match the candidate profile in profile.json,
with a focus on employers that can issue a Certificate of Sponsorship (COS).

Sources:
  - NHS Jobs (jobs.nhs.uk)          always on, no key needed
  - Adzuna (adzuna.co.uk)           optional: set ADZUNA_APP_ID and ADZUNA_APP_KEY
  - Reed (reed.co.uk)               optional: set REED_API_KEY

Sponsorship is judged from:
  - the NHS Jobs advert's own "Certificate of Sponsorship" section (employer welcomes applicants needing a COS)
  - the Home Office register of licensed sponsors (employer holds a Skilled Worker licence)
  - advert wording such as "unable to offer sponsorship"

Output (in ./output):
  - dashboard/index.html                 the dashboard (self-contained, works offline or hosted)
  - jobs_latest.csv, jobs_YYYY-MM-DD.csv spreadsheet versions
  - seen.json, cache/                    memory between runs

Usage:
  python find_jobs.py                 run with profile.json
  python find_jobs.py --open          also open the dashboard in the browser
  python find_jobs.py --open-if-new   open the dashboard only when something new turned up (used by the daily task)
"""

import argparse
import base64
import bisect
import csv
import html
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
CACHE = OUT / "cache"
UA = {"User-Agent": "nursing-job-finder/2.0 (personal job search)"}

NO_SPONSOR = re.compile(
    r"(no|not|unable to|cannot|can't|don't|do not|will not|won't)\s+(be\s+)?(able\s+to\s+)?(offer|provide|consider)?\s*"
    r"(visa\s+|skilled worker\s+)?sponsor|sponsorship\s+(is\s+)?not\s+(available|offered|provided)"
    r"|require\s+sponsorship\s+will\s+not", re.I)
SPONSOR = re.compile(r"sponsorship|certificate of sponsorship|skilled worker visa|visa sponsor", re.I)
POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)


def fetch(url, headers=None, data=None, retries=2, timeout=30):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})}, data=data)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            if attempt == retries:
                print(f"  ! failed {url[:90]}: {e}", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def title_ok(title, profile):
    t = title.lower()
    return any(w in t for w in profile["title_must_match"]) and not any(w in t for w in profile["title_exclude"])


# ---------------------------------------------------------------- sources

def nhs_jobs(keyword, profile):
    jobs = []
    for page in range(1, profile["max_pages_per_search"] + 1):
        params = {"keyword": keyword, "limit": 100, "page": page}
        if profile.get("location"):
            params.update(location=profile["location"], distance=profile["radius_miles"])
        raw = fetch(f"https://www.jobs.nhs.uk/api/v1/search_xml?{urllib.parse.urlencode(params)}", timeout=60)
        if not raw:
            break
        root = ET.fromstring(raw)
        batch = [{
            "source": "NHS Jobs",
            "id": "nhs:" + (v.findtext("reference") or v.findtext("id")),
            "title": (v.findtext("title") or "").strip(" *"),
            "employer": v.findtext("employer") or "",
            "location": "; ".join(l.text or "" for l in v.findall("locations/location")),
            "salary": v.findtext("salary") or "",
            "contract": v.findtext("type") or "",
            "posted": (v.findtext("postDate") or "")[:10],
            "closes": v.findtext("closeDate") or "",
            "description": v.findtext("description") or "",
            "url": v.findtext("url") or "",
        } for v in root.findall("vacancyDetails")]
        jobs += batch
        # Results are relevance-ordered: once a page is mostly non-nursing roles, the rest will be too.
        if page >= int(root.findtext("totalPages") or 1) or sum(title_ok(j["title"], profile) for j in batch) < 5:
            break
    return jobs


def money(lo, hi):
    """'£22.06 to £23.50/hr' or '£32,000 to £38,000' from a min/max pair."""
    if not lo:
        return ""
    fmt = (lambda v: f"£{v:,.2f}") if lo < 200 else (lambda v: f"£{v:,.0f}")
    s = fmt(lo) + (f" to {fmt(hi)}" if hi and hi != lo else "")
    return s + ("/hr" if lo < 200 else "")


# Adzuna's free plan allows a few hundred calls a day, so each search stops as soon as a page
# stops producing nursing roles.
def adzuna(keyword, profile, max_pages=5):
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []
    contract_names = {"permanent": "Permanent", "contract": "Fixed-Term"}
    jobs = []
    for page in range(1, max_pages + 1):
        params = {"app_id": app_id, "app_key": app_key, "what": keyword,
                  "category": "healthcare-nursing-jobs", "sort_by": "date",
                  "max_days_old": profile["max_days_old"], "results_per_page": 50}
        if profile.get("location"):
            params.update(where=profile["location"], distance=round(profile["radius_miles"] * 1.609))
        raw = fetch(f"https://api.adzuna.com/v1/api/jobs/gb/search/{page}?{urllib.parse.urlencode(params)}",
                    {"Accept": "application/json"})
        if not raw:
            break
        results = json.loads(raw).get("results", [])
        batch = []
        for r in results:
            area = r.get("location", {}).get("area", [])
            # Adzuna estimates a salary when the advert has none; that guess isn't worth showing.
            predicted = str(r.get("salary_is_predicted")) == "1"
            batch.append({
                "source": "Adzuna",
                "id": f"adzuna:{r['id']}",
                "title": html_text(r.get("title", "")),
                "employer": r.get("company", {}).get("display_name", ""),
                "location": r.get("location", {}).get("display_name", ""),
                "salary": "" if predicted else money(r.get("salary_min"), r.get("salary_max")),
                "contract": " · ".join(filter(None, [contract_names.get(r.get("contract_type"), ""),
                                                     (r.get("contract_time") or "").replace("_", "-").capitalize()])),
                "posted": (r.get("created") or "")[:10],
                "closes": "",
                "description": html_text(r.get("description", "")),
                "url": r.get("redirect_url", ""),
                "lat": r.get("latitude"), "lon": r.get("longitude"),
                "region": area[2] if len(area) > 2 else (area[1] if len(area) > 1 else ""),
            })
        jobs += batch
        if len(results) < 50 or sum(title_ok(j["title"], profile) for j in batch) < 5:
            break
    return jobs


def reed(keyword, profile, max_pages=5):
    key = os.getenv("REED_API_KEY")
    if not key:
        return []
    auth = {"Authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode()}

    def iso(d):
        try:
            return datetime.strptime(d, "%d/%m/%Y").date().isoformat()
        except (TypeError, ValueError):
            return ""

    jobs = []
    for page in range(max_pages):
        params = {"keywords": keyword, "resultsToTake": 100, "resultsToSkip": page * 100}
        if profile.get("location"):
            params.update(locationName=profile["location"], distanceFromLocation=profile["radius_miles"])
        raw = fetch(f"https://www.reed.co.uk/api/1.0/search?{urllib.parse.urlencode(params)}", auth)
        if not raw:
            break
        results = json.loads(raw).get("results", [])
        batch = [{
            "source": "Reed",
            "id": f"reed:{r['jobId']}",
            "title": html_text(r.get("jobTitle", "")),
            "employer": r.get("employerName") or "",
            "location": r.get("locationName") or "",
            "salary": money(r.get("minimumSalary"), r.get("maximumSalary")),
            "contract": "",
            "posted": iso(r.get("date")),
            "closes": iso(r.get("expirationDate")),
            "description": html_text(r.get("jobDescription", "")),
            "url": r.get("jobUrl", ""),
        } for r in results]
        jobs += batch
        if len(results) < 100 or sum(title_ok(j["title"], profile) for j in batch) < 5:
            break
    return jobs


def html_text(s):
    """Adverts from aggregators carry HTML tags/entities in their text."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


SOURCES = [nhs_jobs, adzuna, reed]


# ---------------------------------------------------------------- matching

def score(job, profile):
    """Return (score, reasons) or None if the job doesn't fit the profile at all."""
    if not title_ok(job["title"], profile):
        return None

    text = f"{job['title']} {job['description']} {job['contract']} {job['salary']}".lower()
    points, reasons = 40, []
    for word, pts in profile["boost"].items():
        if word in text:
            points += pts
            reasons.append(word)
    for word, pts in profile["penalty"].items():
        if word in text:
            points += pts
            reasons.append(f"-{word}")

    # Senior bands: on NHS pay scales the minimum of an annual range gives the band away even when
    # the title doesn't. Private adverts are skipped: agencies and care homes often quote hourly rates
    # as annual equivalents (£22/hr ≈ £43k), which says nothing about seniority.
    lo = min_annual_salary(job["salary"]) if job["source"] == "NHS Jobs" else None
    if lo and lo > profile.get("max_annual_salary_floor", 10**9):
        return None

    today = date.today()
    if job["closes"]:
        try:
            if date.fromisoformat(job["closes"]) < today:
                return None
        except ValueError:
            pass
    # Fresh listings are more worth applying to.
    try:
        age = (today - date.fromisoformat(job["posted"])).days
        if age > profile["max_days_old"] and not job["closes"]:
            return None
        points += max(0, 7 - age)
    except ValueError:
        pass
    return max(0, min(points, 100)), reasons


def min_annual_salary(salary):
    """Lowest £ figure in the salary text if it looks annual (hourly rates return None)."""
    nums = [float(n.replace(",", "")) for n in re.findall(r"£\s?([\d,]+(?:\.\d+)?)", salary)]
    return min(nums) if nums and min(nums) >= 1000 else None


def dedupe_key(job):
    """Same role at the same employer in the same town, whichever site it came from.
    Sites format locations differently ("Gateshead, NE9 6JE" / "Gateshead, Tyne & Wear"), so only the town is used."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    town = POSTCODE.sub("", re.split(r"[,;]", job["location"])[0])
    return "|".join([norm(job["title"]), norm_org(job["employer"]).replace(" ", ""), norm(town)])


# When the same job is on several sites, keep the NHS Jobs copy: its advert gets the COS check.
SOURCE_RANK = {"NHS Jobs": 0, "Reed": 1, "Adzuna": 2}


# ---------------------------------------------------------------- sponsorship

ORG_NOISE = re.compile(r"\b(the|ltd|limited|plc|llp|uk|group|t/a|trading as|company|co|operations|cic)\b")
NHS_SUFFIX = re.compile(r"\bnhs\s*(foundation\s*trust|foundationtrust|ft|trust)\b|\bnhsft\b")


def norm_org(name):
    s = name.lower().replace("&", " and ").replace("health care", "healthcare")
    s = re.sub(r"[^a-z0-9/ ]", " ", s)
    s = NHS_SUFFIX.sub(" nhs ", s)
    return " ".join(ORG_NOISE.sub(" ", s).split())


# Names shared by many unrelated GP practices etc.: only trust these when the town matches too.
GENERIC_ORG = re.compile(r"\b(medical cent(re|er)|medical practice|practice|surgery|health cent(re|er)|partnership|medical group|family practice)\b")


class Register:
    """Sponsor names with their towns, searchable by exact name or by 2+ word prefix."""

    def __init__(self, rows):
        self.towns = {}
        for name, town in rows:
            n = norm_org(name)
            if n:
                self.towns.setdefault(n, set()).add(town.lower().strip())
        self.sorted = sorted(self.towns)

    def licensed(self, employer, location):
        e = norm_org(employer)
        words = e.split()
        if not words:
            return False
        loc = location.lower()
        town_ok = lambda names: any(t and t in loc for n in names for t in self.towns[n])
        generic = bool(GENERIC_ORG.search(e))

        if e in self.towns:
            return not generic or town_ok([e])
        if len(words) < 2:
            return False
        # Register has a longer form: "Nuffield Health" -> "nuffield health hospitals division".
        longer, i = [], bisect.bisect_left(self.sorted, e + " ")
        while i < len(self.sorted) and self.sorted[i].startswith(e + " ") and len(longer) < 20:
            longer.append(self.sorted[i])
            i += 1
        if longer and (not generic or town_ok(longer)):
            return True
        # Register has a shorter form: "St Christopher's Hospice" -> "st christopher s". Easily a
        # different organisation, so the town must match as well.
        shorter = [p for n in range(len(words) - 1, 1, -1) if (p := " ".join(words[:n])) in self.towns]
        return town_ok(shorter)


def sponsor_register():
    """Organisations licensed for the Skilled Worker route, refreshed daily."""
    path = CACHE / "sponsors.json"
    cached = load_json(path, {})
    if cached.get("date") == date.today().isoformat() and cached.get("v") == 3:
        return Register(cached["rows"])
    print("Downloading Home Office register of licensed sponsors...")
    page = fetch("https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers")
    m = page and re.search(rb'https://assets\.publishing\.service\.gov\.uk/[^"]+\.csv', page)
    raw = m and fetch(m.group(0).decode(), timeout=600)
    if not raw:
        print("  ! could not refresh the register, using the previous copy", file=sys.stderr)
        return Register(cached.get("rows", []))
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", "replace")))
    rows = sorted({(r["Organisation Name"].strip(), (r.get("Town/City") or "").strip())
                   for r in reader if "Skilled Worker" in (r.get("Route") or "")})
    print(f"  {len(rows)} licensed Skilled Worker sponsor entries")
    save_json(path, {"v": 3, "date": date.today().isoformat(), "rows": rows})
    return Register(rows)


def nhs_advert_cos(url):
    """'welcome' if the advert has NHS Jobs' Certificate of Sponsorship section, 'no' if it rules it out."""
    raw = fetch(url, timeout=30)
    if raw is None:
        return None
    page = raw.decode("utf-8", "replace")
    if 'id="tier-two-sponsorship"' in page:
        return "welcome"
    return "no" if NO_SPONSOR.search(re.sub(r"<[^>]+>", " ", page)) else "not stated"


def add_sponsorship(jobs, advert_cache):
    """Set each job's advert_cos / licensed / cos. advert_cache (job id -> advert result) is read and updated."""
    register = sponsor_register()
    todo = [j for j in jobs if j["source"] == "NHS Jobs" and j["id"] not in advert_cache]
    if todo:
        print(f"Checking {len(todo)} NHS adverts for a Certificate of Sponsorship section...")
        with ThreadPoolExecutor(6) as pool:
            for i, (job, status) in enumerate(zip(todo, pool.map(lambda j: nhs_advert_cos(j["url"]), todo)), 1):
                if status:
                    advert_cache[job["id"]] = status
                if i % 200 == 0:
                    print(f"  {i}/{len(todo)}")

    for j in jobs:
        advert = advert_cache.get(j["id"])
        if not advert:
            text = j["description"]
            advert = "no" if NO_SPONSOR.search(text) else "welcome" if SPONSOR.search(text) else "not stated"
        j["advert_cos"] = advert
        j["licensed"] = register.licensed(j["employer"], j["location"])
        # welcome > licensed > unknown; an explicit "no" always wins.
        j["cos"] = advert if advert in ("welcome", "no") else "licensed" if j["licensed"] else "unknown"


# ---------------------------------------------------------------- location

def haversine_miles(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def add_location(jobs, profile, cache):
    """Set region and miles on each job. cache (postcode or "town:x" -> [lat, lon, region] | None) is
    read and updated; returns the keys that were added."""
    added = set()
    home = profile.get("home_postcode", "").upper()
    for j in jobs:
        m = POSTCODE.search(j["location"])
        j["postcode"] = f"{m.group(1)} {m.group(2)}".upper() if m else ""
    todo = sorted({p for p in [home] + [j["postcode"] for j in jobs] if p and p not in cache})
    for i in range(0, len(todo), 100):
        body = json.dumps({"postcodes": todo[i:i + 100]}).encode()
        raw = fetch("https://api.postcodes.io/postcodes?filter=postcode,region,country,latitude,longitude",
                    {"Content-Type": "application/json"}, data=body)
        for item in (json.loads(raw)["result"] if raw else []):
            r = item["result"]
            cache[item["query"]] = [r["latitude"], r["longitude"], r["region"] or r["country"]] if r else None
            added.add(item["query"])
    # Aggregator adverts usually give a town instead of a postcode ("Gateshead, Tyne & Wear").
    town_of = lambda j: "town:" + re.split(r"[,;]", j["location"])[0].strip().lower()
    for key in sorted({town_of(j) for j in jobs if not j["postcode"] and j["location"]} - set(cache)):
        raw = fetch(f"https://api.postcodes.io/places?{urllib.parse.urlencode({'q': key[5:], 'limit': 1})}")
        if raw is not None:
            hits = json.loads(raw)["result"]
            cache[key] = [hits[0]["latitude"], hits[0]["longitude"], hits[0]["region"] or hits[0]["country"]] if hits else None
            added.add(key)

    home_ll = cache.get(home)
    for j in jobs:
        hit = cache.get(j.pop("postcode")) or cache.get(town_of(j))
        lat, lon = (j.get("lat"), j.get("lon")) if j.get("lat") else (hit[0], hit[1]) if hit else (None, None)
        j["region"] = hit[2] if hit else ""
        j["miles"] = round(haversine_miles(home_ll[:2], (lat, lon))) if home_ll and lat and lon else None
    return added


# ---------------------------------------------------------------- storage

# Columns stored per job (also what the dashboard receives, plus "active").
JOB_COLUMNS = ["id", "source", "title", "employer", "location", "region", "miles", "salary", "contract", "posted",
               "closes", "cos", "advert_cos", "licensed", "score", "matched", "url", "snippet", "first_seen", "last_seen"]


def is_active(job, today):
    """Still worth showing. NHS Jobs is searched in full every day, so an NHS job that wasn't seen has been
    withdrawn (a day's grace for a missed page). Adzuna/Reed searches are capped, so their jobs are kept
    for a week after last being seen, unless the closing date has passed."""
    if job.get("closes") and job["closes"] < today.isoformat():
        return False
    grace = 1 if job["source"] == "NHS Jobs" else 7
    return job["last_seen"] >= (today - timedelta(days=grace)).isoformat()


class FileStore:
    """State in output/*.json. Used for local runs without Supabase credentials."""
    name = "local files"

    def known(self):
        seen, cos = load_json(OUT / "seen.json", {}), load_json(CACHE / "cos.json", {})
        first = lambda v: v["first"] if isinstance(v, dict) else v
        return {k: {"first_seen": first(seen[k]) if k in seen else None, "advert_cos": cos.get(k)}
                for k in set(seen) | set(cos)}

    def geo(self):
        return load_json(CACHE / "postcodes.json", {})

    def save_geo(self, cache, added):
        if added:
            save_json(CACHE / "postcodes.json", cache)

    def save(self, jobs, today):
        seen, cos = load_json(OUT / "seen.json", {}), load_json(CACHE / "cos.json", {})
        for j in jobs:
            seen[j["id"]] = {"first": j["first_seen"], "last": j["last_seen"]}
            if j["source"] == "NHS Jobs":
                cos[j["id"]] = j["advert_cos"]
        cutoff = (today - timedelta(days=60)).isoformat()
        seen = {k: v for k, v in seen.items() if (v["last"] if isinstance(v, dict) else v) >= cutoff}
        save_json(OUT / "seen.json", seen)
        save_json(CACHE / "cos.json", {k: v for k, v in cos.items() if k in seen})
        return [{**j, "active": True} for j in jobs]


class SupabaseStore:
    """State in Supabase (see supabase/schema.sql), written with the service-role / secret key."""
    name = "Supabase"

    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1/"
        self.headers = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):  # legacy JWT keys also go in Authorization; new sb_secret_ keys must not
            self.headers["Authorization"] = f"Bearer {key}"

    def _call(self, method, path, body=None, prefer=None):
        headers = {**UA, **self.headers, **({"Prefer": prefer} if prefer else {})}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                if e.code < 500 or attempt == 2:
                    raise RuntimeError(f"Supabase {method} {path.split('?')[0]} failed: {e.code} {detail}") from None
            except urllib.error.URLError:
                if attempt == 2:
                    raise
            time.sleep(3 * (attempt + 1))

    def _select_all(self, table, query):
        rows, page = [], 1000
        while True:
            batch = self._call("GET", f"{table}?{query}&limit={page}&offset={len(rows)}")
            rows += batch
            if len(batch) < page:
                return rows

    def _upsert(self, table, rows):
        for i in range(0, len(rows), 500):
            self._call("POST", table, rows[i:i + 500], prefer="resolution=merge-duplicates,return=minimal")

    def _in(self, ids):
        return urllib.parse.quote(",".join(f'"{x}"' for x in ids))

    def known(self):
        rows = self._select_all("jobs", "select=id,first_seen,advert_cos&order=id")
        if not rows:
            print("  Supabase is empty; seeding from the local state files")
            return FileStore().known()
        return {r["id"]: r for r in rows}

    def geo(self):
        return {r["key"]: ([r["lat"], r["lon"], r["region"]] if r["lat"] is not None else None)
                for r in self._select_all("geo_cache", "select=key,lat,lon,region&order=key")}

    def save_geo(self, cache, added):
        self._upsert("geo_cache", [{"key": k, "lat": v[0] if v else None, "lon": v[1] if v else None,
                                    "region": v[2] if v else None} for k in sorted(added) for v in [cache[k]]])

    def save(self, jobs, today):
        self._upsert("jobs", [{c: j.get(c) for c in JOB_COLUMNS} for j in jobs])

        # Dashboard: everything still active, plus anything she has tracked (even once it has closed).
        tracked = {r["job_id"] for r in self._select_all("job_status", "select=job_id&order=job_id")}
        since = (today - timedelta(days=8)).isoformat()
        cols = ",".join(JOB_COLUMNS)
        rows = {r["id"]: r for r in self._select_all("jobs", f"select={cols}&last_seen=gte.{since}&order=id")}
        missing = sorted(tracked - set(rows))
        for i in range(0, len(missing), 100):
            rows |= {r["id"]: r for r in self._call("GET", f"jobs?select={cols}&id=in.({self._in(missing[i:i + 100])})")}

        # Drop long-gone, untracked jobs so the table stays small.
        cutoff = (today - timedelta(days=120)).isoformat()
        old = [r["id"] for r in self._select_all("jobs", f"select=id&last_seen=lt.{cutoff}&order=id") if r["id"] not in tracked]
        for i in range(0, len(old), 100):
            self._call("DELETE", f"jobs?id=in.({self._in(old[i:i + 100])})")

        return [{**r, "active": is_active(r, today), "tracked": r["id"] in tracked} for r in rows.values()
                if r["id"] in tracked or is_active(r, today)]


def open_store():
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    return SupabaseStore(url, key) if url and key else FileStore()


def load_env_file(path):
    """KEY=VALUE lines from a local, git-ignored file (for local runs; the Action uses repo secrets)."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


# ---------------------------------------------------------------- output

CSV_FIELDS = ["cos", "score", "title", "employer", "location", "region", "miles", "salary", "contract",
              "posted", "closes", "licensed", "first_seen", "active", "matched", "source", "url"]


def write_csv(jobs, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(jobs)


def write_dashboard(jobs, profile, path):
    keep = ["id", "title", "employer", "location", "region", "miles", "salary", "contract", "posted", "closes",
            "cos", "licensed", "score", "matched", "source", "url", "first_seen", "snippet", "active"]
    data = {
        "generated": datetime.now().isoformat(timespec="minutes"),
        "home": profile.get("home_label") or profile.get("home_postcode", ""),
        "jobs": [{k: j.get(k) for k in keep} for j in jobs],
    }
    # The public (anon / publishable) key only lets signed-in users touch job_status; see supabase/schema.sql.
    auth = {"url": os.getenv("SUPABASE_URL"), "key": os.getenv("SUPABASE_ANON_KEY")}
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = (ROOT / "dashboard_template.html").read_text(encoding="utf-8")
    page = page.replace("/*__DATA__*/null", dump(data))
    page = page.replace("/*__SUPABASE__*/null", dump(auth) if all(auth.values()) else "null")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default=ROOT / "profile.json", type=Path)
    ap.add_argument("--open", action="store_true", help="open the dashboard when done")
    ap.add_argument("--open-if-new", action="store_true", help="open the dashboard only if there are new jobs")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_env_file(ROOT / ".env.local")

    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    OUT.mkdir(exist_ok=True)
    store = open_store()
    today = date.today()

    active = [s.__name__ for s in SOURCES if s is nhs_jobs
              or (s is adzuna and os.getenv("ADZUNA_APP_ID"))
              or (s is reed and os.getenv("REED_API_KEY"))]
    where = f"within {profile['radius_miles']} miles of {profile['location']}" if profile.get("location") else "across the UK"
    print(f"Searching {', '.join(active)} for {len(profile['searches'])} terms {where} (state: {store.name})...")
    known = store.known()

    found = {}
    for kw in profile["searches"]:
        before = len(found)
        for source in SOURCES:
            for job in source(kw, profile):
                found.setdefault(job["id"], job)
        print(f"  '{kw}': +{len(found) - before}")
    print(f"  {len(found)} raw listings")

    by_key = {}
    for job in found.values():
        s = score(job, profile)
        if not s:
            continue
        job["score"], reasons = s
        job["matched"] = ", ".join(reasons)
        k = dedupe_key(job)
        pick = lambda j: (SOURCE_RANK[j["source"]], -j["score"])
        if k in by_key and pick(by_key[k]) <= pick(job):
            continue
        by_key[k] = job
    matches = list(by_key.values())
    per_source = {s: sum(j["source"] == s for j in matches) for s in SOURCE_RANK}
    print(f"  {len(matches)} match the profile ({', '.join(f'{s} {n}' for s, n in per_source.items() if n)})")

    add_sponsorship(matches, {k: v["advert_cos"] for k, v in known.items() if v.get("advert_cos")})
    geo = store.geo()
    store.save_geo(geo, add_location(matches, profile, geo))

    for j in matches:
        j["snippet"] = j["description"][:220]
        j["first_seen"] = (known.get(j["id"]) or {}).get("first_seen") or today.isoformat()
        j["last_seen"] = today.isoformat()
    new = [j for j in matches if j["id"] not in known and j["cos"] != "no"]

    # Jobs that rule sponsorship out are stored too (so their adverts aren't re-checked) but not shown.
    # Stored jobs are re-checked against the current profile, so a newly excluded title disappears today
    # rather than after its grace period. Jobs she is tracking always stay.
    shown = [j for j in store.save(matches, today)
             if j.get("tracked") or (title_ok(j["title"], profile) and not (profile.get("needs_cos") and j["cos"] == "no"))]
    rank = {"welcome": 0, "licensed": 1, "unknown": 2, "no": 3}
    shown.sort(key=lambda j: (not j["active"], rank[j["cos"]], -j["score"], j["closes"] or "9999"))

    write_csv(shown, OUT / "jobs_latest.csv")
    dashboard = OUT / "dashboard" / "index.html"
    write_dashboard(shown, profile, dashboard)

    live = [j for j in shown if j["active"]]
    counts = {c: sum(j["cos"] == c for j in live) for c in ("welcome", "licensed", "unknown")}
    print(f"  {len(live)} open jobs: {counts['welcome']} welcome COS applicants, "
          f"{counts['licensed']} at licensed sponsors, {counts['unknown']} unknown; {len(new)} new today")
    for j in [j for j in new if j["cos"] != "unknown"][:10]:
        print(f"  [{j['cos']:>8}] {j['title']} - {j['employer']} ({j['location']}) {j['salary']}")
    print(f"Dashboard: {dashboard}")
    if args.open or (args.open_if_new and new):
        webbrowser.open(dashboard.as_uri())


if __name__ == "__main__":
    main()
