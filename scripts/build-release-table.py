#!/usr/bin/env python3
"""Rebuild a collected snapshot into the demo's release corpus.

Reads an already-published `tech_jobs_*` table, reapplies the corrected region, remote, role and
title rules from collect-tech-jobs.py, collapses multi-city duplicates of the same req into one
canonical job, and stamps each job with a synthetic `release_day` (DESIGN.md section 5).

The real `posted_at` is copied through untouched. `release_day` is our schedule for replaying a
static corpus, not a claim about when anything was posted; both columns ship so the schedule
stays auditable.

Python 3.10+, standard library only.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
import importlib.util
import json
from pathlib import Path
import random
import re

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("collector", Path(__file__).with_name("collect-tech-jobs.py"))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

SOURCE_COLUMNS = [
    "job_id", "source_job_id", "company", "title", "role_category", "location", "remote",
    "region_match", "workplace_type", "department", "employment_type", "source", "source_board",
    "source_url", "url", "apply_url", "description", "content_sha256", "salary_min", "salary_max",
    "salary_currency", "salary_interval", "salary_summary", "posted_at", "posted_at_kind",
    "source_created_at", "source_updated_at", "collected_at", "collected_date", "date_timezone",
]
# Hand-placed hiring waves. A uniformly random schedule makes the "company X opened 5+ matching
# roles this week" insight fire on noise; the schedule is ours, so the waves should be deliberate.
WAVES = [("Perplexity", 9, 7), ("Ramp", 17, 6), ("Replit", 24, 6)]


def fetch(env, table, catalog):
    """Page by job_id: the query API truncates a page by response bytes, not by LIMIT."""
    headers = {"Authorization": "Bearer " + env["HOTDATA_API_KEY"], "X-Workspace-Id": env["HOTDATA_WORKSPACE_ID"]}
    rows, cursor = [], ""
    while True:
        where = f"WHERE job_id > '{cursor}'" if cursor else ""
        body = {"database_id": env["HOTDATA_DATABASE_ID"],
                "sql": f"SELECT {', '.join(SOURCE_COLUMNS)} FROM {catalog}.public.{table} {where} ORDER BY job_id LIMIT 60"}
        page = c.request("https://api.hotdata.dev/v1/query", body, headers).get("rows") or []
        if not page:
            return rows
        rows.extend(dict(zip(SOURCE_COLUMNS, r)) for r in page)
        cursor = page[-1][0]


def refilter(row):
    """Reapply the corrected rules to an already-normalized row."""
    title = c.clean_title(row["title"])
    category = c.role(title)
    if not category:
        return None
    segments = [x.strip() for x in (row["location"] or "").split(";") if x.strip()]
    if not any(c.in_north_america(x) for x in segments):
        return None
    # The stored `workplace_type` falls back to a literal "remote" written by the old buggy flag,
    # so only trust it when the location did not contain the token that used to set it.
    declared = row["workplace_type"] == "Remote" or (
        row["workplace_type"] == "remote" and not c.REMOTE.search(row["location"] or ""))
    return dict(row, title=title, title_raw=row["title"], role_category=category, segments=segments,
                remote=declared or (bool(segments) and all(c.REMOTE.search(x) for x in segments)))


def merge(group):
    """Collapse one req posted once per city into a single job carrying every location."""
    best = max(group, key=lambda r: len(r["description"] or ""))
    segments = list(dict.fromkeys(s for r in group for s in r["segments"]))
    posted = sorted(r["posted_at"] for r in group if r["posted_at"])
    priced = [r for r in group if r["salary_min"] is not None] or group
    job = dict(best)
    job.update(
        location="; ".join(segments),
        remote=any(r["remote"] for r in group),
        region_match="us_canada_location" if all(c.in_north_america(s) for s in segments) else "mixed_region_posting",
        posted_at=posted[0] if posted else None,
        salary_min=min((r["salary_min"] for r in priced if r["salary_min"] is not None), default=None),
        salary_max=max((r["salary_max"] for r in priced if r["salary_max"] is not None), default=None),
        merged_count=len(group),
        merged_job_ids="; ".join(sorted(r["job_id"] for r in group)),
    )
    job.pop("segments")
    return job


def assign_release_days(jobs, days, seed):
    """Even batches by construction, then swap deliberate hiring waves in without changing them."""
    shuffled = sorted(jobs, key=lambda j: j["job_id"])
    random.Random(seed).shuffle(shuffled)
    for index, job in enumerate(shuffled):
        job["release_day"] = index % days + 1
    for company, day, size in WAVES:
        if not 1 <= day <= days:
            continue
        short = size - sum(1 for j in shuffled if j["release_day"] == day and j["company"] == company)
        movers = [j for j in shuffled if j["company"] == company and j["release_day"] != day][:max(short, 0)]
        # Swap rather than reassign, so every day keeps the count it started with.
        partners = [j for j in shuffled if j["release_day"] == day and j["company"] != company][:len(movers)]
        for mover, partner in zip(movers, partners):
            mover["release_day"], partner["release_day"] = partner["release_day"], mover["release_day"]
    return shuffled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-table", default="tech_jobs_20260911")
    parser.add_argument("--table", default="tech_jobs_release")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--last-day", default=None, help="calendar date of the final release day (default: the snapshot's collected_date)")
    parser.add_argument("--seed", type=int, default=20260911)
    parser.add_argument("--publish", action="store_true", help="write the result to a Hotdata table")
    parser.add_argument("--replace", action="store_true", help="overwrite --table if it already exists")
    parser.add_argument("--cache", default=None, help="read/write the fetched source rows here instead of refetching")
    args = parser.parse_args()

    env = c.load_env()
    catalog = env.get("HOTDATA_CATALOG", "jobs")
    cache = Path(args.cache) if args.cache else None
    if cache and cache.exists():
        rows = json.loads(cache.read_text())
    else:
        rows = fetch(env, args.source_table, catalog)
        if cache:
            cache.write_text(json.dumps(rows))

    kept = [r for r in (refilter(x) for x in rows) if r]
    groups = defaultdict(list)
    for row in kept:
        groups[(row["company"], row["title"].lower())].append(row)
    jobs = [merge(g) for g in groups.values()]

    last = date.fromisoformat(args.last_day or jobs[0]["collected_date"])
    for job in assign_release_days(jobs, args.days, args.seed):
        job["release_date"] = (last - timedelta(days=args.days - job["release_day"])).isoformat()

    fields = [k for k in jobs[0] if k not in ("release_day", "release_date")] + ["release_day", "release_date"]
    jobs = [{k: job[k] for k in fields} for job in sorted(jobs, key=lambda j: (j["release_day"], j["company"], j["title"]))]

    per_day = Counter(j["release_day"] for j in jobs)
    print(f"source rows          {len(rows)}")
    print(f"after region/role    {len(kept)}  (dropped {len(rows) - len(kept)})")
    print(f"canonical jobs       {len(jobs)}  (merged {len(kept) - len(jobs)} duplicate city reqs)")
    print(f"retitled             {sum(1 for j in jobs if j['title'] != j['title_raw'])}")
    print(f"remote               {sum(1 for j in jobs if j['remote'])}")
    print(f"release days         {args.days} ({jobs[0]['release_date']} .. {jobs[-1]['release_date']}), "
          f"{min(per_day.values())}-{max(per_day.values())} jobs/day")
    for company, day, _ in WAVES:
        print(f"  wave day {day:>2}        {sum(1 for j in jobs if j['release_day'] == day and j['company'] == company)} {company} roles")
    if not args.publish:
        print("\nDry run. Re-run with --publish to write the table.")
        return
    snapshot = ROOT / "data" / "job_snapshots" / args.table
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "release_jobs.json").write_text(json.dumps(jobs, indent=2))
    c.publish(jobs, snapshot, args.table, allow_replace=args.replace)


if __name__ == "__main__":
    main()
