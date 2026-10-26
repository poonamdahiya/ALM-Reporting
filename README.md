# ALM Test Execution Report

Pulls test executions from **HP / Micro Focus / OpenText ALM (Quality Center)** through its REST API and writes a single interactive HTML report: what ran, when, who executed it, what the result was, and which tests have not run.

| File | Purpose |
|---|---|
| `generate_report.py` | Signs in to ALM, reads the Test Lab and Test Plan, writes the report (HTML) and a CSV to `reports/`. |
| `alm-execution-report.html` | The report template. Opened directly, it shows generated **sample data** (amber banner). You can also drop an ALM CSV export onto it. |
| `.env.example`, `requirements.txt` | Settings template and Python dependencies. |
| `sample_alm_export.csv` | Example of the CSV format (demo data, not real results). |

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in ALM_URL, ALM_DOMAIN, ALM_PROJECT and a login
python generate_report.py --list-fields          # optional: see field names in your project
python generate_report.py --with-defects
```

Open `reports/alm-execution-report_<timestamp>.html` in Chrome, Edge, Firefox or Safari. The file is self-contained: email it or put it on a share. It contains test data only, never the ALM login.

Requires Python 3.9+ and ALM 12.x or later (REST API). The account needs read access to the project only. All ALM calls are read-only `GET`s.

## Options

| Option | What it does |
|---|---|
| `--test-set-id ID` | Report on one test set. Repeat for several. |
| `--test-set-folder-id ID` | Every test set under a Test Lab folder, including subfolders. |
| *(neither)* | Every test set in the project. |
| `--unit instance` (default) | One row per **test instance** (test inside a test set), like the Test Lab. |
| `--unit test` | One row per **Test Plan test**, all instances combined. Test set becomes the Test Plan folder. |
| `--history-days N` | Read only runs from the last N days. Faster on big projects. Older tests show as *Run before the period* (from ALM's last-run date), not *Never executed*. |
| `--alm-timezone ZONE` | Timezone ALM records run times in, e.g. `Europe/London`. **Recommended**: ALM stores dates without a zone. Also settable as `ALM_TIMEZONE`. |
| `--timezone ZONE` | Default timezone for viewers (they can change it in the report). |
| `--with-defects` | Also read defects linked to runs. |
| `--list-fields` | Print field names on runs, test instances and tests, then exit. |
| `--csv-only`, `--output-dir`, `--page-size`, `--verbose`, `--insecure` | Output and debugging. `--insecure` skips TLS checks (self-signed test servers only; prefer `ALM_CA_BUNDLE`). |

### Login

Either an API key (`ALM_CLIENT_ID` / `ALM_CLIENT_SECRET`, ALM 15+, via `/qcbin/rest/oauth2/login`) or a username and password (`/qcbin/api/authentication/sign-in`, falling back to `/authentication-point/authenticate` + `/rest/site-session` on older servers). The script signs in again if the session expires mid-run and signs out when done.

### Custom fields (environment, application …)

Environment is usually a user field. Find it with `--list-fields`, then in `.env`:

```bash
ALM_REPORT_FIELDS={"environment": "run.user-02", "application": "test.user-01"}
```

Keys: `environment`, `application`, `assigned_to`, `planned_date`, `tester`. Values: `run.<field>`, `test-instance.<field>` or `test.<field>`. An unknown field stops the script with a message naming it.

## Where each report field comes from

| Report field | ALM source |
|---|---|
| Test ID | `TI-<test instance id>`, or `T-<test id>` with `--unit test` |
| Test name, description, type | Test Plan test (`name`, `description`, `subtype-id` → Manual / Automated) |
| Test set | Test Lab folder path + test set name |
| Application | Top-level Test Plan folder, or a field from `ALM_REPORT_FIELDS` |
| Run ID, date & time, status, duration | Run: `id`, `execution-date` + `execution-time`, `status`, `duration` |
| **Executed by** | Run `owner` (Tester), falling back to the instance's Responsible Tester |
| Assigned to, planned date | Test instance Responsible Tester, Planned Exec Date |
| Last execution (ALM) | Test instance `exec-date` + `exec-time` |
| Defects | `defect-links` pointing at the run (`--with-defects`) |

Statuses: Passed, Failed, Blocked as named; *Not Completed* → **In progress**; *No Run* / *N/A* → **Not run** (kept in history, never counted as executed). Any other value is shown and flagged as **Unrecognized**; add it to `STATUS_MAP` at the top of the script to map it.

Field names that differ between ALM versions (Responsible Tester, planned date, tester) are looked up by name and label from the project's field metadata, so the script adapts to most projects without changes.

## Accuracy safeguards

- **All or nothing.** If any request fails after retries, no report is written, so a partial download never makes tests look unexecuted.
- **Paging.** Every list is paged to ALM's `TotalResults`, even if the server caps page size; a short read stops the run.
- **Deleted instances.** Runs whose test instance was deleted still count, marked "(deleted instance)".
- **Timezones.** Run times are converted from the ALM timezone to UTC; the report buckets days in the viewer's timezone, including across daylight-saving changes.
- **CSV safety.** Values starting with `= + - @` are prefixed with `'` so Excel won't treat them as formulas.

