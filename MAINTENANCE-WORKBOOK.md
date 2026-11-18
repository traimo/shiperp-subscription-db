# ShipERP Deferred Revenue Maintenance workbook — structure, fields, rules and scenarios

Written 2026-11-12 from the live workbook and from the dashboard code that reads it. Companion to
`SUBSCRIPTION-SUMMARY-WORKBOOK.md`; self-contained so a separate project (an accounting maintenance UI) can start from
it. Where the dashboard's reading rules matter for compatibility they are spelled out.

Workbook: `6. 2026 GL4300 & GL2300 Deferred Revenue Maintenance Fee Amortization_Updated_Adv Billing.xlsx`
(SharePoint; the dashboard pulls it by the pattern `*Deferred Revenue Maintenance*.xlsx`). Owner: ERP Integrated
Solutions LLC (ShipERP) accounting. GL 4300 = Software Maintenance revenue, GL 2300 / 2310 = deferred maintenance
revenue.

---

## 1. What the workbook is for

It is accounting's **register of every perpetual-licence customer and the annual maintenance fee each one pays**,
plus the straight-line amortization of that fee into monthly GL 4300 revenue and the deferred balance (GL 2310). It
does five jobs:

1. **Maintenance ARR by contract year** — one column per maintenance year ("2015-2016" … "2026-2027"); the GRAND
   TOTAL row is the company's maintenance ARR, which the dashboard adds to subscription ARR for the ARR tile and uses,
   per customer, for churn / GRR / NRR and conversion tracking.
2. **Revenue recognition** — monthly columns from January 2020 spread each year's fee over the twelve months from the
   billing anniversary; "YYYY Total Revenue" (GL 4300) and "12/31/YYYY Deferred" (GL 2310) per customer tie to the GL.
3. **Licence history** — original perpetual licence price, discount, additional module / plant / carrier purchases
   with go-live dates, Total Current License; the base the maintenance percentage is applied to.
4. **Maintenance pricing** — Original Maintenance, Additional Maintenance, the cumulative 4 % annual increase, Total
   Maintenance (the current fee), Maintenance % of licence value (typically 20–28 %), and the support-hours budget
   derived from the fee.
5. **Customer population** — the same tab also lists every subscription customer (On-Prem, Cloud, converted) and
   every closed account, so the "Summary Customer Count" block (133 + 73 + 11 + 21 + 2 = 240) is the company's full
   customer list and converted / churned customers keep their maintenance history.

The **advance-billing tabs** list the QuickBooks maintenance invoices (item `1000-MT`) raised in December and in
January–April for the following maintenance year; "Adj Advance Billing" in the main tab's name means the schedule is
adjusted for those invoices billed before the period they cover.

Subscription ARR is **not** here; it is in `2. ShipERP Subscription Summary_On-Premise and Cloud_ASC606.xlsx`. The two
join on **Customer Number** (the 80xxx QuickBooks number).

---

## 2. Tabs

| Tab | Role | Read by the dashboard |
|---|---|---|
| `Maintenance-Adj Advance Billing` | **The register and schedule** (343 rows × 166 columns). | Yes (the only tab read) |
| `Maintenance` | Earlier copy of the same layout without the advance-billing adjustment; its GRAND TOTAL differs from 2023-2024 on (e.g. 2026-2027: 3,298,413 vs 2,756,957). Historical; do not use. | No |
| `12-2023 / 12-2024 / 12-2025 Advance Billing` | QuickBooks invoice lists for December: Customer, Num (invoice no., `RI` suffix = reseller invoice), Date, Memo (`1000-MT`), Due Date, Aging, Amount. | No |
| `Addition 1-2026 to 04-2026` | Same list for January–April 2026 invoices. | No |
| `Summary` | "Summary of Maintenance Fees as of 12/31/2025": accounts and annual revenue per group (live 133 / not live / converted / closed), adjustments for conversions and closures, projected 2026 decrease (5 %) and increase (4 %), projected maintenance revenue, monthly accrual (259,000). | No |
| `Formula - Onpremise and Cloud`, `Formula - ADB Safegate` | Worked examples of the pricing formula: licence value × maintenance % (22 % / 26 %) = annual maintenance; subscription = (licence + maintenance) ÷ term; support-hours budget = maintenance ÷ hourly rate. | No |

---

## 3. Layout of `Maintenance-Adj Advance Billing`

