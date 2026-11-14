# ShipERP Subscription Summary workbook — structure, fields, rules and scenarios

Written 2026-11-11 from the live workbook and from the dashboard code that reads it. Purpose: a self-contained
brief for a new project that builds a UI for accounting to maintain this spreadsheet. Nothing here depends on the
dashboard repository; where the dashboard's reading rules matter for compatibility they are spelled out.

Workbook: `2. ShipERP Subscription Summary_On-Premise and Cloud_ASC606.xlsx` (SharePoint; the dashboard pulls it by the
pattern `*Subscription Summary*.xlsx`). Owner: ERP Integrated Solutions LLC (ShipERP) accounting. Row 1 of each ARR tab
names the person responsible for each column group (Tin, Michelle, Norvy / Sophie, Marilou, Wincy).

---

## 1. What the workbook is for

It is accounting's **contract register and revenue schedule for every subscription licence** ShipERP sells (On-Prem
and Cloud), one row per contract term. It serves four jobs at once:

1. **ARR / CARR register** — the per-year ARR columns are the company's official ARR by year and the source of the
   dashboard's ARR, CARR, churn, GRR / NRR, renewals pipeline, customer counts, CAC denominator and new-logo counts.
2. **ASC 606 revenue recognition schedule** — the monthly columns to the right spread each contract's TCV straight-line
   over its term (Monthly Subscription Revenue), roll quarters and years, and tie the deferred revenue balance to the
   GL (GL 2320 On-Prem, GL 2340 Cloud) with a "Difference" column that should be 0.
3. **Billing tracker** — invoice date / amount per billing year, "Deferred Billing" (GL 1215.3 / 1215.4) balances,
   which contract year (Y1 / Y2 / Y3) was last invoiced and when the next one is due ("Review by Tin").
4. **Control checklist** — a block of yes / done / mismatched columns proving each row was verified against the
   commission report, Salesforce, the amortization schedule, QuickBooks posting, Celoxis SOs and the deferred revenue
   review.

Perpetual-licence **maintenance** ARR is **not** in this workbook; it lives in a separate workbook
(`6. ... Deferred Revenue Maintenance Fee Amortization ... Adv Billing.xlsx`, tab `Maintenance-Adj Advance Billing`,
one row per customer, one column per maintenance year "YYYY-YYYY"). The two are joined by **Customer Number**.

---

## 2. Tabs

| Tab | Role | Read by the dashboard |
|---|---|---|
| `On Prem_License ARR&CARR` | The On-Prem contract register (sections A–E). Title "ShipERP Subscription Fee Summary - On Premise and Cloud", "As of 07/31/2026" in row 5. ~278 rows × ~220 columns. | Yes (source = On-Prem) |
| `Cloud License ARR&CARR` | Same layout for Cloud contracts. "As of 04/30/2026". ~132 rows × ~213 columns. | Yes (source = Cloud) |
| `SF Recon&Upcoming Ren (Norvy)` | Monthly reconciliation of Salesforce closed contracts vs the summary tabs (On-Prem, Cloud, Perpetual) vs the commission report, split by section A–D. Contains `#VALUE!` cells today. | No |
| `GL` | Customer Name / PID# / PM list (project manager per account). | No |
| `SF Contracts Report` | Raw Salesforce "Contracts Report" export (as of a date in row 3), filtered by Contract Start Date. | No |
| `sorted by company` | The same SF export sorted: contract number (00000444), customer, start, end, status (Activated), owner. | No |
| `Masterlist` | ACCOUNT NUMBER ↔ CUSTOMER NAME (80001 …). The 80xxx numbers are the QuickBooks customer numbers used as **Customer Number** everywhere. | No |

Only the two ARR&CARR tabs carry data the dashboard computes from. The others are working papers; a maintenance UI can
treat them as reference lookups (Masterlist for the customer number list, the SF export for Salesforce contract ids).

---

## 3. Layout of an ARR&CARR tab