## Using the report

**Filter bar:** search (test ID, name, run ID, defect), **From / To date with optional times**, quick periods (Today, Yesterday, 7/14/30 days, Month to date, All data), and multi-select filters: Status, **Executed by**, Environment, Test set, Application, Test type, Assigned to. Active filters show as removable chips.

**Tabs**

1. **Overview** – cards (tests in scope, executed in period with coverage, attempts, latest Passed/Failed/Blocked/In progress, pass rate, never executed, not executed in period, overdue), executions-per-day chart (click a bar to open that day), who executed, coverage by test set, data-quality notes.
2. **Daily executions** – every run grouped by day: time, test, test set, executed by, status, attempt number ("2 of 2, rerun (latest)"), duration, environment, run ID, defects.
3. **Today** – runs on the reporting date and the latest status per test.
4. **By tester** – attempts, unique tests, pass/fail/blocked and pass rate per tester, plus a tester × day grid.
5. **Not executed** – A Never executed · B Not executed in period (with reason) · C Not executed today · D Overdue.
6. **Test history** – every test with run count, last status, last executed by and last 10 results. Click any test ID anywhere for its full run list.

Every table sorts by clicking a header and has **Export CSV** (follows the current period, filters and search; the first line records them). **Print** prints the current tab (use "Save as PDF" to share). **Data & settings** sets the report timezone, the reporting date, ambiguous date order, and whether Blocked counts in the pass rate, and lets you load a CSV export or the script's CSV instead.

## How the numbers are counted

| Metric | Definition |
|---|---|
| Attempt | One ALM run. Reruns are separate attempts. |
| Executed in period | Unique tests with at least one run inside the period. |
| Passed / Failed / … cards | Unique tests by the status of their **latest** run in the period. |
| Pass rate | Passed ÷ (Passed + Failed) attempts in the period (optionally + Blocked). |
| Never executed | No run on record and no ALM last-execution date. |
| Not executed in period | No run in the period; labelled *Never executed*, *Run before the period*, *Run only after the period* or *Run (date unknown)*. |
| Overdue | Planned date before the reporting date and no run on or after it. |
| Coverage | Executed in period ÷ tests in scope. |

Failed, blocked and in-progress runs count as executed. **Status** and **Executed by** filters narrow execution lists, charts and tester tables only; they never change coverage or the not-executed lists. **Environment** changes what counts as executed ("executed in UAT"). The period start is inclusive; the end date runs to 23:59:59 (or the end of the chosen minute).

## Run it every day automatically

**Windows Task Scheduler** – create a basic task, daily at e.g. 07:00, action *Start a program*:
- Program: `C:\Path\To\python.exe`
- Arguments: `generate_report.py --with-defects --history-days 90 --output-dir \\fileshare\qa\alm-reports`
- Start in: the folder containing `generate_report.py`

**Linux / macOS cron**

```cron
0 7 * * 1-5  cd /opt/alm-execution-report && /usr/bin/python3 generate_report.py --with-defects --history-days 90 >> reports/run.log 2>&1
```

The script exits with code 2 and writes nothing if ALM can't be read, so a failed run never overwrites a good report.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `HTTP 401` at sign-in | Wrong credentials, or SSO-only account: use an API key. |
| `did not return JSON` | `ALM_URL` points at the wrong place; it should be the address you use for `…/qcbin`. |
| TLS / certificate error | Set `ALM_CA_BUNDLE` to your company CA file. |
| Times off by hours | Set `--alm-timezone` to the ALM **server's** timezone. |
| "Executed by" empty | Your project may store the tester elsewhere: check `--list-fields` and set `"tester": "run.<field>"`. |
| Slow on large projects | Use `--test-set-folder-id` and/or `--history-days`. |

## Tested

Against a mock ALM REST server (JSON format, page-size cap of 7, query filters, session expiry mid-run): folder subtree selection, paging, re-login, defect-link filtering, deleted instances, both `--unit` modes, `--history-days`, wrong password and unknown custom field (both stop with no output). Report totals were recomputed independently from the CSV and matched; the CSV loaded back into the report gives the same figures. All tabs render without errors in Chromium. Not yet run against a real ALM server, so expect to check field names with `--list-fields` on first use.
