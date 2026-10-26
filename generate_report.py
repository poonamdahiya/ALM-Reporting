#!/usr/bin/env python3
"""
generate_report.py - pull test executions from OpenText / Micro Focus / HP ALM
(Quality Center) through its REST API and write an interactive HTML report.

Reads the Test Lab (test set folders, test sets, test instances, runs) and the
Test Plan (tests, test folders), optionally linked defects, and embeds the
result in alm-execution-report.html. All ALM calls are read-only GETs.

    python generate_report.py --alm-timezone Europe/London --with-defects

Settings come from environment variables or a .env file next to this script
(see .env.example). Run with --help for all options.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency: run  pip install -r requirements.txt")

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    sys.exit("Python 3.9 or later is required.")

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "alm-execution-report.html"
MARKER = "/*__ALM_DATA__*/null"
log = logging.getLogger("alm-report")

ID_CHUNK = 100          # ids per query, keeps URLs well under server limits
DEFAULT_PAGE_SIZE = 1000

# ALM run status -> report status. Anything else is passed through and the
# report flags it as "Unrecognized" (you can add it in the report's mapping).
STATUS_MAP = {
    "passed": "Passed",
    "failed": "Failed",
    "blocked": "Blocked",
    "not completed": "In progress",
    "no run": "Not run",
    "n/a": "Not run",
}

AUTOMATED_TYPES = {
    "QUICKTEST_TEST", "SERVICE-TEST", "LEANFT-TEST", "VAPI-XP-TEST", "LR-SCENARIO",
    "SYSTEM-TEST", "WR-AUTOMATED", "ALT-SCENARIO", "QAINSPECT_TEST", "EXTERNAL-TEST",
}


class AlmError(Exception):
    pass


# --------------------------------------------------------------------------- #
# REST client
# --------------------------------------------------------------------------- #
class AlmClient:
    def __init__(self, url: str, domain: str, project: str, *, username: str | None = None,
                 password: str | None = None, client_id: str | None = None,
                 client_secret: str | None = None, verify: bool | str = True,
                 timeout: int = 120, page_size: int = DEFAULT_PAGE_SIZE, retries: int = 3):
        url = url.rstrip("/")
        if not re.search(r"/qcbin$", url, re.I):
            url += "/qcbin"
        self.base = url
        self.proj = f"{url}/rest/domains/{domain}/projects/{project}"
        self.username, self.password = username, password
        self.client_id, self.client_secret = client_id, client_secret
        self.timeout, self.page_size, self.retries = timeout, page_size, retries
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers.update({"Accept": "application/json", "User-Agent": "alm-execution-report/1.0"})
        self._auth_mode = None

    # -- authentication ----------------------------------------------------- #
    def sign_in(self) -> None:
        self.s.cookies.clear()
        if self.client_id and self.client_secret:
            r = self.s.post(f"{self.base}/rest/oauth2/login", timeout=self.timeout,
                            json={"clientId": self.client_id, "secret": self.client_secret})
            self._check_auth(r, "API key")
            self._auth_mode = "oauth"
        elif self.username:
            r = self.s.post(f"{self.base}/api/authentication/sign-in", timeout=self.timeout,
                            auth=(self.username, self.password or ""))
            if r.status_code in (404, 405):   # ALM 12.2x and older
                r = self.s.post(f"{self.base}/authentication-point/authenticate",
                                timeout=self.timeout, auth=(self.username, self.password or ""))
                self._check_auth(r, "username/password")
                r2 = self.s.post(f"{self.base}/rest/site-session", timeout=self.timeout)
                if r2.status_code not in (200, 201):
                    raise AlmError(f"Opening a site session failed: HTTP {r2.status_code} {_snip(r2)}")
                self._auth_mode = "legacy"
            else:
                self._check_auth(r, "username/password")
                self._auth_mode = "api"
        else:
            raise AlmError("No ALM login configured. Set ALM_USERNAME/ALM_PASSWORD or "
                           "ALM_CLIENT_ID/ALM_CLIENT_SECRET.")
        log.info("Signed in to %s", self.base)

    @staticmethod
    def _check_auth(r, what):
        if r.status_code in (200, 201):
            return
        if r.status_code == 401:
            raise AlmError(f"ALM rejected the {what} login (HTTP 401). Check the credentials.")
        raise AlmError(f"ALM sign-in failed: HTTP {r.status_code} {_snip(r)}")

    def sign_out(self) -> None:
        try:
            if self._auth_mode == "oauth":
                self.s.post(f"{self.base}/rest/oauth2/logout", timeout=30)
            elif self._auth_mode == "api":
                self.s.get(f"{self.base}/api/authentication/sign-out", timeout=30)
            elif self._auth_mode == "legacy":
                self.s.delete(f"{self.base}/rest/site-session", timeout=30)
                self.s.get(f"{self.base}/authentication-point/logout", timeout=30)
        except requests.RequestException:
            pass
        self._auth_mode = None

    # -- requests ----------------------------------------------------------- #
    def get(self, url: str, params: dict | None = None) -> dict:
        reauthed = False
        last_exc = None
        for attempt in range(self.retries + 1):
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last_exc = e
                log.warning("Request failed (%s), retrying…", e)
                time.sleep(min(30, 2 ** attempt))
                continue
            if r.status_code == 401 and not reauthed:
                log.info("Session expired, signing in again")
                self.sign_in()
                reauthed = True
                continue
            if r.status_code >= 500 or r.status_code == 429:
                last_exc = AlmError(f"HTTP {r.status_code} {_snip(r)}")
                log.warning("ALM returned HTTP %s, retrying…", r.status_code)
                time.sleep(min(30, 2 ** attempt))
                continue
            if r.status_code != 200:
                raise AlmError(f"GET {url} failed: HTTP {r.status_code} {_snip(r)}")
            try:
                return r.json()
            except ValueError:
                raise AlmError(f"GET {url} did not return JSON. Is ALM_URL correct? {_snip(r)}")
        raise AlmError(f"GET {url} failed after {self.retries + 1} attempts: {last_exc}")

    def list_entities(self, collection: str, query: str | None = None,
                      fields: Iterable[str] | None = None) -> list[dict]:
        """Read every entity of a collection, paging until TotalResults is reached."""
        out: list[dict] = []
        start, total = 1, None
        while True:
            params = {"page-size": self.page_size, "start-index": start, "order-by": "{id[ASC]}"}
            if query:
                params["query"] = query
            if fields:
                params["fields"] = ",".join(dict.fromkeys(fields))
            data = self.get(f"{self.proj}/{collection}", params)
            ents = data.get("entities") or []
            total = int(data.get("TotalResults", len(ents)))
            out.extend(_flatten(e) for e in ents)
            if not ents or len(out) >= total:
                break
            start += len(ents)
        if len(out) < total:
            raise AlmError(f"{collection}: ALM reported {total} results but only {len(out)} "
                           "were returned. Stopping so the report isn't incomplete.")
        return out

    def list_by_ids(self, collection: str, field: str, ids: Iterable, extra: str = "",
                    fields: Iterable[str] | None = None) -> list[dict]:
        ids = sorted({str(i) for i in ids if i not in (None, "")}, key=_num)
        out = []
        for i in range(0, len(ids), ID_CHUNK):
            chunk = ids[i:i + ID_CHUNK]
            q = "{" + f"{field}[{' OR '.join(chunk)}]" + (f";{extra}" if extra else "") + "}"
            out.extend(self.list_entities(collection, q, fields))
        return out

    def field_meta(self, entity: str) -> list[dict] | None:
        """Field names and labels of an entity type, or None if the server won't say."""
        try:
            data = self.get(f"{self.proj}/customization/entities/{entity}/fields", {"required": "false"})
        except AlmError as e:
            log.debug("No field metadata for %s: %s", entity, e)
            return None
        fl = data.get("Fields") or data.get("fields") or {}
        if isinstance(fl, dict):
            fl = fl.get("Field") or fl.get("field") or []
        res = []
        for f in fl:
            name = f.get("Name") or f.get("name")
            if name:
                res.append({"name": name, "label": f.get("Label") or f.get("label") or "",
                            "type": f.get("Type") or f.get("type") or ""})
        return res or None


