# Subscription database (POC 2)

POC 2 replaces the "Subscription Summary" workbook with a normalised database. The workbook is used **once** — to
load existing data — and the app then works only against the database. (POC 1, in the sibling project
`shiperp-revenue-waterfall-ui`, keeps the workbook as the source of truth instead.)

## Data model
`customer` → `subscription` (one licence line per platform) → `term` (each contracted period: INITIAL, CONVERSION,
ADD_ON, RENEWAL, AMENDMENT) · `term_supersession` (a renewal/amendment replacing one or more terms, e.g. base +
add-ons) · `termination` (an event — no negative mirror rows) · `invoice` · `control_check_type` / `control_check`
(the accounting checklist) · `non_recurring_fee` · `account_exec` · `reseller` · `audit_log` ·
`maintenance_contract` / `maintenance_fee` (perpetual licences and their agreed annual maintenance fee).

Nothing derived is stored: ARR (TCV ÷ term), ARR by calendar year, CARR, statuses, renewals due, retention and the
monthly revenue schedule are computed in `app/metrics.py`. `term.arr_override` only keeps a legacy ARR the sheet used
when it differs from TCV ÷ term (shown with an "override" badge).

ARR-by-year follows the sheet's convention so totals reconcile: a term counts its full ARR in every calendar year it
touches, except from the start year of a term that supersedes it and after its subscription's cancellation year.

## Company ARR
ARR(year) = subscription ARR + maintenance fees of the maintenance year **ending** in that year ("2025-2026" -> 2026),
for **every** customer, as the workbook's spec 5.1 (accounting's ~10.2M for 2026). A customer with an active
subscription next to perpetual maintenance is a **Hybrid** account (workbook "License Category" = Hybrid): both
count (confirmed by accounting 2026-11-18; this replaces the 2026-11-13 rule that left a converted customer's
maintenance out as a prepaid leftover). A conversion stops the maintenance line itself (explicit 0 from the next
contract year), so whatever is still billed is ARR until then. The ARR CSV lists it as `MAINTENANCE`.

**License Category / Hybrid class** are stored per customer (migration 0012; `customer.license_category`,
`customer.hybrid_class`), set by the import: Hybrid when any of the customer's contract rows says so, otherwise the
most recent row's value. Rows that disagree are a sheet error, reported by /check (X08); X06 flags a converted
customer that still pays maintenance but is not marked Hybrid, X07 a Hybrid customer with a perpetual class and no
maintenance billed. The customer page and the customers list show a Hybrid badge; there is no form for the fields.

