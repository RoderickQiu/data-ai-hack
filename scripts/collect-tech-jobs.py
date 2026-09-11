#!/usr/bin/env python3
"""Collect public tech openings; retain raw snapshots and publish a new Hotdata table.

Python 3.10+, standard library only. See docs/hotdata-jobs.md.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PACIFIC = ZoneInfo("America/Los_Angeles")
SOURCES = [
    ("greenhouse", slug, company) for slug, company in [
        ("stripe", "Stripe"), ("cloudflare", "Cloudflare"),
        ("databricks", "Databricks"), ("figma", "Figma"),
        ("mongodb", "MongoDB"), ("anthropic", "Anthropic"),
        ("discord", "Discord"), ("reddit", "Reddit"),
        ("robinhood", "Robinhood"), ("duolingo", "Duolingo")]
] + [
    ("ashby", slug, company) for slug, company in [
        ("openai", "OpenAI"), ("notion", "Notion"), ("ramp", "Ramp"),
        ("Perplexity", "Perplexity"), ("linear", "Linear"),
        ("replit", "Replit"), ("vanta", "Vanta")]
] + [("lever", "spotify", "Spotify"), ("lever", "palantir", "Palantir"),
     ("lever", "wealthsimple", "Wealthsimple")]


def request(url, body=None, headers=None):
    """Retry only public reads; writes use persisted idempotency keys."""
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
        headers={"User-Agent": "SecondNatureJobCollector/0.1", "Accept": "application/json",
                 "Content-Type": "application/json", **(headers or {})})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if body is None and exc.code in (429, 502, 503, 504) and attempt < 2:
                time.sleep(min(int(exc.headers.get("Retry-After", "2")), 20))
                continue
            raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode()[:500]}") from None


def fetch_board(source, snapshot):
    ats, slug, company = source
    url = {"greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
           "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
           "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1000"}[ats]
    record = dict(ats=ats, slug=slug, company=company, url=url)
    try:
        payload = request(url)
        if ats == "lever":
            page = payload
            while len(page) == 1000:
                page = request(url + f"&skip={len(payload)}")
                payload.extend(page)
        jobs = payload if isinstance(payload, list) else payload["jobs"]
        (snapshot / f"{ats}_{slug}.json").write_text(json.dumps(payload, ensure_ascii=False))
        record.update(status="ok", count=len(jobs), fetched_at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        record.update(status="error", error=str(exc))
    return record


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def text(value):
    parser = PlainText()
    parser.feed(html.unescape(value or ""))
    return re.sub(r"\n\s*\n+", "\n\n", "".join(parser.parts)).strip()


def parse_date(value):
    if not value:
        return None
    try:
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value / 1000, timezone.utc)
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


ROLE_RULES = [
    ("hardware_robotics", r"mechanical|electrical|robotic|silicon|\bfpga\b|\basic\b|physical design|hardware|firmware"),
    ("infrastructure", r"data cent(er|re)|compute capacity"),
    ("product_management", r"product (manager|management|lead)|head of product|director.{0,15}product"),
    ("design_research", r"designer|\bux\b|\bui\b|user research|design (engineer|technologist)"),
    ("technical_program", r"technical (program|project|product)|engineering program|\btpm\b"),
    ("ai_ml_research", r"machine learning|research (scientist|engineer)|applied scientist|"
        r"\A(?=.*\b(engineer|scientist|research|developer|architect|infrastructure|platform|"
        r"systems|learning|fellows?|alignment|safety|evals?|robotics|model|training|inference|"
        r"analyst|intern)\b).*\b(ai|ml)\b"),
    ("data_analytics", r"\bdata\b|analytics|business intelligence|decision scientist|quantitative"),
    ("security", r"security|cyber|threat|detection engineer"),
    ("infrastructure", r"infrastructure|\bsre\b|site reliability|devops|cloud|network|systems engineer"),
    ("solutions_support", r"solutions? (engineer|architect)|sales engineer|support engineer|technical (support|account)|developer (advocate|relations)"),
    ("software_engineering", r"engineer|developer|software|architect"),
    ("technical_writing", r"technical writer|documentation engineer"),
    # Last: an AI-lab "Member of Technical Staff" with no other signal is research; one that
    # names a discipline ("... (Software Engineer, Backend)") is matched by that rule above.
    ("ai_ml_research", r"member of technical staff"),
]


def role(title):
    if re.search(r"recruit|talent acquisition|account executive|marketing|legal|counsel|human resources|facilit(y|ies)|real estate|physical security|accounting|people partners?|sourcing|procurement operations|supply chain", title, re.I):
        return None
    if re.search(r"finance|financial|compensation|payroll", title, re.I) and not re.search(r"engineer|developer|architect|scientist|analyst|analytics|data science|technical program|product manager", title, re.I):
        return None
    for category, pattern in ROLE_RULES:
        if re.search(pattern, title, re.I):
            return category
    return None


REMOTE = re.compile(r"\bremote\b", re.I)

# "U.S." never matched the old \b(...)\b group (a trailing "." is not a word boundary), and state
# abbreviations were absent, so real US rows such as "Remote U.S." and "Honolulu, HI" fell out of
# region. Abbreviations are matched case-sensitively: lowercased they collide with English words
# ("or", "in", "me", "la", "hi").
NORTH_AMERICA = re.compile(
    r"\bu\.s\.?a?\.?|\b(united states|usa|us|canada|canadian|north america|americas|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
    r"hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|"
    r"michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    r"new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    r"rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|"
    r"west virginia|wisconsin|wyoming|district of columbia|puerto rico|"
    r"ontario|quebec|québec|british columbia|alberta|manitoba|saskatchewan|nova scotia|"
    r"new brunswick|newfoundland|prince edward island|"
    r"san francisco|bay area|nyc|seattle|bellevue|redmond|boston|cambridge|"
    r"austin|dallas|houston|denver|boulder|chicago|atlanta|los angeles|san diego|"
    r"san jose|sunnyvale|santa clara|palo alto|menlo park|mountain view|redwood city|"
    r"arlington|reston|mclean|pittsburgh|philadelphia|raleigh|durham|honolulu|"
    r"miami|orlando|tampa|phoenix|portland|salt lake|san mateo|foster city|irvine|"
    r"oakland|toronto|vancouver|montreal|montréal|ottawa|waterloo|calgary)\b", re.I)
STATE_CODE = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|"
    r"NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC|PR|"
    r"ON|QC|BC|AB|MB|SK|NS|NB|NL|PE)\b")


def in_north_america(segment):
    return bool(NORTH_AMERICA.search(segment) or STATE_CODE.search(segment))


# Hiring seasons and start years are scheduling text, not part of what the role is. The demo
# releases this corpus on its own day clock (see DESIGN.md §5), so a title that announces
# "Winter 2027" contradicts the schedule on screen. `title_raw` keeps the board's wording.
TITLE_TIME = [
    re.compile(r"\s*[\(\[][^()\[\]]*\b(20\d\d|spring|summer|fall|autumn|winter|q[1-4])\b[^()\[\]]*[\)\]]", re.I),
    re.compile(r"\s*[-–—,:]\s*\b(spring|summer|fall|autumn|winter)\b\s*(20\d\d)?\s*$", re.I),
    re.compile(r"\s*\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+20\d\d\b", re.I),
    re.compile(r"\s*\b20\d\d\b"),
]


def clean_title(title):
    for pattern in TITLE_TIME:
        title = pattern.sub("", title)
    title = re.sub(r"\(\s*\)|\[\s*\]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return re.sub(r"^[\s,:;\-–—]+|[\s,:;\-–—]+$", "", title)


def normalize(job, source, fetched_at):
    ats, slug = source["ats"], source["slug"]
    title_raw = job.get("title") or job.get("text", "")
    title = clean_title(title_raw)
    category = role(title)
    if not category or job.get("isListed") is False:
        return None
    deadline = parse_date(job.get("application_deadline"))
    if deadline and deadline < parse_date(fetched_at):
        return None
    categories = job.get("categories") or {}
    locations = []
    loc = job.get("location")
    if loc:
        locations.append(loc.get("name", "") if isinstance(loc, dict) else loc)
    if ats == "greenhouse":
        locations.extend(x.get("name", "") for x in job.get("offices", []))
    locations.extend(x.get("location", "") for x in job.get("secondaryLocations", []))
    locations.extend(categories.get("allLocations") or [categories.get("location", "")])
    location = "; ".join(dict.fromkeys(x for x in locations if x))
    segments = [x.strip() for x in location.split(";") if x.strip()]
    # A "; "-joined multi-city string containing "Remote" anywhere does not make the job remote.
    remote = (job.get("isRemote") is True or (job.get("workplaceType") or "").lower() == "remote"
              or (bool(segments) and all(REMOTE.search(x) for x in segments)))
    countries = [job.get("country", ""), ((job.get("address") or {}).get("postalAddress") or {}).get("addressCountry", "")]
    countries.extend((x.get("address") or {}).get("addressCountry", "") for x in job.get("secondaryLocations", []))
    in_region = (any(str(c).upper() in ("US", "USA", "UNITED STATES", "CA", "CAN", "CANADA") for c in countries)
                 or any(in_north_america(x) for x in segments))
    if not in_region:
        return None
    # Every surviving row has a North American location, so the old flag was constant. What is
    # still worth carrying is whether the same req also lists offices outside the region.
    region_match = "us_canada_location" if all(in_north_america(x) for x in segments) else "mixed_region_posting"
    description = job.get("descriptionPlain") or text(job.get("content") or job.get("descriptionHtml") or job.get("description"))
    if ats == "lever":
        description = "\n\n".join(filter(None, [job.get("openingPlain"), description,
            *[x.get("text", "") + "\n" + text(x.get("content")) for x in job.get("lists", [])], job.get("additionalPlain")]))
    published = parse_date(job.get("first_published") if ats == "greenhouse" else job.get("publishedAt") if ats == "ashby" else None)
    kind = "first_published" if ats == "greenhouse" and published else "last_published" if ats == "ashby" and published else "unavailable"
    created = parse_date(job.get("createdAt")) if ats == "lever" else None
    fetched = parse_date(fetched_at)
    published_today = published.astimezone(PACIFIC).date() == fetched.astimezone(PACIFIC).date() if published else None
    comp = job.get("compensation") or {}
    salary = next((x for x in comp.get("summaryComponents", []) if x.get("compensationType") == "Salary"), {})
    lever_salary = job.get("salaryRange") or {}
    source_id = str(job.get("id") or job.get("jobUrl", "").rstrip("/").split("/")[-1])
    url = job.get("absolute_url") or job.get("jobUrl") or job.get("hostedUrl")
    if not source_id or not url or not description or not url.startswith("https://"):
        return None
    return dict(
        job_id=f"{ats}:{slug}:{source_id}", source_job_id=source_id,
        company=source["company"], title=title, title_raw=title_raw, role_category=category,
        location=location, remote=remote, region_match=region_match,
        workplace_type=job.get("workplaceType") or ("remote" if remote else "unspecified"),
        department=job.get("department") or categories.get("department") or "; ".join(x["name"] for x in job.get("departments", [])),
        employment_type=job.get("employmentType") or categories.get("commitment"),
        source=ats, source_board=slug, source_url=source["url"], url=url,
        apply_url=job.get("applyUrl") or url, description=description,
        salary_min=salary.get("minValue", lever_salary.get("min")),
        salary_max=salary.get("maxValue", lever_salary.get("max")),
        salary_currency=salary.get("currencyCode") or lever_salary.get("currency"),
        salary_interval=salary.get("interval") or lever_salary.get("interval"),
        salary_summary=comp.get("compensationTierSummary"),
        posted_at=published.isoformat() if published else None, posted_at_kind=kind,
        source_created_at=created.isoformat() if created else None,
        source_updated_at=job.get("updated_at"),
        published_today=published_today,
        first_published_today=published_today if kind == "first_published" else None,
        collected_at=fetched_at, collected_date=fetched.astimezone(PACIFIC).date().isoformat(),
        date_timezone="America/Los_Angeles", status="open_at_collection",
        content_sha256=hashlib.sha256(description.encode()).hexdigest())


BOOLEAN_FIELDS = ("remote", "published_today", "first_published_today")
NUMERIC_FIELDS = ("salary_min", "salary_max", "release_day", "merged_count")


def load_env():
    values = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    values.update(os.environ)
    return values


def publish(rows, snapshot, table):
    if not re.fullmatch(r"tech_jobs_[a-z0-9_]+", table):
        raise ValueError("Destination must be a new tech_jobs_* table")
    env = load_env()
    headers = {"Authorization": "Bearer " + env["HOTDATA_API_KEY"], "X-Workspace-Id": env["HOTDATA_WORKSPACE_ID"]}
    database = env["HOTDATA_DATABASE_ID"]
    def api(path, body=None):
        return request("https://api.hotdata.dev/v1" + path, body, headers)
    def query(sql):
        return api("/query", {"database_id": database, "sql": sql})
    detail = api("/databases/" + database)
    catalog = env.get("HOTDATA_CATALOG", "jobs")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", catalog):
        raise ValueError("Invalid catalog")
    if detail.get("default_catalog") != catalog:
        raise ValueError("HOTDATA_CATALOG must match the database's default catalog")
    existing = query(f"SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = '{table}'")
    if existing.get("rows"):
        raise RuntimeError("Table already exists; choose a fresh --table to preserve existing data")
    fields = list(rows[0])
    columns = {key: "BOOLEAN" if key in BOOLEAN_FIELDS else "DOUBLE" if key in NUMERIC_FIELDS else "VARCHAR" for key in fields}
    def csv_line(values):
        out = io.StringIO(newline="")
        csv.writer(out).writerow(values)
        return out.getvalue()
    header = csv_line(fields)
    batches, lines, size = [], [header], len(header.encode())
    for row in rows:
        line = csv_line([str(row[k]).lower() if isinstance(row[k], bool) else row[k] for k in fields])
        if len(line.encode()) > 1_400_000:
            raise ValueError("One job exceeds inline load limit")
        if size + len(line.encode()) > 1_500_000:
            batches.append("".join(lines)); lines, size = [header], len(header.encode())
        lines.append(line); size += len(line.encode())
    if len(lines) > 1:
        batches.append("".join(lines))
    receipts = []
    for i, data in enumerate(batches):
        body = dict(mode="replace" if i == 0 else "append", data=data, columns=columns,
                    idempotency_key=hashlib.sha256((table + str(i) + data).encode()).hexdigest())
        result = api(f"/databases/{database}/schemas/public/tables/{table}/loads", body)
        receipts.append(result)
        (snapshot / "load_receipts.json").write_text(json.dumps(receipts, indent=2))
        print(f"Loaded batch {i+1}/{len(batches)}", flush=True)
    verification = query(f"SELECT count(*) AS total, count(DISTINCT job_id) AS unique_jobs, count(DISTINCT company) AS companies FROM {catalog}.public.{table}")
    if verification.get("rows", [[]])[0][:2] != [len(rows), len(rows)]:
        raise RuntimeError(f"Readback count mismatch: {verification}")
    result = dict(table=f"{catalog}.public.{table}", database_id=database, verification=verification)
    (snapshot / "hotdata_result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, help="Reprocess existing raw snapshot instead of fetching")
    parser.add_argument("--publish", action="store_true", help="Write a NEW Hotdata table and verify it")
    parser.add_argument("--table", help="New table name, e.g. tech_jobs_20260911")
    args = parser.parse_args()
    snapshot = args.snapshot or ROOT / "data/job_snapshots" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot.mkdir(parents=True, exist_ok=True)
    if not args.snapshot:
        with ThreadPoolExecutor(max_workers=4) as pool:
            sources = list(pool.map(lambda s: fetch_board(s, snapshot), SOURCES))
        (snapshot / "sources.json").write_text(json.dumps(sources, indent=2))
    else:
        sources = json.loads((snapshot / "sources.json").read_text())
    records = {}
    for source in sources:
        if source["status"] != "ok":
            continue
        path = snapshot / f"{source['ats']}_{source['slug']}.json"
        fetched_at = source.get("fetched_at") or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        payload = json.loads(path.read_text())
        for job in payload if isinstance(payload, list) else payload["jobs"]:
            row = normalize(job, source, fetched_at)
            if row:
                records[row["job_id"]] = row
    rows = sorted(records.values(), key=lambda r: (r["posted_at"] or "", r["job_id"]), reverse=True)
    if not rows:
        raise RuntimeError("No matching jobs; nothing published. Inspect sources.json")
    (snapshot / "jobs.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    summary = dict(total=len(rows), source_total=sum(s.get("count", 0) for s in sources),
        by_company=dict(Counter(r["company"] for r in rows)), by_role=dict(Counter(r["role_category"] for r in rows)),
        by_source=dict(Counter(r["source"] for r in rows)),
        published_today=sum(r["published_today"] is True for r in rows),
        first_published_today=sum(r["first_published_today"] is True for r in rows),
        publication_date_unknown=sum(r["posted_at"] is None for r in rows),
        remote=sum(r["remote"] for r in rows), failed_sources=[s for s in sources if s["status"] != "ok"],
        zero_result_sources=[s["company"] for s in sources if s.get("count") == 0], snapshot=str(snapshot))
    (snapshot / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if args.publish:
        publish(rows, snapshot, args.table or "tech_jobs_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))


if __name__ == "__main__":
    main()