def _flatten(entity: dict) -> dict:
    out = {}
    for f in entity.get("Fields", []) or []:
        vals = f.get("values") or []
        vs = [v.get("value") for v in vals if isinstance(v, dict) and v.get("value") not in (None, "")]
        out[f.get("Name")] = vs[0] if len(vs) == 1 else (";".join(map(str, vs)) if vs else None)
    return out


def _snip(r) -> str:
    t = (r.text or "").strip().replace("\n", " ")
    m = re.search(r"<Title>(.*?)</Title>", t) or re.search(r'"Title"\s*:\s*"([^"]+)"', t)
    return (m.group(1) if m else t)[:300]


def _num(x):
    try:
        return (0, int(x))
    except (TypeError, ValueError):
        return (1, str(x))


# --------------------------------------------------------------------------- #
# Field resolution
# --------------------------------------------------------------------------- #
# Standard fields we read, and alternatives by name/label in case a project differs.
OPTIONAL = {
    "test-instance": {
        "responsible": (["actual-tester", "owner"], ["Responsible Tester"]),
        "planned_date": (["plan-scheduling-date"], ["Planned Exec Date", "Planned Execution Date"]),
        "exec_date": (["exec-date"], ["Exec Date", "Last Run Date"]),
        "exec_time": (["exec-time"], ["Time", "Exec Time"]),
    },
    "run": {
        "tester": (["owner"], ["Tester"]),
        "duration": (["duration"], ["Duration"]),
        "exec_time": (["execution-time"], ["Exec Time", "Execution Time"]),
    },
    "test": {
        "description": (["description"], ["Description"]),
        "subtype": (["subtype-id"], ["Type"]),
    },
}
USER_KEYS = {"environment", "application", "assigned_to", "planned_date", "tester"}