```
row 1      reviewer initials per column group (Tin / Michelle / Norvy / ...)
row 3-5    company, title, "As of MM/DD/YYYY"
row 7      "A. ON PREM NET NEW"                 <- section title: column A = "<letter>. <name>"
row 8      header row: column A = "Counter - Grand Total" ...   <- the MASTER header row
row 9..    contract rows (numeric Customer Number + Subscription Period)
           "Total Year 2020" ... subtotal rows inside section A (one per signing year)
row 104    "B. UPSELL - PERPETUAL TO SUBSCRIPTION"
row 105    header row (repeats the master, may differ slightly)
...
row 130    "C. UPSELL - SUBSCRIPTION CUSTOMERS' ADDITIONAL MODULE/PLANTS (Michelle)"
row 174    "D. RENEWAL (WITH UPTICK) (NTT COMMISSION 10%, MEHRWERK - 7.5%)"
row 214    "E. TERMINATED ACCOUNTS (ON PREM ONLY)"
           Grand Total rows at the bottom of sections / the tab
```

Rules a reader relies on (keep them when the UI writes the sheet):

- A **section** starts at a row whose column A matches `^[A-E]\.\s+<name>` (longer than 3 characters).
- The **header row** of a section is the first row after it whose column A starts with `Counter`. The tab's first header
  row (row 8) is the master; later header rows may rename or drop a header, and a missing header falls back to the
  master's column. Headers are matched **by text, never by column letter**, so columns may be inserted or moved.
- A **contract row** has a numeric Customer Number and a non-empty Subscription Period. Rows without both are
  subtotals, notes or blanks and are skipped.
- **`Total Year YYYY`** rows inside section A carry, in the "Counter - per Year" column, the count of net-new customers
  signed that year. That subtotal is the official **new customer count** (CAC denominator). Each contract row has
  "Counter - per Year" = 1 (0 on two rows today), and "Counter - Grand Total" is a running count within the section.
- Section E (terminated) on On-Prem renames some headers: `Cutomer Number` (sic), `Customer Name`, `Date Cancelled`
  in place of `Contract Signed Date`, `ARR` in place of `ARR - Annual Recurring Revenue`, and the year columns read
  `ARR 12/31/2020` … `ARR TODATE - 2025`. Cloud section E keeps the master headers except the year columns. A year
  column is any header matching `^ARR … (20\d\d)`.

### Column regions (On-Prem master header, row 8; Cloud is the same minus one control column so Cloud's indexes are one lower in the first block)