```
row 1     header row (the ONLY header row; 155 non-blank headers)
row 2     "A. PERPETUAL CUSTOMERS - LIVE 133"              <- section title in column A
row 3..   customer rows (Customer Count, Customer, Customer Name, ...)
row 136-7 add-on rows without a Customer Count (see §7)
row 139   "TOTAL LIVE"                                      <- subtotal
row 141   "B. NOT LIVE, BUT PAYING MAINTENANCE"  (1 customer) + subtotal row 144
row 146   "NOT LIVE - NET NEW NOT PAYING MAINTENANCE" (3)   + subtotal row 151
row 153   "SUBSCRIPTION (ON-PREMISE) - 73"
row 228   "UPSELL(PERPETUAL TO SUBSCRIPTION (ON-PREM)) - 11"
row 246   "SUBSCRIPTION (CLOUD) - 21"
row 271   "UPSELL (PERPETUAL TO SUBSCRIPTION(CLOUD)) - 2"
row 277   "OTHERS" (two partner rows without customer numbers)
row 282   "SUBSCRIPTION CUSTOMERS AND CONVERTED CUSTOMERS"  <- subtotal of rows 153-279
row 284   "CLOSED ACCOUNTS/TERMINATED/CHURN - 29"
row 316   subtotal of the closed block
row 318   "GRAND TOTAL"                                     <- the maintenance ARR per year
row 328   "Summary Customer Count": Perpetual 133, On-Premise 73, Perpetual-Subscription (on-prem) 11, Cloud 21,
          Perpetual-Subscription (cloud) 2, total 240
```