class Fields:
    def __init__(self, client: AlmClient, user_fields: dict):
        self.meta = {e: client.field_meta(e) for e in ("run", "test-instance", "test")}
        self.resolved: dict[tuple, str | None] = {}
        for ent, keys in OPTIONAL.items():
            for key, (names, labels) in keys.items():
                self.resolved[(ent, key)] = self._resolve(ent, names, labels)
        self.user: dict[str, tuple[str, str]] = {}
        for key, spec in (user_fields or {}).items():
            if key not in USER_KEYS:
                raise AlmError(f"ALM_REPORT_FIELDS: unknown key '{key}'. Valid keys: {', '.join(sorted(USER_KEYS))}")
            m = re.fullmatch(r"(run|test-instance|test)\.([\w-]+)", str(spec).strip())
            if not m:
                raise AlmError(f"ALM_REPORT_FIELDS: '{spec}' must look like run.user-01, test-instance.<field> or test.<field>")
            ent, name = m.groups()
            if self.meta[ent] is not None and name not in {f["name"] for f in self.meta[ent]}:
                raise AlmError(f"ALM_REPORT_FIELDS: field '{name}' does not exist on {ent}. "
                               "Run with --list-fields to see the valid names.")
            self.user[key] = (ent, name)

    def _resolve(self, ent, names, labels):
        meta = self.meta.get(ent)
        if meta is None:
            return names[0]          # can't check; use the standard name
        by_name = {f["name"] for f in meta}
        for n in names:
            if n in by_name:
                return n
        low = {f["label"].strip().lower(): f["name"] for f in meta}
        for l in labels:
            if l.lower() in low:
                return low[l.lower()]
        log.info("Field %s not found on %s; that column will be empty.", names[0], ent)
        return None

    def get(self, ent, key):
        return self.resolved.get((ent, key))

    def list_for(self, ent, base: list[str]) -> list[str]:
        out = list(base)
        for (e, _k), name in self.resolved.items():
            if e == ent and name:
                out.append(name)
        for e, name in self.user.values():
            if e == ent:
                out.append(name)
        return list(dict.fromkeys(out))


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def folder_paths(folders: list[dict], root_name: str = "Root") -> dict[str, str]:
    by_id = {str(f["id"]): f for f in folders}
    cache: dict[str, str] = {}

    def path(fid, depth=0):
        fid = str(fid)
        if fid in cache:
            return cache[fid]
        f = by_id.get(fid)
        if f is None or depth > 100:
            return root_name if fid in ("0", "", "None") else f"(folder {fid})"
        parent = str(f.get("parent-id") or "0")
        p = f.get("name") or fid if parent in ("0", "") or parent == fid else path(parent, depth + 1) + "\\" + (f.get("name") or fid)
        cache[fid] = p
        return p

    for fid in by_id:
        path(fid)
    return cache


def descendants(folders: list[dict], roots: list[str]) -> set[str]:
    kids = defaultdict(list)
    for f in folders:
        kids[str(f.get("parent-id"))].append(str(f["id"]))
    seen, stack = set(), [str(r) for r in roots]
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        stack.extend(kids.get(x, []))
    return seen