| Cols | Region | Content |
|---|---|---|
| 1–14 | Control checklist | Counter - Grand Total, Counter - per Year, License Category, If Hybrid - License Class, Verified with Commission Report, Verified with SF, With Amortization Schedule, Invoice - Rev Coding (Tin, Wincy), SO Memo Rev coding (Marilou), SO Matched with Celoxis / Missing SO per Celoxis to enter in QB, SO - GL / OS and CS on - Subscription dates entered / Number of months, Posted in QB, (Cloud: Match with Summary of Capitalized Services), Q1/2026 Review of Deferred Rev Balance |
| 15–37 | Contract descriptors | Customer Number, Customer Name (Contract) / Contract Name, Product, Contract Signed Date, New/Existing, AE, Payment Terms, Reseller, Subscription Period, Subscription Renewal Date, Status, Anniversary Month, TCV - Net New, TCV - Existing, TCV - NN & Existing, Term in yrs, ARR - Annual Recurring Revenue, Monthly Subscription Revenue / Recurring Revenue, Renewal Status, GLive Status (Cloud: "GLive Status, Estimated GL Date"), two "NOT APPLICABLE (LEAVE BLANK)" helper columns, TRANSACTIONAL VOLUME |
| 38–47 | **ARR by year** | ARR - 2020 … ARR - 2028 (one column per calendar year; header spelling varies: "ARR -2024", "ARR  - 2025"), then **CARR TODATE** |
| 48–75 | Billing | Per billing year: Date Invoiced, Amount, "12/31/YYYY-GL#1215.3 Deferred Billing" (Cloud: GL#1215.4); then 2026 month columns, Recent Invoice, Period (Y1 / Y2 / Y3 …), Sales Person, Review by Tin |
| 76–220 | **Revenue recognition schedule** | "GL#2320 Deferred Revenue,Beg"; then per year: 12 month columns (dated headers), Q1–Q4 subtotals, "YTD GAAP Revenue - YYYY", "New Accounts added this year YYYY", "GL#2320 Deferred Revenue 12/31/YYYY" (Cloud: GL 2340), from 2023 also AR-Deferred Billing / ST Deferred Billing / LT Deferred Billing; 2026 adds "Ending Balance GL#2320, MM/DD/2026" after each month, and ends with Deferred Revenue 12/31/2026, GL 12/31/2026, **Difference** |

---

## 4. Field dictionary (contract rows)

Values in the "Seen" column are what the live sheet contains today (165 On-Prem rows, ~46 Cloud rows). "Dashboard"
says whether the dashboard reads the column and how.

### 4.1 Identity and lineage

| Header | Meaning | Seen | Dashboard |
|---|---|---|---|
| **Customer Number** | QuickBooks customer number (80xxx, see Masterlist). **The key**: the same number on rows in A/B/C (initial contracts), D (renewals) and E (terminations) is one customer; also the join to the maintenance workbook and across the On-Prem / Cloud tabs. | integers 80001–80290 | Required. Lineage, cohorts, churn, dedupe |
| Customer Name (Contract) / Contract Name | Legal name on the contract | free text, upper case | Display only |
| Product | `On Prem` / `Cloud` (always matches the tab) | 2 values | Not used (the tab is the source) |
| License Category | `On Premise` / `Hybrid` (Cloud tab: `Cloud` …) | 2 values | No |
| If Hybrid - License Class | which licences a hybrid customer holds | `Perpetual/On Prem`, `On-Premise/Cloud`, `On Prem/Cloud`, `Cloud`, typos | No |
| New/Existing | Why the row exists: `New Account` (net new), `Existing` / `Existing Account` (renewal or upsell of a subscriber), `Converted` / `Converted P-C` / `Converted P-OP&C` (perpetual → subscription), `Hybrid`, `Amendment`, `Renewal`, `Plant Location`, `HazMat`, `New Account - see cloud`, `New Account - US` | 16 spellings incl. `Exisiting`, `Coverted` | A label containing "convert" (or "coverted") marks the customer as **converted** (see §5.6) |
| AE | Account executive initials | `AC`, `JG`, `CR` | No |
| Sales Person | Same as AE, in the billing region | | No |
| Reseller | Partner the deal came through (commission applies: NTT 10 %, Mehrwerk 7.5 %) | `n/a` / `None` / `NTT` / `Netgo` / `Mehrwerk` | No |

### 4.2 Term and status

| Header | Meaning | Seen | Dashboard |
|---|---|---|---|
| **Subscription Period** | The term as text `MM/DD/YYYY-MM/DD/YYYY`. A `Rev ` prefix means a revised term. | `05/01/2020-04/30/2023`, `Rev 01/01/2023-12/31/2025`; bad values `pending`, `*Subscription Period - Subscription Star…`, `01/01/2026-06/31/2026` (invalid day) | Required. Parsed to period_start / period_end (tolerates `Rev`, `//`, spaces, 2-digit years, clamps invalid days). Unparsable → the row is noted and the Renewal Date is used as the end. Drives ARR by month, renewals due, follow-on detection, term length |
| Subscription Renewal Date | Day after the term ends (= next start) | date | Fallback end date; shown in grids |
| Contract Signed Date | Signature date (section E On-Prem: **Date Cancelled**) | date | E rows: cancellation date |
| Term in yrs | Term length | `3` (101 rows), `5`, `2`, `1`, `0.5`, fractions, `n/a` | No (derived from the period instead: median term 1,096 days) |
| Anniversary Month | Billing anniversary; `May-upfront` when paid upfront | month names | No |
| Payment Terms | `Annual`, `Upfront 3 yrs`, `Upfront`, `Annual (NTT)`, `Annual - Quarterly`, mixes | 10 spellings | No |
| **Status** | Row state: `Active` (term running), `Closed` (term finished, superseded by a renewal row or ended), `Churn` | 4 values (`closed` lower-case once) | "churn" / "terminat" / "cancel" text flags the row (see §5.5) |
| **Renewal Status** | What happened or will happen at the end of the term: `Active`, `Renewed`, `Renewed 2025`, `For renewal`, `Not due for renewal`, `See 2026`, `Churn`, `Churn-in collection`, `Terminated`, `Final Period`, `Cancelled MM/DD/YYYY`, `Amended`, `Invoiced but pending doc`, `Terminated, not a subscription` | 15 values | Churn flag text; `Cancelled <date>` gives the cancel date; `not a subsc…` excludes the row entirely |
| GLive Status | Go-live date, or `Not Live (Neha)` | dates / text | No |
| Review by Tin | Free-text next action: `Renewed`, `To invoice Y3 - 8/1/2027`, `For renewal - 7/1/2027`, `Terminated` | | No |
| Period (billing region) | Which contract year is being billed: `Y1`, `Y2`, `Y3`, `Y1, Y2, Y3 - Upfront 3yrs`, `Y1 - 50%` | | No |

### 4.3 Money

| Header | Meaning | Seen | Dashboard |
|---|---|---|---|
| TCV - Net New | Total contract value when the row is net new (section A) | number | No |
| TCV - Existing | TCV when the row is a renewal / upsell / conversion | number | No |
| TCV - NN & Existing | The one that applies (= TCV) | number; `n/a` on E rows | No |
| **ARR - Annual Recurring Revenue** | The contract's annual value = TCV ÷ term years (fractional values such as 61,762.67 come from a 3-year TCV ÷ 3; 4,931.14 from a partial co-terminus term) | number; `0` on signed-not-started rows; **negative on E rows** | Required. The row's ARR; 0 → "not yet live" note |
| Monthly Subscription Revenue / Recurring Revenue | ARR ÷ 12; the straight-line monthly revenue used by the schedule columns | number | No |
| **ARR - YYYY** (2020 … 2028) | The ARR that this row contributes to each **calendar year**: the full ARR in every year the term touches, blank / 0 in years it does not. Not prorated (a May-2020 start shows the full ARR under 2020). Future years (2027, 2028) are filled for live multi-year terms. On E rows the mirror values are negative (or sometimes positive). | number / blank / 0 | **Σ over sections A–D of both tabs = ARR by year** (plus the maintenance workbook's GRAND TOTAL). E rows' values are ignored. Churn cohorts key on these columns per customer |
| **CARR TODATE** | Contracted ARR to date for the row (blank / 0 on older rows; = ARR on live rows) | number | A row with ARR blank/0 and CARR TODATE > 0 (outside E) is **signed, not started**: counted in CARR, not ARR (0 such rows today; 2 rows have ARR 0 and CARR 0) |
| NOT APPLICABLE (LEAVE BLANK) ×2 | Despite the header, D rows hold the **previous ARR** (negative) and the **change at renewal** (new ARR − previous, negative when it is a decrease) | e.g. `-61762.67`, `-5558.67` | No |
| TRANSACTIONAL VOLUME | Annual shipment volume for volume-priced deals | number / `n/a` | No |
| Date Invoiced / Amount (per billing year) | Invoice date and amount billed for that contract year | | No |
| 12/31/YYYY-GL#1215.3 Deferred Billing | Billed-not-yet-recognised balance at year-end (Cloud: GL 1215.4) | | No |
| Monthly columns (2020-01-01 …) | Revenue recognised that month (= Monthly Subscription Revenue while the term runs) | | No |
| Q1-YYYY … Q4-YYYY, YTD GAAP Revenue - YYYY | Quarter and year sums of the monthly columns; the year sum ties to GL 4100 / the P&L subscription line | | No |
| New Accounts added this year YYYY | TCV of contracts signed in the year (feeds deferred revenue roll-forward) | | No |
| GL#2320 Deferred Revenue … / Ending Balance GL#2320 / GL 12/31/2026 / Difference | Deferred revenue roll-forward per contract, the GL balance, and the tie-out difference (should be ~0) | | No |

### 4.4 Control checklist

`Counter - Grand Total`, `Counter - per Year` (1 per row; summed by the Total Year rows), `Verified with Commission
Report` (Yes / not in commission / To check), `Verified with SF` (Yes / **mismatched** on 40 rows / To check), `With
Amortization Schedule` (Yes / Missing / see 2026), `Invoice - Rev Coding`, `SO Memo Rev coding`, `SO Matched with
Celoxis`, `SO - GL …` (Done / Done-Tin / Same PID with Perpetual), `Posted in QB`, `Q1/2026 Review of Deferred Rev
Balance`. These are the accounting close controls per contract; a UI should present them as a checklist with the
responsible person from row 1.

---

## 5. Rules the dashboard applies (what the numbers mean)

### 5.1 ARR by year
`ARR(year) = Σ "ARR - year" over contract rows in sections A, B, C, D of both tabs + maintenance GRAND TOTAL(year)`.
A row whose Renewal Status contains "not a subsc" (one-off fees booked here, e.g. temporary hosting) is excluded from
everything. Section E rows are churn markers only; their amounts are never summed.

### 5.2 CARR (contracted ARR)
`CARR = ARR + Σ CARR TODATE of rows (A–D) whose ARR is blank or 0 and CARR TODATE > 0` — signed contracts that have
not started billing. Prior years show CARR = ARR.

### 5.3 ARR by month
A contract is live on a month-end when `period_start ≤ month-end ≤ period_end`, the row is not in E, ARR > 0, and the
customer was not cancelled before that date (cancel date from E: Date Cancelled, else "Cancelled MM/DD/YY" in Renewal
Status, else the E row's period end). Month-end ARR = Σ live rows' ARR + the year's maintenance ARR. This is why a
live row with a **blank "ARR - <current year>" cell** is reported: ARR by year misses it until the cell is filled.

### 5.4 Renewals due and estimates
A row is **due for renewal** when its period_end falls in the window and **no follow-on row exists**: a follow-on is
any other row of the same Customer Number on the same tab whose period_start ≥ period_end − 31 days. For the current
year, a due row without a follow-on and with a blank current-year ARR cell is **estimated**: the prior year's ARR is
carried forward and flagged (never for maintenance). A due row whose current-year cell is already filled is "renewal
pending". Rows whose Status / Renewal Status say churn / terminated / final period / not renewing / cancelled are never
estimated.

### 5.5 Churn, GRR, NRR (cohort per customer number)
Cohort for year Y = customers with effective ARR at the end of Y−1, keyed by Customer Number across On-Prem, Cloud and
maintenance. Effective ARR(Y) = subscription ARR(Y) + maintenance ARR(Y), except a **converted** customer whose
subscription has started counts only the subscription (the prepaid maintenance tail is not a downgrade).

- **Churned**: effective ARR goes to 0 → logo churn. Split by type: *subscription* (started on subscription, stopped),
  *conversion* (maintenance → subscription → stopped), *maintenance* (never subscribed, maintenance stopped).
- **Partial churn**: one line (subscription or maintenance) stops, the other continues; counted in churn $, not logos.
- **Expansion / downgrade / flat**: effective ARR up / down / same.
- **Gap**: ARR blank in Y but present later → sheet gap, held at the prior value, listed in notes, not churn.
- **Pending**: a row covers Y (period includes 12/31/Y, not flagged) but the Y cell is empty → held, not churn.
- A row flagged churn / terminated that still has ARR in a year is **retained that year** and churns when the column
  goes blank (the notes announce the expected churn year).
- GRR = (start − churned − downgrades) ÷ start; NRR = end ÷ start; logo churn = churned customers ÷ cohort.

### 5.6 Conversion detection
A customer is **converted** (perpetual maintenance → subscription) when any of its New/Existing labels contains
"convert" / "coverted", or when the maintenance workbook shows maintenance ARR in a year and subscription ARR starts in
the same or a later year while maintenance stops (that year or the next). Under the On-Prem / Cloud source filter a
maintenance → Cloud conversion is a maintenance churn on On-Prem and a new logo on Cloud (by design).

### 5.7 New customers and new logos
- **New customers (CAC denominator)** = section A `Total Year YYYY` subtotals of "Counter - per Year", both tabs.
  Sections B, C, D are never new customers.
- **New logos (ARR bridge)** = customers with no effective ARR at the prior year-end and ARR in the year (includes
  re-activations); a converted customer is not a new logo.

### 5.8 Row hygiene
Duplicate rows (same source, section, Customer Number, period text and ARR) are ignored after the first. Fractional
residues under $1 are treated as 0.

---

## 6. Scenarios, as the sheet records them

Customer numbers are real; names are omitted on purpose.

### 6.1 Net-new subscription (section A)
One row in A: `New/Existing = New Account`, `TCV - Net New` filled, `Term in yrs`, ARR = TCV ÷ years, ARR-YYYY filled
for every year the term touches, `CARR TODATE` = ARR on recent rows, `Counter - per Year = 1`. The row's signing year
is counted in the `Total Year YYYY` subtotal.
Example #80072: A row, term 05/01/2020–04/30/2023, TCV 185,288, ARR 61,762.67, ARR-2020 / 2021 / 2022 = 61,762.67,
Payment Terms "Upfront 3 yrs", Status Closed, Renewal Status Renewed (because a D row followed).

### 6.2 Renewal with uptick (section D)
A new row in **D** with the same Customer Number, `New/Existing = Existing`, `TCV - Existing`, the next period starting
the day after the old end, ARR-YYYY filled for the new years. The old row keeps its values for its own years, its
`Status` becomes `Closed` and its `Renewal Status` becomes `Renewed`. The two helper columns on the D row hold the
previous ARR (negative) and the change. The section title records partner commissions on renewals (NTT 10 %, Mehrwerk
7.5 %).
Example #80072: D row 05/01/2023–04/30/2026 ARR 56,204 (a **decrease** from 61,763), then a second D row
05/01/2026–04/30/2029 ARR 58,452.16 (uplift of 2,248, +4 %). On the live sheet renewals are 25 uplifts, 6 flat,
3 decreases.
Dashboard effect: the customer's ARR by year is continuous (no churn), the change shows as expansion / downgrade in the
cohort bridge; the renewal counts as retained in GRR.

### 6.3 Renewal not yet signed
The old row stays `Status = Active`, `Renewal Status = For renewal` / `See 2026` / `Not due for renewal`, no D row yet.
The dashboard lists it in the renewals pipeline (next 12 months, no follow-on) and, in the current year, carries the
prior ARR forward as an **estimate** until the D row and the year cell exist. `Review by Tin` carries the planned date
("For renewal - 7/1/2027").

### 6.4 Conversion, perpetual licence → subscription (section B)
A customer who owned a perpetual licence (and paid maintenance in the maintenance workbook) signs a subscription. The
row goes to **B** with `New/Existing = Converted` (variants: `Converted P-C` = perpetual → Cloud, `Converted P-OP&C` =
perpetual → On-Prem + Cloud, `Hybrid`, or plain `Existing`). `License Category` may be `Hybrid` with `If Hybrid - License
Class = Perpetual/On Prem` when the perpetual licence is kept alongside. The maintenance line stops (explicit 0 or blank
in the next maintenance year). Later renewals of the converted contract go to D like any other.
Examples: #80008 (Cloud B 10/2025–09/2026 ARR 30,000, renewed in D at 32,700), #80096 (Cloud B 09/2024–09/2027 ARR
30,000), #80131 (On-Prem B 55,000 "Converted P-OP&C" plus a Cloud C row 10,000 "Converted P-C", same customer on both
tabs).
Dashboard effect: not a new customer (CAC), not churn of the maintenance (conversion rule §5.5), subscription replaces
maintenance as the customer's effective ARR; counted in the conversion tracker; under the Cloud source filter it is a
Cloud new logo.

### 6.5 Upsell, additional modules / plants (section C)
An existing subscriber adds modules, plants, users or a product line. The row goes to **C** with `New/Existing =
Existing Account` (or `Plant Location`, `HazMat`, `Amendment`), its own period (often **co-terminus** with the base
contract, so a fractional term such as 1.67 years and a fractional ARR), its own ARR-YYYY. A customer can have several
C rows; at renewal the base and the upsells are usually rolled into one D row.
Example #80197: A row 04/2023–04/2026 ARR 67,600; C rows 11/2024–10/2026 8,400, 05/2025–04/2026 16,800,
10/2025–03/2026 16,800; then one D row 10/2026–03/2029 ARR 133,625 that replaces all of them.
Example #80172: A 24,980 + two C rows (4,931 and 7,545, co-terminus to 03/31/2025) → one D row 40,813 from 04/2025.
Dashboard effect: expansion in the cohort bridge; the Σ of the customer's rows per year is its ARR; never a new logo.

### 6.6 Amendment mid-term
`Renewal Status = Amended` or `New/Existing = Amendment`; a `Rev MM/DD/YYYY-MM/DD/YYYY` period marks a revised term.
Accounting keeps the original row (closed) and adds the amended row in C or D. For the dashboard an amended row is just
another row of the customer; make sure the old row's ARR-YYYY cells are blanked for the years the new row covers,
otherwise the customer is double-counted.

### 6.7 Termination / churn (section E)
Two things happen: the original A/B/C/D row is flagged (`Status = Churn` and/or `Renewal Status = Churn`,
`Terminated`, `Final Period`, `Churn-in collection`, `Cancelled MM/DD/YYYY`), and a **mirror row is added in E** with
the same Customer Number, the same period, `ARR` **negative** (−35,042), `Date Cancelled` (On-Prem; Cloud keeps the
signed-date header), TCV / Term `n/a`, and the ARR-YYYY columns mirrored (negative on most rows; some E rows are
positive, e.g. #80214 — the dashboard ignores E amounts, so the sign does not matter to it but the UI should
normalise it).
Example #80168: A row 2022–2024 ARR 38,430 (Renewal Status Terminated), D row 2025 ARR 35,042 (Status Churn, "Final
Period"), D row 01/01/2026–06/31/2026 (sic) 35,042 "Final Period" with no 2026 ARR cell, E row for the same half-year
with ARR −35,042 and "Cancelled 6/31/2026".
Dashboard effect: the customer is retained for every year that still has ARR in the columns and churns in the first
blank year (expected churn year is announced in the notes); the cancel date drops the customer out of ARR by month;
churn type is *subscription* (or *conversion* if the customer had converted). A row flagged churn is never estimated.

### 6.8 Partial churn
A customer with a subscription line and a maintenance line loses one and keeps the other (or a hybrid customer drops the
Cloud contract and keeps On-Prem). Counted in churn $ by type, not as a lost logo.

### 6.9 Not a subscription (one-off fee recorded here)
`Renewal Status = "Terminated, not a subscription"` (#80148: a 10-month 25,000 hosting fee, License Category Hybrid).
Excluded from ARR, CARR and churn entirely; listed in the dashboard's data notes. The UI should probably keep such rows
out of the register or tag them explicitly.

### 6.10 Signed, not started
A row with the term in the future or billing not yet begun: `ARR - Annual Recurring Revenue = 0`, year cells 0,
`GLive Status = Not Live`, `Renewal Status = See 2026`, and either `CARR TODATE` > 0 (→ counted in CARR) or 0 (→ noted
as "ARR is 0, contract not yet live or not entered"). Example #80275: 5-year term from 01/05/2026, TCV 571,340,
ARR 0, CARR TODATE 0 today.

### 6.11 Reseller / partner contracts
`Reseller = NTT / Netgo / Mehrwerk`, `Payment Terms = Annual (NTT)`, `Verified with Commission Report` applies with the
partner's rate (section D title). No dashboard effect.

### 6.12 Multi-year upfront vs annual billing
`Payment Terms` (Upfront 3 yrs vs Annual), `Anniversary Month` ("May-upfront"), one `Date Invoiced / Amount` pair per
billing year, `Period` = Y1 / Y2 / Y3 billed so far, `Review by Tin` = "To invoice Y3 - 8/1/2027". ARR is the same
either way; only the billing and deferred-billing columns differ.

### 6.13 Same customer on both tabs
Five customer numbers have rows on both On-Prem and Cloud (e.g. #80131, #80172, #80197, #80230 "New Account - see
cloud"). Under "All" sources the lines are summed per customer; under one source only that tab counts, so a customer
can be a churn on one side and flat on the other.

### 6.14 Short or odd terms
Terms of 0.5, 0.75, 0.83 years exist (bridge periods, pilots, co-terminus stubs). ARR is still TCV annualised; the year
cells carry the full ARR for the calendar year(s) touched.

---

## 7. Data-quality issues on the live sheet (validation targets for the UI)

- Header spelling drift: `Cutomer Number`, `ARR -2024`, `ARR  - 2025`, section-E year headers `ARR 12/31/2020`,
  `ARR TODATE - 2025`, Cloud `Contract Name` vs On-Prem `Customer Name (Contract)`.
- `New/Existing` has 16 spellings for ~6 meanings (`Exisiting`, `Coverted`, `Existing` vs `Existing Account`).
- `Status` has `closed` and `Closed`; `Status` and `Renewal Status` can disagree (Status Active with Renewal Status
  Churn on #80214).
- Dates inside text: `Cancelled 6/31/2026` (invalid day), period `01/01/2026-06/31/2026`.
- Placeholder periods: `pending` (On-Prem row 196), `*Subscription Period - Subscription Start…` (On-Prem row 68).
- Live rows whose current-year ARR cell is blank (ARR by year undercounts until filled).
- `Counter - per Year` = 0 on two contract rows; `Total Year` subtotals must equal the block's sum (the dashboard warns
  and uses the subtotal).
- `Verified with SF = mismatched` on 40 rows; `With Amortization Schedule = Missing` on 14.
- E-row amounts inconsistent in sign; TCV / Term `n/a` on E rows.
- `NOT APPLICABLE (LEAVE BLANK)` columns are in fact used (prior ARR and delta on renewals).
- `SF Recon` tab shows `#VALUE!`.
- Fractional ARR (TCV ÷ 3) and float residues (24,979.666…); ARR-YYYY cells are copies of ARR, not formulas, so a
  changed ARR must be re-copied into every year cell.

---

## 8. Recommendations for a maintenance UI

Model the sheet as **customers → contracts (rows) → yearly ARR cells**, with the section as a row attribute:

- **Enums** (store canonical values, write the sheet's spelling): section A–E; `New/Existing` ∈ {New Account,
  Existing, Converted, Hybrid, Amendment, Plant Location}; `Status` ∈ {Active, Closed, Churn}; `Renewal Status` ∈
  {Active, Renewed, For renewal, Not due for renewal, See <year>, Amended, Final Period, Churn, Churn-in collection,
  Terminated, Cancelled <date>, Invoiced but pending doc, Terminated – not a subscription}; `Payment Terms`; `Reseller`;
  `License Category`.
- **Derived, not typed**: period_start / period_end (write back as `MM/DD/YYYY-MM/DD/YYYY`), Subscription Renewal Date
  = end + 1 day, Term in yrs = days ÷ 365, ARR = TCV ÷ term years, Monthly = ARR ÷ 12, ARR-YYYY = ARR for every
  calendar year in [start, end] else blank, CARR TODATE = ARR once signed, Anniversary Month = start month.
- **Workflows that create rows**: New contract (A), Renew (closes the old row: Status Closed + Renewal Status Renewed;
  adds a D row with the prior ARR / delta helpers), Convert (B, needs the maintenance workbook customer; stops the
  maintenance line), Upsell (C, co-terminus option), Amend (Rev period, blank the superseded years), Terminate (flags
  the live row, adds the negative E mirror with Date Cancelled, blanks future year cells).
- **Validations**: unique (section, Customer Number, period); no overlapping live terms per customer unless an upsell;
  Σ year cells of a customer per year = the customer's ARR; a D row must follow a row of the same customer ending
  within 31 days of its start; the signing year's Total Year subtotal = count of A rows; churn-flagged rows must have an
  E mirror; Counter - per Year = 1; valid dates; current-year cell filled for live rows.
- **Compatibility with the dashboard** (if the UI writes the workbook): keep the section title rows (`A. …` in column
  A), a `Counter…` header row per section, the header texts listed in §4 (new columns may be added anywhere; order does
  not matter), the `Total Year YYYY` subtotal rows in section A with the count in "Counter - per Year", 80xxx Customer
  Numbers, and the `MM/DD/YYYY-MM/DD/YYYY` period text. Year columns must keep a header matching `ARR … YYYY`.

---

## 9. Headline numbers on the live sheet (2026-11-11, for sanity checks)

| | On-Prem | Cloud |
|---|---|---|
| A net new rows | 80 | 27 |
| B conversions | 17 | 3 |
| C upsells | 32 | 5 |
| D renewals | 29 | 5 |
| E terminated | 6 | 6 |
| As of (row 5) | 07/31/2026 | 04/30/2026 |

Parsed contract rows 210 (one excluded as "not a subscription", two with unparsable periods, two with ARR 0). Year
columns 2020–2028. Median term 1,096 days (3 years); 184 of 208 terms are longer than 400 days. Renewals on the sheet:
25 uplifts, 6 flat, 3 decreases. 27 customers have an A row followed by a D row; 19 have a B row; 27 have a C row;
12 have an E row; 5 customer numbers appear on both tabs.