Reading rules (the dashboard's; keep them if the UI writes the sheet):

- The **header row** is the first row (within the first 10) that carries at least three `YYYY-YYYY` labels. Every
  such label is a **year column**; the dashboard keys it by its **END year** ("2025-2026" → 2026, configurable to
  start) and keeps years ≥ 2020 ("2019-2020" is the first).
- Columns are found **by header text only**: `Customer Count` (the label / counter column), `Customer` (or
  `Customer Number`), `Customer Name`. Never by column letter.
- A **customer row** needs a numeric Customer Count, a numeric Customer Number and a name. Rows missing any of the
  three (section titles, subtotals, the OTHERS partner rows, and the two add-on rows in §7) are skipped.
- A **section** is a row whose counter cell matches `^[A-Z]\.\s` — so only "A. PERPETUAL …" and "B. NOT LIVE …" are
  recognised today; every row after row 141 is tagged with section B. The section tag is informational only (no
  metric depends on it), but a UI that writes lettered titles for the later blocks would make them visible.
- **GRAND TOTAL** = the last row whose first cells read `GRAND TOTAL` (case-insensitive); its year cells are the
  maintenance ARR per year. The dashboard uses this row for the ARR tile and the per-customer rows for churn, and
  reconciles the two on screen (the difference is the rows the parser skips, §7).
- A year cell that is **text** (`n/a`, a partner name) reads as empty; **0** is an explicit zero; **blank** is "not
  entered yet". Maintenance values are **never carried forward or estimated** (Tony's rule; only subscriptions are).
- One customer number used by two different companies is flagged in Data notes (none today).

### Column regions (header row 1)

| Cols | Region | Headers |
|---|---|---|
| 1–8 | Identity | Customer Count, Customer, Customer Name, Product, Year, Contract Month, Go Live Date, Billing Anniversary |
| 10–25 | Licence history | Original before discount, Discount, Original net of discount; Addl Purchase - Module + Go Live Date + Amount; Addl Purchase - Plant + Plant Qty (excluding Core) + Go Live Date + Amount; Total Addl Purchase; Purchase - Carrier + Go-Live + Purchase Amount; **Total Current License** |
| 27–31 | Maintenance pricing | Original Maintenance, Additional Maintenance, 4% increase, **Total Maintenance**, Maintenance % |
| 32–43 | **Maintenance ARR by contract year** | 2015-2016 … 2026-2027 |
| 44–46 | Budget | Basis for Budget, Monthly Revenue, 2025-2026 Budget Hours |
| 48–166 | **Revenue schedule** | per calendar year 2020–2027: twelve month columns (dated headers), "YYYY Total Revenue" (from 2023 "GL4300_YYYY Total Revenue"), "12/31/YYYY Deferred" (from 2023 "GL2310_12/31/YYYY Deferred") |

---

## 4. Field dictionary (customer rows)

276 customer-like rows today (267 parsed by the dashboard). "Seen" = values on the live sheet.

### 4.1 Identity

| Header | Meaning | Seen | Dashboard |
|---|---|---|---|
| **Customer Count** | Running counter within the block (1 … 133 in A). Blank on add-on rows. | integers; blank | Required: the row is skipped without it |
| **Customer** | QuickBooks customer number, the key to the Subscription Summary and to churn | 80002–80290 | Required |
| Customer Name | Company; partner-billed accounts read "Netgo (…)" / "NTT (…)" | free text | Display |
| Product | Licence type: `Perpetual`, `On-Premise` (subscription), `Cloud` (subscription). **On closed rows the cell holds the termination note instead** ("04/15/2026 - Terminated per …", "Old unable to retrieve date and reason", "Inactive 06/05/2023 …"). On two add-on rows: `Addl Purchase - SO`, `Addl Plant`. | 136 / 84 / 25 + notes | No |
| Year | Year the licence contract was signed | 2013–2025 | No |
| Contract Month | Contract date | date | No |
| Go Live Date | Date, or `Not Live` (65 rows), or free text ("Live per … 12/13/2025", "10/1/2018, EU 02/16/2025") | | No |
| Billing Anniversary | Month the annual maintenance invoice is raised and the maintenance year starts: month name, `January - Calendar` (calendar-year billing), suffix `(Converted)` once the customer moved to subscription, `not applicable` on subscription-only rows | 106 n/a, 32 January - Calendar, months | No (but it explains the monthly schedule steps) |

### 4.2 Licence history and pricing

| Header | Meaning | Dashboard |
|---|---|---|
| Original before discount / Discount / Original, net of discount | Perpetual licence list price, discount (negative), net | No |
| Addl Purchase - Module, Go Live Date, Additional Purchase - Amount | Modules bought later (EWM, Hazmat, SO, AES, Planning, QuickPak, DP, TR…) | No |
| Addl Purchase - Plant, Plant Qty (excluding Core), Go Live Date, Additional Purchase - Amount | Additional plants / sites and their price | No |
| Total Addl Purchase | Σ module + plant purchases | No |
| Purchase - Carrier, Go - Live, Purchase Amount | Carrier integrations bought (AFS, LC Express, Penske…) | No |
| **Total Current License** | Net licence + all additional purchases = the maintenance base | No |
| Original Maintenance | First-year maintenance fee (≈ 20–22 % of the net licence) | No |
| Additional Maintenance | Maintenance on later purchases; **negative when plants / modules were decommissioned** (e.g. −32,889) | No |
| 4% increase | Cumulative effect of the 4 % annual uplift applied each anniversary | No |
| **Total Maintenance** | Current annual fee = Original + Additional + 4 % increase; equals the newest year column | No |
| Maintenance % | Total Maintenance ÷ Total Current License (0.15–0.31; formula tabs use 22 % / 26 %) | No |
| Basis for Budget | The current-year fee used for the support budget (0 on stopped rows) | No |
| Monthly Revenue | Basis ÷ 12 | No |
| 2025-2026 Budget Hours | Support hours budgeted for the year, derived from the fee at an hourly rate (formula tabs) | No |

### 4.3 Maintenance ARR by contract year (the dashboard's input)

| Header | Meaning | Values | Dashboard |
|---|---|---|---|
| **YYYY-YYYY** (2015-2016 … 2026-2027) | The maintenance fee billed for the contract year that starts at the billing anniversary in the first year and ends before it in the second. The first year is **prorated** when billing started mid-year (1,168.75 then 24,310); each later year is the prior year × 1.04 unless re-priced. | number; `0` = explicitly not billed / stopped; blank = not yet entered; `n/a` = before the customer existed; occasionally text (`Mehrwerk`, `NTT`) | **Keyed by the END year.** Σ GRAND TOTAL(year) = maintenance ARR added to the ARR tile; per customer → effective ARR for churn. An explicit 0 in the **next** year's column (current year + 1) while the current year is > 0 means the line stopped now; blank is left alone |

### 4.4 Revenue schedule

| Header | Meaning |
|---|---|
| Month columns (2020-01-01 …) | Revenue recognised in the month = the fee of the maintenance year in force ÷ 12; the amount steps at the anniversary month (A.O. Smith: 2,017.81 through January 2026, 2,423.52 from February 2026) |
| YYYY Total Revenue / GL4300_YYYY Total Revenue | Calendar-year maintenance revenue per customer; Σ ties to GL 4300 (the P&L line "4300 Software Maintenance") |
| 12/31/YYYY Deferred / GL2310_12/31/YYYY Deferred | Unearned balance at year-end = months of the prepaid year not yet recognised; Σ ties to GL 2310 |
| Row 328+ block | Customer counts per group; the "Summary Customer Count" row also holds GL 2310 comparison figures in the 12/31/2024 and 12/31/2025 Deferred columns |

---

## 5. How the dashboard uses it

- **ARR tile / ARR by year** = subscription ARR (sections A–D of the Subscription Summary) + **GRAND TOTAL** of the
  maintenance year keyed to that calendar year (2026 = column "2025-2026" = 3,251,918).
- **ARR by month** adds the year's maintenance total as a flat amount to every month-end (maintenance is yearly on
  the sheet), so the monthly bridge shows the maintenance change as one January step.