def to_ts(date_s, time_s, tz: ZoneInfo | None) -> str | None:
    """ALM date (YYYY-MM-DD) + time (HH:MM:SS) -> ISO string, UTC when the ALM timezone is known."""
    if not date_s:
        return None
    d = str(date_s).strip()[:10]
    t = (str(time_s).strip() if time_s else "") or "00:00:00"
    if len(t) == 5:
        t += ":00"
    try:
        naive = dt.datetime.fromisoformat(f"{d}T{t[:8]}")
    except ValueError:
        return f"{date_s} {time_s or ''}".strip()   # report flags unreadable dates
    if tz is None:
        return naive.isoformat()
    return naive.replace(tzinfo=tz).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<(br|/p|/div|/li)[^>]*>", "\n", str(s), flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(s)).strip()


def test_type(subtype):
    if not subtype:
        return ""
    s = str(subtype).upper()
    if s == "MANUAL":
        return "Manual"
    if s in ("BUSINESS-PROCESS", "FLOW"):
        return "Business process"
    return "Automated" if s in AUTOMATED_TYPES or "TEST" in s else str(subtype)


def map_status(s):
    if s is None or str(s).strip() == "":
        return ""
    return STATUS_MAP.get(str(s).strip().lower(), str(s).strip())


def extract(client: AlmClient, args, fields: Fields) -> dict:
    alm_tz = ZoneInfo(args.alm_timezone) if args.alm_timezone else None
    warnings: list[str] = []
    U = fields.user

    # 1. Test Lab structure
    log.info("Reading Test Lab folders…")
    ts_folders = client.list_entities("test-set-folders", fields=["id", "name", "parent-id"])
    ts_paths = folder_paths(ts_folders)

    log.info("Reading test sets…")
    set_fields = ["id", "name", "parent-id"]
    if args.test_set_id:
        test_sets = client.list_by_ids("test-sets", "id", args.test_set_id, fields=set_fields)
        missing = set(map(str, args.test_set_id)) - {str(s["id"]) for s in test_sets}
        if missing:
            raise AlmError(f"Test set id(s) not found: {', '.join(sorted(missing))}")
    elif args.test_set_folder_id:
        ids = descendants(ts_folders, args.test_set_folder_id)
        test_sets = client.list_by_ids("test-sets", "parent-id", ids, fields=set_fields)
    else:
        test_sets = client.list_entities("test-sets", fields=set_fields)
    if not test_sets:
        raise AlmError("No test sets matched. Check --test-set-id / --test-set-folder-id.")
    set_name = {str(s["id"]): f"{ts_paths.get(str(s.get('parent-id')), 'Root')}\\{s.get('name')}" for s in test_sets}
    set_ids = list(set_name)
    log.info("  %d test set(s)", len(set_ids))

    # 2. Test instances
    log.info("Reading test instances…")
    ti_fields = fields.list_for("test-instance", ["id", "test-id", "cycle-id", "status"])
    instances = client.list_by_ids("test-instances", "cycle-id", set_ids, fields=ti_fields)
    log.info("  %d test instance(s)", len(instances))

    # 3. Runs
    log.info("Reading runs…")
    run_fields = fields.list_for("run", ["id", "name", "test-id", "testcycl-id", "cycle-id", "status",
                                         "execution-date"])
    extra = ""
    if args.history_days:
        since = (dt.date.today() - dt.timedelta(days=args.history_days)).isoformat()
        extra = f"execution-date[>='{since}']"
    runs = client.list_by_ids("runs", "cycle-id", set_ids, extra=extra, fields=run_fields)
    log.info("  %d run(s)", len(runs))

    # 4. Tests (Test Plan)
    log.info("Reading Test Plan tests…")
    test_ids = {i.get("test-id") for i in instances} | {r.get("test-id") for r in runs}
    t_fields = fields.list_for("test", ["id", "name", "parent-id"])
    tests = {str(t["id"]): t for t in client.list_by_ids("tests", "id", test_ids, fields=t_fields)}
    tf = client.list_entities("test-folders", fields=["id", "name", "parent-id"])
    tf_paths = folder_paths(tf, "Subject")
    tf_by_id = {str(f["id"]): f for f in tf}

    def top_folder(fid):
        fid, seen = str(fid), set()
        while fid in tf_by_id and fid not in seen:
            seen.add(fid)
            p = str(tf_by_id[fid].get("parent-id") or "0")
            if p not in tf_by_id:
                return tf_by_id[fid].get("name") or ""
            fid = p
        return ""

    # 5. Defects
    defects_by_run: dict[str, list[str]] = defaultdict(list)
    if args.with_defects and runs:
        log.info("Reading defect links…")
        links = client.list_by_ids("defect-links", "second-endpoint-id", [r["id"] for r in runs],
                                   fields=["id", "first-endpoint-id", "second-endpoint-id", "second-endpoint-type"])
        for l in links:
            if str(l.get("second-endpoint-type") or "").lower() in ("run", "runs"):
                defects_by_run[str(l.get("second-endpoint-id"))].append(str(l.get("first-endpoint-id")))
        log.info("  %d run-defect link(s)", sum(len(v) for v in defects_by_run.values()))

    # 6. Build report rows
    def uf(key, ent, rec):
        spec = U.get(key)
        return (rec or {}).get(spec[1]) if spec and spec[0] == ent else None

    f_resp = fields.get("test-instance", "responsible")
    f_plan = fields.get("test-instance", "planned_date")
    f_edate = fields.get("test-instance", "exec_date")
    f_etime = fields.get("test-instance", "exec_time")
    f_rtester = fields.get("run", "tester")
    f_rtime = fields.get("run", "exec_time")
    f_dur = fields.get("run", "duration")
    f_desc = fields.get("test", "description")
    f_sub = fields.get("test", "subtype")

    inst_by_id = {str(i["id"]): i for i in instances}

    def test_info(tid):
        t = tests.get(str(tid)) or {}
        app = uf("application", "test", t) or top_folder(t.get("parent-id"))
        return {
            "name": t.get("name") or (f"(test {tid})" if tid else ""),
            "description": strip_html(t.get(f_desc)) if f_desc else "",
            "type": test_type(t.get(f_sub)) if f_sub else "",
            "application": app or "",
            "folder": tf_paths.get(str(t.get("parent-id")), "") if t else "",
            "almTestId": str(tid) if tid else "",
        }

    def inst_fields(i):
        t = tests.get(str(i.get("test-id"))) or {}
        assigned = uf("assigned_to", "test-instance", i) or (i.get(f_resp) if f_resp else None)
        planned = uf("planned_date", "test-instance", i) or (i.get(f_plan) if f_plan else None)
        app_i = uf("application", "test-instance", i)
        last = to_ts(i.get(f_edate), i.get(f_etime) if f_etime else None, alm_tz) if f_edate else None
        return {"assignedTo": assigned or "", "plannedDate": (str(planned)[:10] if planned else ""),
                "lastExecution": last or "", "appOverride": app_i,
                "neverRunStatus": str(i.get("status") or "").lower() in ("no run", "")}

    out_tests: dict[str, dict] = {}
    deleted = 0
    if args.unit == "instance":
        for i in instances:
            info = test_info(i.get("test-id"))
            f = inst_fields(i)
            out_tests[f"TI-{i['id']}"] = {
                "id": f"TI-{i['id']}", "name": info["name"], "description": info["description"],
                "type": info["type"], "application": f["appOverride"] or info["application"],
                "testSet": set_name.get(str(i.get("cycle-id")), ""), "assignedTo": f["assignedTo"],
                "plannedDate": f["plannedDate"], "lastExecution": f["lastExecution"], "almTestId": info["almTestId"],
            }
    else:
        grouped = defaultdict(list)
        for i in instances:
            grouped[str(i.get("test-id"))].append(i)
        for tid, insts in grouped.items():
            info = test_info(tid)
            fs = [inst_fields(i) for i in insts]
            planned = sorted(f["plannedDate"] for f in fs if f["plannedDate"])
            lasts = sorted(f["lastExecution"] for f in fs if f["lastExecution"])
            owners = list(dict.fromkeys(f["assignedTo"] for f in fs if f["assignedTo"]))
            out_tests[f"T-{tid}"] = {
                "id": f"T-{tid}", "name": info["name"], "description": info["description"], "type": info["type"],
                "application": next((f["appOverride"] for f in fs if f["appOverride"]), None) or info["application"],
                "testSet": info["folder"], "assignedTo": ", ".join(owners), "plannedDate": planned[0] if planned else "",
                "lastExecution": lasts[-1] if lasts else "", "almTestId": tid,
            }

    executions = []
    for r in runs:
        inst = inst_by_id.get(str(r.get("testcycl-id")))
        if args.unit == "instance":
            key = f"TI-{r.get('testcycl-id')}"
            if key not in out_tests:
                info = test_info(r.get("test-id"))
                deleted += 1
                out_tests[key] = {"id": key, "name": info["name"] + " (deleted instance)", "description": info["description"],
                                  "type": info["type"], "application": info["application"],
                                  "testSet": set_name.get(str(r.get("cycle-id")), ""), "almTestId": info["almTestId"]}
        else:
            key = f"T-{r.get('test-id')}"
            if key not in out_tests:
                info = test_info(r.get("test-id"))
                out_tests[key] = {"id": key, "name": info["name"], "description": info["description"],
                                  "type": info["type"], "application": info["application"],
                                  "testSet": info["folder"], "almTestId": info["almTestId"]}
            if inst is None:
                deleted += 1
        tester = uf("tester", "run", r) or (r.get(f_rtester) if f_rtester else None) or \
            ((inst or {}).get(f_resp) if f_resp else None) or ""
        dur = r.get(f_dur) if f_dur else None
        try:
            dur = int(float(dur)) if dur not in (None, "") else None
        except ValueError:
            pass
        executions.append({
            "testId": key, "runId": str(r["id"]), "runName": r.get("name") or "",
            "ts": to_ts(r.get("execution-date"), r.get(f_rtime) if f_rtime else None, alm_tz) or "",
            "status": map_status(r.get("status")), "statusRaw": r.get("status") or "",
            "tester": tester, "duration": dur,
            "environment": uf("environment", "run", r) or "",
            "defects": defects_by_run.get(str(r["id"]), []),
        })

    if deleted:
        warnings.append(f"{deleted} run(s) belong to test instances that no longer exist; they are shown as '(deleted instance)'.")
    if not alm_tz:
        warnings.append("ALM timezone not set (--alm-timezone): run times are shown as recorded by the ALM server.")

    meta = {
        "source": f"ALM {client.base}", "domain": args.domain, "project": args.project,
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "almTimezone": args.alm_timezone or "", "viewerTimezone": args.timezone or "",
        "unit": args.unit, "historyDays": args.history_days or None,
        "testSets": len(set_ids), "warnings": warnings,
    }
    return {"meta": meta, "tests": list(out_tests.values()), "executions": executions}


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_html(data: dict, path: Path) -> None:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    if tpl.count(MARKER) != 1:
        raise AlmError(f"{TEMPLATE.name} is missing the data marker {MARKER}. Use the original template.")
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    title = f"<title>ALM Execution Report · {html.escape(data['meta'].get('project') or '')}</title>"
    out = tpl.replace(MARKER, payload, 1).replace("<title>ALM Execution Report</title>", title, 1)
    path.write_text(out, encoding="utf-8")