## Projected Renewal (agreed 2026-11-16)
A contract often ends before the customer signs the renewal. Rather than show that gap as churn (the workbook types the
ARR into the current year's column by hand), the app carries the contract as a **Projected Renewal**: an estimate,
flagged everywhere, that disappears by itself once the real renewal or a termination is recorded.
- **Which contracts** (`metrics.is_projected`): ended before today, nothing supersedes it (no renewal/amendment), no
  termination (whole subscription or this contract), ARR above 0, the renewal note does not say it will not renew
  (churn, terminat…, cancel…, final period, not renewing), and not an add-on that ended while its subscription carries
  on.
- **Amount:** prior ARR × (1 + uplift), applied once (not compounded). The uplift is the contract's own *Projected
  renewal uplift %* (term page, audited and undoable) or the default `PROJECTED_RENEWAL_UPLIFT` (0.05 = 5%, range
  0–50%).
- **When:** in ARR today from the day after the end date; in ARR by year for every year after the end year (the end year
  already counts in full). It is **in headline ARR** and in CARR (so "Signed, not started" is unchanged), and lasts
  until the renewal or a termination is recorded.
- **Where it shows:** a warning line on the dashboard ("ARR includes $X of projected renewals on N contracts") and an
  "of which projected" row under ARR by year; a **Projected** badge and the projected ARR in the renewals worklist
  (filter *Projected renewals*); "projected renewal +5%" on the customer page with its own row under ARR by year;
  `ON_PREM_PROJECTED` / `CLOUD_PROJECTED` rows in the ARR CSV (the `ON_PREM` / `CLOUD` rows stay signed-only); check
  rule **R4** (info) where the workbook's ARR year cells differ because of it.
- shiperp-dashboard computes ARR on its own from this database and does not include projections yet.

## Deferred Billing (header link, `/deferred-billing`)
For each customer and each month-end it shows two figures:
- **Balance** = TCV ("TCV - NN & Existing") less the amounts invoiced up to that month-end.
- **Invoiced** = the amount invoiced in that month.

How it works:
- **Data:** read from PostgreSQL (`term.tcv` and the `invoice` table), so invoices recorded in the app count straight
  away. The import fills those from the workbook's "Amount" and monthly billing columns, found by header text.
- **When a contract counts:** from its start or its first invoice, whichever is earlier. Before that it is blank, so
  a renewal doesn't inflate earlier months.
- **Undated invoices** count at 12/31 of their billing year.
- **Over-billing:** invoicing more than the TCV gives a negative balance, shown in parentheses.
- **Columns** run from the first contract month through the later of this month and the last invoice month.
- **Rows:** each customer row expands to show its contracts. Totals of both figures are at the bottom.
- **"With a balance"** (the default) hides customers whose balance is zero in every month.
- **Download Excel** (`/deferred-billing.xlsx`) gives the same view and filters: a Balance and an Invoiced column per
  month, with the contracts grouped under each customer.

Code: `app/deferred.py` (calculation), `app/deferred_export.py`; tests in `tests/test_deferred.py`.

**Cross-check against the workbook:** the test compares 431 year-end balances with the workbook's own "GL#1215
Deferred Billing" columns. 30 differ, all from workbook data:
- duplicate entries in the 2020 group (A90, A91);
- invoice dates in a different year from their column group (Cloud A10, A61, A66);
- TCV 0 with a deferred balance (A87, A88);
- invoiced more than the TCV (A20).

Amended contracts are also affected, because the database splits the TCV between the old and the new term.

## Conversion, add-on, termination (workflows)
- **Conversion** (customer page) replaces the perpetual licence: it creates the CONVERSION subscription and, for each
  ticked maintenance line (ticked by default), stops maintenance (explicit 0) from the first contract year starting on
  or after the subscription start - one change, one Undo. Keeping the perpetual licence (and its maintenance) next to
  a subscription makes the account **Hybrid** on the workbook: record it as a **New subscription** and leave the
  maintenance line running.
- **Add-on** = extra modules / plants / users added to an existing subscription contract (sheet section C), usually
  co-terminus with the main contract. **New subscription** = a separate contract on a platform the customer does not
  have yet (or after a cancellation).
- **Amend** shows the ARR change and offers **Keep current ARR** (TCV = current ARR x new term) for co-terming.
- **Renew** merge popup: Continue, Keep all separate, or Cancel renewal.
- **Terminate** lists the subscription's open contracts: all ticked cancels the subscription; unticking ends only the
  ticked contracts (`termination_term`, migration 0010) - the others keep running and keep their ARR. Each contract row
  also has a Terminate button for just that contract.

## Run
The app runs in Docker on PostgreSQL only (see "Run in Docker" below): `docker compose up -d --build`, then
http://localhost:8021. The schema is managed with Alembic (`alembic/`); the app container runs `alembic upgrade head`
on every start.

Money is `Decimal`: amounts are `NUMERIC(14,2)` (migration 0004), every assignment is rounded to the cent half-up
(`app/money.py`), and derived figures (ARR, monthly revenue, deferred) are computed in Decimal and rounded only for
display.

Contract documents (SharePoint links only): every term and maintenance line has a "Contract documents" panel, and the
customer page a **Link** button in its Docs column. Paste the file's SharePoint link (https://…sharepoint.com; direct
path, library view `?id=` link or sharing link). The app stores only the link: it never connects to SharePoint, and the
file opens with the viewer's own SharePoint access. Optional `SP_ALLOWED_HOSTS=yourcompany.sharepoint.com` in `.env`
restricts links to the company's host. **Contract links (.csv)** on the customer page lists every link with its term /
maintenance line, period, TCV, ARR and sheet ref. Re-imports keep the links (re-attached by customer number + sheet ref;
unmatched ones stay on the customer as "unlinked").

## Run in Docker (PostgreSQL)
`compose.yaml` (the app, also the Azure deploy contract) plus `compose.override.yaml` (local only: `db`, volumes,
loopback port, sign-in off; merged automatically) run the app (`http://127.0.0.1:8021`) on PostgreSQL (`db`, host port
`127.0.0.1:5433`).
`.env` needs `POSTGRES_PASSWORD` and `DASHBOARD_DB_PASSWORD` (hex; see `.env.example`) besides the `SP_*` values.
```powershell
docker compose up -d --build                      # app runs `alembic upgrade head`, then serves; /healthz = probe
python -m pip install -r requirements.txt         # host side: tests and scripts
python -m pytest -q                               # needs the db container (uses database subs_test)
```
- **Import the spreadsheets** into Postgres with the header's **Refresh data**: it always pulls the two workbooks
  from SharePoint (the single source of truth). Command line (re-imports the copies last pulled):
  `docker compose exec app python -m app.importer`.