- **Source filter**: maintenance has no On-Prem / Cloud split on the sheet and counts as **On-Prem**; under Cloud
  it is excluded.
- **Churn / GRR / NRR**: each customer number's maintenance value per year is one "line" next to its subscription
  line. Maintenance stopping with no subscription = **maintenance churn**; maintenance stopping while a subscription
  starts = **conversion** (not churn); a customer with both lines losing one = partial churn.
- **Conversion detection**: a customer is converted when its Subscription Summary label contains "convert", or when
  maintenance > 0 in some year and subscription ARR starts in that or a later year while maintenance is ≤ 0 that
  year or the next. In this app both lines count as ARR while they are billed (Hybrid account, 2026-11-18); the
  dashboard's cohort still replaces the maintenance by the subscription once it starts.
- **Conversion tracker** (Customers tab): maintenance-only customers and ARR remaining per year, conversions per year.
- **Reconciliation** (ARR bridge tab): walks from the tile (GRAND TOTAL) to the cohort basis: maintenance of
  converted customers, maintenance on rows the parser does not key to a customer, explicit-zero stops.
- **Data notes**: "maintenance for YYYY-YYYY is an explicit 0 … counted as stopped", duplicate customer numbers,
  missing tab / header / GRAND TOTAL.
- Configuration: `MAINT_FILE_PATTERN`, `MAINT_TAB` (default `Maintenance-Adj Advance Billing`), `MAINT_YEAR_KEY`
  (`end`), `MAINT_TOTAL_LABEL` (`GRAND TOTAL`), `FIRST_YEAR` (2020).

---

## 6. Scenarios, as the sheet records them

Customer numbers are real; names omitted.

### 6.1 New perpetual customer
A row in block A: licence price / discount / net, Original Maintenance (≈ 20–22 % of net), Billing Anniversary, Go Live
Date. The first maintenance-year cell is prorated from go-live or first invoice to the anniversary, then full years
follow. Example #80136: 2019-2020 = 0, 2020-2021 = 1,168.75 (stub), 2021-2022 = 24,310, then 4 % steps to 27,345.45
in 2026-2027. Customers whose maintenance starts only at go-live show `n/a` / blank until then.
Dashboard: maintenance ARR appears in the first keyed year with a value; the customer is a new logo that year.

### 6.2 Annual 4 % uplift
Each anniversary the fee rises 4 %: the next year column = prior × 1.04, "4% increase" accumulates the delta, Total
Maintenance = the newest column. On the live sheet 733 year-over-year steps are increases, 98 flat, 36 decreases.
Dashboard: expansion in the cohort bridge (maintenance line up).