CSV_COLS = ["Test ID", "Test Name", "Test Set", "Application", "Test Type", "Assigned To", "Planned Date",
            "Last Execution Date", "Run ID", "Execution Date Time", "Status", "ALM Status", "Executed By",
            "Duration Seconds", "Environment", "Defects", "ALM Test ID"]


def _safe(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def write_csv(data: dict, path: Path) -> None:
    by_test = defaultdict(list)
    for e in data["executions"]:
        by_test[e["testId"]].append(e)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLS)
        for t in sorted(data["tests"], key=lambda t: _num(t["id"].split("-", 1)[-1])):
            base = [t["id"], t.get("name"), t.get("testSet"), t.get("application"), t.get("type"), t.get("assignedTo"),
                    t.get("plannedDate"), t.get("lastExecution"), t.get("almTestId")]
            runs = sorted(by_test.get(t["id"], []), key=lambda e: e["ts"] or "")
            if not runs:
                w.writerow([_safe(x) for x in base[:8] + [""] * 8 + [base[8]]])
            for e in runs:
                w.writerow([_safe(x) for x in base[:8] + [e["runId"], e["ts"], e["status"], e["statusRaw"], e["tester"],
                                                         e["duration"], e["environment"], ";".join(e["defects"]), base[8]]])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Build an HTML test execution report from ALM (Quality Center) via REST.")
    p.add_argument("--test-set-id", action="append", default=[], metavar="ID", help="Test set to report on (repeatable).")
    p.add_argument("--test-set-folder-id", action="append", default=[], metavar="ID",
                   help="Test Lab folder; all test sets under it, including subfolders (repeatable).")
    p.add_argument("--unit", choices=["instance", "test"], default="instance",
                   help="instance: one row per test instance (default). test: one row per Test Plan test.")
    p.add_argument("--history-days", type=int, metavar="N", help="Only read runs from the last N days.")
    p.add_argument("--alm-timezone", metavar="ZONE", default=os.getenv("ALM_TIMEZONE"),
                   help="ALM server timezone, e.g. Europe/London. Recommended.")
    p.add_argument("--timezone", metavar="ZONE", default=os.getenv("REPORT_TIMEZONE"),
                   help="Default timezone for report viewers.")
    p.add_argument("--with-defects", action="store_true", help="Also read defects linked to runs.")
    p.add_argument("--list-fields", action="store_true", help="Print run, test-instance and test field names, then exit.")
    p.add_argument("--csv-only", action="store_true", help="Write only the CSV.")
    p.add_argument("--output-dir", default=os.getenv("REPORT_OUTPUT_DIR") or str(HERE / "reports"))
    p.add_argument("--page-size", type=int, default=int(os.getenv("ALM_PAGE_SIZE") or DEFAULT_PAGE_SIZE))
    p.add_argument("--insecure", action="store_true", help="Skip TLS certificate checks (self-signed servers only).")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    if load_dotenv:
        load_dotenv(HERE / ".env")
        load_dotenv()
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    url, domain, project = os.getenv("ALM_URL"), os.getenv("ALM_DOMAIN"), os.getenv("ALM_PROJECT")
    missing = [n for n, v in (("ALM_URL", url), ("ALM_DOMAIN", domain), ("ALM_PROJECT", project)) if not v]
    if missing:
        sys.exit(f"Missing settings: {', '.join(missing)}. Copy .env.example to .env and fill it in.")
    args.domain, args.project = domain, project
    for z in (args.alm_timezone, args.timezone):
        if z:
            try:
                ZoneInfo(z)
            except (ZoneInfoNotFoundError, ValueError):
                sys.exit(f"Unknown timezone '{z}'. Use an IANA name such as Europe/London or Australia/Melbourne "
                         "(on Windows also run: pip install tzdata).")
    try:
        user_fields = json.loads(os.getenv("ALM_REPORT_FIELDS") or "{}")
    except json.JSONDecodeError as e:
        sys.exit(f"ALM_REPORT_FIELDS is not valid JSON: {e}")

    verify: bool | str = not args.insecure
    if verify and os.getenv("ALM_CA_BUNDLE"):
        verify = os.getenv("ALM_CA_BUNDLE")
    if args.insecure:
        import urllib3
        urllib3.disable_warnings()
        log.warning("TLS certificate checks are OFF (--insecure).")

    client = AlmClient(url, domain, project, username=os.getenv("ALM_USERNAME"), password=os.getenv("ALM_PASSWORD"),
                       client_id=os.getenv("ALM_CLIENT_ID"), client_secret=os.getenv("ALM_CLIENT_SECRET"),
                       verify=verify, page_size=args.page_size)
    try:
        client.sign_in()
        if args.list_fields:
            for ent in ("run", "test-instance", "test"):
                meta = client.field_meta(ent)
                print(f"\n== {ent} ==")
                if not meta:
                    print("  (the server did not return field metadata)")
                    continue
                for f in sorted(meta, key=lambda f: f["name"]):
                    print(f"  {ent}.{f['name']:<28} {f['label']}")
            return 0
        fields = Fields(client, user_fields)
        data = extract(client, args, fields)
    except AlmError as e:
        log.error("ERROR: %s", e)
        log.error("No report was written.")
        return 2
    except requests.exceptions.SSLError as e:
        log.error("TLS error: %s\nSet ALM_CA_BUNDLE to your company CA file, or use --insecure for a self-signed test server.", e)
        return 2
    except requests.RequestException as e:
        log.error("Could not reach ALM: %s", e)
        return 2
    finally:
        client.sign_out()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out / f"alm_executions_{stamp}.csv"
    write_csv(data, csv_path)
    log.info("CSV:    %s", csv_path)
    if not args.csv_only:
        html_path = out / f"alm-execution-report_{stamp}.html"
        write_html(data, html_path)
        log.info("Report: %s", html_path)
    log.info("%d tests, %d runs.", len(data["tests"]), len(data["executions"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