- **Backups**: before every Refresh / import the whole database is copied into its own schema
  (`backup_YYYYMMDD_HHMMSS`, `app/backups.py`); the newest 10 are kept and listed on the Database page. Put one back
  with `docker compose exec app python scripts/restore_backup.py <name>` (lists them without a name; the current data
  is backed up first). The dashboard's `dashboard_ro` login cannot read the backup schemas.
- **shiperp-dashboard** reads as `dashboard_ro` (SELECT only; password `DASHBOARD_DB_PASSWORD`) at `db:5432`,
  database `subscriptions`, after joining the external network `shiperp`. The role is created when the `pgdata`
  volume is first initialised (`services/db/init/`); a re-import keeps its grant (default privileges).
- From Windows, use `127.0.0.1`, not `localhost`, in Postgres URLs (localhost tries IPv6 first: ~2 s per connection).
- Tests run against the throwaway database `subs_test` in the same container (created on first run; URL built from
  `POSTGRES_PASSWORD`, or set `TEST_DATABASE_URL`). Each test works in its own schema; the live `subscriptions`
  database is refused. If the container is not running, every test is skipped with a message.
- The SQLite file used before the move (`data\subscriptions.db`) and its `.db` backups are archived in `data\archive\`
  and no longer used.

## Maintenance (perpetual licences) — see MAINTENANCE-WORKBOOK.md
Tables: `maintenance_contract` (one register line: block, product, anniversary month / calendar billing, go-live,
converted / ended with date and reason, add-on lines linked to their parent), `license_purchase` (original licence,
module / plant / carrier purchases, decommissions, with the maintenance each adds), `maintenance_fee` (fee per
contract year, keyed by the year it ends: "2025-2026" = 2026; **no row = blank / not billed, 0 = explicitly stopped**),
`maintenance_invoice` (QuickBooks 1000-MT invoices incl. advance billing). Computed, not stored: total current licence,
original / additional maintenance, cumulative uplift, total maintenance, maintenance %, monthly revenue (steps at the
anniversary), GL 4300 revenue per calendar year and 12/31 GL 2310 deferred.

UI: **Maintenance** menu — totals by block and GRAND TOTAL per contract year, filters (block, anniversary month, missing
next year, stops next year, not live, ended), checks (annual roll due, invoiced vs fee, value after a stop), contract
page (pricing, fee per contract year with edit, licence history, invoices, revenue schedule), workflows with preview:
new perpetual licence (first year prorated), mark live, annual roll (+4 % default, per anniversary month, untick lines),
additional purchase, decommission, convert to subscription, terminate.

Reload only maintenance from the workbook (keeps subscriptions and their app changes; refuses if maintenance was changed
in the app): `python -m app.importer --maintenance-only`.

## Sign-in
Microsoft (Entra ID) sign-in, the same flow as shiperp-dashboard (`app/auth.py`): `/login` -> Microsoft ->
`/auth/callback`; access for members of `ALLOWED_GROUP` and/or e-mails on `ALLOWED_USERS`; the audit log records the
signed-in e-mail. `AUTH_DISABLED=1` (the local Docker default) turns it off, and the app refuses to start that way when
`APP_ALLOWED_HOSTS` names any non-loopback host. With sign-in on, `SESSION_SECRET_KEY` (32+ characters) is required.
Each session records a fingerprint of the access rule (`ALLOWED_GROUP` + `ALLOWED_USERS`) it was granted under; when
either setting changes, existing sessions are sent back through sign-in, so the new rule applies at once rather than
when the 12-hour session ends.

## Safety on the local machine
With sign-in off locally, the app protects itself from other web pages open in the
same browser: it answers only to `127.0.0.1` / `localhost` (add names with `APP_ALLOWED_HOSTS` in `.env`), form
posts from another site are refused (Origin / Referer check), `back` links only go to pages of the app, and CSV exports
prefix text cells starting with `= + - @` with an apostrophe so Excel does not run them as formulas.
`python -m app.importer` backs the database up (a `backup_*` schema) first and reads the workbook before anything is
dropped (a locked or broken file, or renamed / look-alike columns, leave the database as it was).

## Viewing the database
- In the app: **Database** in the top menu (`/db`) — table list with row counts and purpose, a diagram of how the
  tables connect, and per-table pages (structure, search, sort, paging, click foreign keys to follow them). Read-only.
  The page names the database (never the login), shows its size and the backup list.
- Outside the app: any PostgreSQL client (e.g. PyCharm's Database tool window) at `127.0.0.1:5433`, database
  `subscriptions` - preferably as the read-only `dashboard_ro`. Don't edit data there: it bypasses the audit log and
  the business rules.

## Before go-live: spreadsheet check
Goal: make both source workbooks 100 % accurate, then import. **Spreadsheet check** (menu) or
`python -m app.checker [--export]` reads the workbooks in `data\source\` (read-only) and lists every issue with its
exact cell, the current and expected value and how to fix it. Importer-based findings come from a trial import into a
throwaway schema (`sheetcheck_<pid>`, dropped afterwards) - the live tables and import report are never touched.

1. Run the check; work through **errors** (wrong data), **decisions** (open ARR rules: cancellations, renewal
   transition years), then **warnings**; **info** is spelling / tidy-up.
2. Fix the cell in the original workbook, save it in `data\source\`, run the check again. If a finding is correct as
   it is, **Accept** it with a reason (`data\checks\accepted.json`; it re-opens if the cell value changes).
3. Downloads: the issue list (.xlsx) and highlighted copies of each workbook (problem cells coloured with a comment,
   first sheet "Check issues" links to every cell) - review copies only, saved in `data\checks\`.
4. When nothing important is open, import (`python -m app.importer.cli`). The importer shows the check status and
   asks to confirm while errors are open or a workbook changed since the check (`--yes` skips the question).

## Refresh data (header button)
Loads the workbooks on SharePoint (the production source of truth until go-live) into the database - always from
SharePoint, never from files on a laptop. **Refresh data** opens a popup showing the two workbooks on SharePoint (last
modified, by whom, changed since the last import?), the spreadsheet check status and the changes made in the app since
the last import, then:

1. downloads both workbooks (read-only Microsoft Graph), checks them (name, opens as Excel) and swaps them into
   `data/source` (old copies -> `data/backups/source-<stamp>/`); nothing changes if one is bad;
2. backs up the database into a `backup_YYYYMMDD_HHMMSS` schema and saves the change list
   (`data/backups/audit-before-refresh-<stamp>.json`); the column check (renamed / look-alike columns) refuses here,
   before any table is touched;
3. re-imports (every table, including the audit log, is rebuilt from the workbooks; contract links are kept);
4. **Reapply?** - default **Do not reapply**: the database then matches the workbooks exactly. "Reapply" replays the
   changes through the normal operations (`app/reapply.py`), matching rows by customer number + sheet ref (or, for
   terms created in the app, platform + kind + dates + TCV): new customer / subscription, add-on, renewal, amendment,
   termination, term edit, maintenance conversion. Checklist ticks, invoices and other maintenance operations are
   listed to redo by hand; changes already in the workbook are skipped. Each reapplied change has its own Undo.
5. The result page (`/refresh/result`) shows ARR / CARR before -> after (dashboard figures), the import summary and
   what was reapplied / skipped.

Setup (`.env`): `SP_TENANT_ID`, `SP_CLIENT_ID`, `SP_CLIENT_SECRET` (Entra app with Sites.Selected **read** on the
fi_team site) and the sharing links `SP_SUBSCRIPTION_URL`, `SP_MAINTENANCE_URL`. The app only reads the workbooks; it
never writes to SharePoint, and contract documents stay links-only. If SharePoint cannot be reached (not set up, no
access, offline), the popup shows why and the Refresh button is disabled - nothing changes.

## Import rules (see the Import page for every decision with its sheet reference)
- A → NET_NEW subscription + INITIAL term; B → CONVERSION; C → ADD_ON; D → RENEWAL superseding the rows named in the
  sheet's "previous ARR" formula (or, failing that, terms ending within 31 days before it starts).
- E rows → terminations (cancel date: Date Cancelled, "Cancelled m/d/y", or the period end). E amounts are dropped.
- Negative non-E rows (amendment reductions) are folded into the row with the same period.
- "Not a subscription" → non-recurring fee. Duplicate rows and rows without any usable date are rejected.
- Unparsable periods are estimated from the signed date and term; invalid days are clamped.

## Reconciliation (as of 2026-11-12 import)
Maintenance: every year 2016–2027 ties to the maintenance workbook's GRAND TOTAL to the cent.
2020–2021 match exactly. The 22 differences in 2022–2026 are all classified: renewal transition years (sheet keeps
the old ARR in the year a renewal starts), blank sheet cells on live terms, sheet values outside any term, and ARR the
sheet still carries after a cancellation date (e.g. #80239 cancelled 11/30/2024) — **those need accounting's call**.
Future-year (2027+) differences are mostly cells the sheet never filled.

## Not covered yet
Editing maintenance fees in the UI (read-only today), roles beyond one access group, deferred revenue / GL tie-out, attachments,
Salesforce/QuickBooks integration.

## Azure (live since 2026-11-15)
az-deploy-agent deploys `compose.yaml` to the private Container Apps environment `cae-agents-private` (westus2,
VPN only), one replica.

Data resources live in their own resource group `rg-data-shiperp-subscription-db` (never `rg-app-…`, so tearing the
app down cannot delete the data):
- PostgreSQL Flexible Server `psql-shiperp-subs` (PG 16, B1ms, 32 GB, 7-day backups), public access **disabled**,
  reached through private endpoint `pe-psql-shiperp-subs` (10.60.0.4 in `snet-azure-ai`; the VNet has no room for a
  delegated subnet) and the private DNS zone `privatelink.postgres.database.azure.com`. Logins: `pgadmin` (server
  admin), `subs` (owns database `subscriptions` and schema `public`; the app's login), `dashboard_ro` (SELECT only).
- Storage account `stshiperpsubsdata`, share `subs-data` (public access disabled, private endpoint 10.60.0.6),
  registered on `cae-agents-private` as environment storage `subs-data`, mounted on `/app/data`.

Neither is reachable from outside the VNet. To work on them from a PC that is not on the office network, open public
access for one IP for the duration and close it again (`az postgres flexible-server update --public-access …` plus a
firewall rule; `az storage account update --public-network-access …` plus a network rule).

`DATABASE_URL` (Key Vault secret `database-url`) must end in `?sslmode=require`. `/app/data` is the Azure Files share
mounted after the deploy (spreadsheet-check acceptances and reports live there).

Sign-in reuses **shiperp-dashboard's Entra app registration** (same tenant and client), but access is separate: this
app admits only its own group, "ShipERP Subscription DB Users" (`ALLOWED_GROUP` default in compose.yaml), while the
dashboard admits "ShipERP Dashboard Users". Who can use which app is decided by group membership in Entra.
The app's host name is fixed by the deployer as `<repo>-<service>`:
`shiperp-subscription-db-app.bravesmoke-02934eef.westus2.azurecontainerapps.io` (the `APP_ALLOWED_HOSTS` default in
compose.yaml; change it there if the first-pass report shows another name).

Deploy, step by step (tests and a review first, per the deploy rules):
1. Once: install the `az-deploy-agent-shiperp` GitHub App on the repository, and add
   `https://shiperp-subscription-db-app.bravesmoke-02934eef.westus2.azurecontainerapps.io/auth/callback` to the
   dashboard Entra app's web redirect URIs.
2. `uv run az-deploy-agent deploy --repo traimo/shiperp-subscription-db --ref main --exposure private` (from the
   AZ-Deploy-Agent folder). The first pass creates `rg-app-shiperp-subscription-db` and an empty Key Vault, then stops.
3. `python scripts/azure_secrets.py --vault <vault from the report>` fills it: `sp-*` from `.env`, `entra-*` from
   shiperp-dashboard's `.env`, `database-url` (login `subs`, `?sslmode=require`), a fresh `session-secret-key`,
   `redirect-uri`. `--dry-run` first lists what it will set; values are never printed.
4. Run the same deploy command again: builds and deploys the app.
5. Check `/app/data`: `python scripts/azure_mount_data.py --check`. compose.yaml declares the share under
   `x-azure.azure_files`, so an az-deploy-agent that supports it mounts it on every deploy (it refuses to deploy when
   the environment storage `subs-data` is missing). With an older deployer the mount is dropped on each redeploy: then
   run `python scripts/azure_mount_data.py` right after. While `/app/data` is not the share, every page shows a red
   "Data folder not mounted" banner and `/healthz` reports `"data_mount": "missing"` (still 200).