### 6.3 Additional purchase (module / plant / carrier)
The purchase goes in the Addl Purchase columns with its own go-live and amount, Total Current License grows,
Additional Maintenance rises and the next maintenance year jumps more than 4 % (#80124: 15,456.80 → 25,056.80 after a
QuickPak module + 2 plants). Two customers (#80162, #80083) have the add-on on a **separate row without a Customer
Count** (rows 136–137) instead — see §7.
Dashboard: expansion.

### 6.4 Decommissioned plants / renegotiated fee
Additional Maintenance goes negative, Total Maintenance shrinks, the next year column drops (#80124: 25,056.80 →
21,067.67; #80052 Additional Maintenance −32,889 before closure). Dashboard: downgrade.

### 6.5 Not live but paying maintenance (block B)
Licence sold, not implemented, maintenance invoiced anyway (one customer, #80155: `Go Live Date = Not Live`, fees
from 2021-2022). Dashboard: ordinary maintenance ARR.

### 6.6 Not live, net new, not paying (block "NOT LIVE - NET NEW NOT PAYING MAINTENANCE")
Three recent perpetual sales (`Product = Perpetual`, `Go Live Date = Not Live`, "Upon go-live" notes) with empty year
cells; maintenance starts at go-live. Dashboard: no ARR yet.

### 6.7 Conversion, perpetual → subscription
The customer keeps its row (moved under "UPSELL (PERPETUAL TO SUBSCRIPTION …)" or left in A), Billing Anniversary gets
"(Converted)", the maintenance year in which the subscription starts is set to an **explicit 0** and later years stay
blank, Basis for Budget = 0. Its subscription appears in the Subscription Summary section B with the same number.
Example #80096: maintenance 18,600 → 21,759 through 2023-2024, 0 in 2024-2025; Cloud subscription 30,000 from
09/2024. Example #80008: maintenance 13,000 → 28,122 through 2023-2024, then 18,718 / 19,466 (a reduced maintenance
continues alongside the Cloud subscription for the hybrid part).
Dashboard: not churn; subscription replaces maintenance as effective ARR; counted in the conversion tracker; under the
Cloud source it is a Cloud new logo and an On-Prem maintenance churn.

### 6.8 Hybrid: subscription customer still paying some maintenance
Customers in the subscription blocks whose year cells carry values (e.g. a perpetual licence kept for one site, a
subscription for the rest). Both lines count as ARR (the Subscription Summary marks them `License Category = Hybrid`,
stored per customer; check X07 flags a Hybrid customer with a perpetual class and no maintenance billed); the
maintenance line is judged separately for partial churn.

### 6.9 Termination / churn (block "CLOSED ACCOUNTS/TERMINATED/CHURN")
The row is moved to the closed block, the Product cell receives the termination note and date, the first unbilled
maintenance year is an explicit 0, later years blank; history before that stays. Example #80052: 26,000 → 31,633 through
2024-2025, 0 in 2025-2026. Example #80060 (converted earlier, "January - Calendar (Converted)"): 35,867 in 2025-2026,
**0 in 2026-2027** = stops next year.
Dashboard: maintenance churn in the first keyed year with 0 / blank after a value; an explicit 0 in the *next* year
while the current year is paid means "stopped now" (listed in churn notes); blank next year = not billed yet, no churn.

### 6.10 Subscription-only customers (blocks ON-PREMISE 73, CLOUD 21)
Listed for the customer count with `Billing Anniversary = not applicable` and empty maintenance cells (100 rows with
no values at all). Their ARR is in the Subscription Summary. Dashboard: ignored here (no maintenance line).

### 6.11 Partner / reseller accounts
Names prefixed "Netgo (…)" / "NTT (…)"; invoices carry an `RI` suffix. The OTHERS block holds two partner rows
(Merhwerk, Blue Grace) with licence amounts but **no customer number** — not keyed to a customer by the dashboard.

### 6.12 Old accounts with no history
Closed rows with `Product = "Old unable to retrieve date and reason"` and no year values (#80089, #80116, #80046…):
kept for the record, no effect on any metric.

---

## 7. Data-quality issues on the live sheet (validation targets for the UI)

- **Rows without a Customer Count are skipped** by the dashboard although they carry a Customer Number: the add-on
  rows for #80162 (4,211 in 2025-2026) and #80083 (7,967). GRAND TOTAL includes them, the per-customer churn does
  not, so GRAND TOTAL − Σ customer rows = 12,178 for 2026 (similar gaps every year: 14,850 in 2020, 9,547 in 2023).
  Either give those rows a counter or fold the amounts into the customer's main row.
- **Text in numeric year cells**: `Mehrwerk` / `NTT` in 2022-2023 on two Cloud rows (#80204, #80219); `n/a` in 326
  cells. Text reads as empty.
- **Product column doubles as a notes field** on closed rows; the actual product of a closed account is lost.
- **Go Live Date** is free text on several rows ("Live per … 12/13/2025", "10/1/2018, EU 02/16/2025").
- Only the first two block titles are lettered ("A.", "B."); the other blocks have no letter, so a lettered-section
  reader sees everything after row 141 as block B.
- **GRAND TOTAL row vs the `Summary` tab** disagree by construction: Summary is "as of 12/31/2025" on a different
  grouping (3,314,531 total 2025 maintenance fee vs the 2025-2026 column 3,352,504).
- The `Maintenance` tab is a stale copy with different totals from 2023-2024 on; a reader picking the wrong tab gets
  different ARR.
- Float residues in the 4 % chain (21,525.920000000002); fees are copied, not formulas, across the year columns on
  some rows.
- No per-customer On-Prem / Cloud split for maintenance (all counted as On-Prem).

---

## 8. Recommendations for a maintenance UI

Model it as **customer → licence purchases → maintenance fee per contract year → monthly schedule**:

- **Enums**: block ∈ {Perpetual live, Not live paying, Not live not paying, Subscription On-Prem, Converted On-Prem,
  Subscription Cloud, Converted Cloud, Others, Closed}; Product ∈ {Perpetual, On-Premise, Cloud}; Billing Anniversary
  ∈ {January–December, Calendar, not applicable} plus a separate **converted** flag (today a suffix in the text);
  Go Live ∈ {date, Not Live} plus a note field; a separate **termination date / reason** field (today inside Product).
- **Derived, not typed**: Original net = list + discount; Total Addl Purchase; Total Current License; 4 % increase and
  Total Maintenance from the fee history; Maintenance %; Basis for Budget = current contract-year fee; Monthly Revenue =
  basis ÷ 12; next contract year = prior × 1.04 unless overridden; the monthly schedule from anniversary + fee; year-end
  Total Revenue and Deferred.
- **Workflows**: New perpetual customer (first-year proration from go-live / first invoice); Annual roll (create next
  contract-year fee at +4 % for every live customer at its anniversary); Additional purchase (adds licence value and
  Additional Maintenance from its go-live); Decommission (negative Additional Maintenance); Convert (explicit 0 in the
  conversion year, converted flag, link to the subscription contract); Terminate (explicit 0 in the first unbilled
  year, move to Closed, record date / reason); Advance billing (match December / Jan–Apr invoices to contract years).
- **Validations**: unique Customer Number; every row with a number has a counter (or is explicitly an add-on of a
  parent row); year cells numeric or blank (no `n/a`, no names); a year cell after an explicit 0 must be blank or
  0 unless re-activated; GRAND TOTAL = Σ rows per year; Σ monthly columns of a calendar year = the row's Total
  Revenue; Deferred(12/31) = fee × months remaining ÷ 12; Σ "YYYY Total Revenue" = GL 4300 for the year; converted
  customers exist in the Subscription Summary with the same number.
- **Compatibility with the dashboard** (if the UI writes the workbook): keep one header row within the first ten rows
  carrying the `YYYY-YYYY` year labels, the headers `Customer Count`, `Customer` (or `Customer Number`) and
  `Customer Name`, a numeric counter on every customer row, a row labelled `GRAND TOTAL` (last one wins) with the
  year totals, explicit 0 for stopped years and blank for not-yet-billed years, and the tab name
  `Maintenance-Adj Advance Billing` (or update `MAINT_TAB`). New columns may be added anywhere.

---

## 9. Headline numbers on the live sheet (2026-11-12, for sanity checks)

| Maintenance year | GRAND TOTAL | Σ customer rows (dashboard) | Dashboard year key |
|---|---|---|---|
| 2019-2020 | 1,695,320 | 1,680,470 | 2020 |
| 2020-2021 | 2,106,182 | 2,103,212 | 2021 |
| 2021-2022 | 2,515,910 | 2,515,910 | 2022 |
| 2022-2023 | 2,941,112 | 2,931,565 | 2023 |
| 2023-2024 | 3,236,110 | 3,224,851 | 2024 |
| 2024-2025 | 3,352,504 | 3,340,794 | 2025 |
| 2025-2026 | 3,251,918 | 3,239,740 | 2026 |
| 2026-2027 | 2,756,957 | 2,744,292 | 2027 |

Rows: 276 customer-like rows, 267 keyed by the dashboard (counter + number + name); 100 of them have no maintenance
values (subscription-only customers). Customer count block: 133 perpetual live, 73 On-Prem subscription, 11 converted
On-Prem, 21 Cloud subscription, 2 converted Cloud = 240. 65 rows read `Not Live`. Four rows carry an explicit 0 in
2026-2027 (stopping next year). The 2026-2027 column is lower than 2025-2026 because most anniversaries for that
contract year have not been billed yet (blank), not because of churn.
