# Starter review

Locations refer to functions in the supplied starter, before replacement. Missing endpoints are listed separately from defects in existing code.

| # | Where | What is wrong | How to notice it | Fix |
|---|---|---|---|---|
| 1 | Database initialization / employee creation | No index enforces employee uniqueness; a preflight read races. | Concurrent creates with one `emp_code`. | Unique employee index; translate `DuplicateKeyError` to 409. |
| 2 | Database initialization / punch-in | No unique attendance key; check-then-insert races. | Twelve simultaneous punches for one employee/day. | Unique `(emp_code, date)` index and direct insert; exactly one 201. |
| 3 | Database initialization | No supporting indexes for lists and analytics. | Explain queries against 100,000 records. | Create query-specific compound indexes idempotently at startup. |
| 4 | Database initialization | PyMongo's default naive UTC dates are not handled explicitly. | Subtract an aware shift boundary from a seeded punch. | Aware UTC decoding and normalization in shared helpers. |
| 5 | `health` | Always claims readiness, even without MongoDB. | Make `ping` fail. | Ping MongoDB; return 503 for driver errors. |
| 6 | `EmployeeIn` | Employee code, lengths and email shape are unchecked. | Submit `EMP001`, an empty name, or `a@b`. | Match the contract's patterns and length limits. |
| 7 | `EmployeeIn` | Shift format and equal boundaries are unchecked. | Submit `24:00`, `9:30`, or equal shift times. | Validate `HH:MM` and reject equal boundaries; overnight shifts remain valid. |
| 8 | `EmployeeIn` / date parameters | Calendar strings are unchecked. | Submit `2026-02-29` or `2026-1-01`. | Require exact formats and validate actual dates/months. |
| 9 | `create_employee` | `datetime.now()` is naive host time. | Inspect creation time on a non-UTC host. | Store aware UTC, truncated to whole seconds. |
| 10 | Employee / attendance responses | Datetimes serialize as ISO strings instead of epoch milliseconds. | Inspect `created_at` or `punch_in`. | Recursively serialize integer milliseconds, including nested audit changes. |
| 11 | `list_employees` | `skip = page * page_size` skips the first page. | Request page 1 with one employee. | Use `(page - 1) * page_size`. |
| 12 | `list_employees` | `total` ignores the department filter. | Filter two Engineering employees from a three-person database. | Count using the page query's filter. |
| 13 | `list_employees` | No deterministic `emp_code` sort. | Insert codes in reverse order and request small pages. | Sort by code ascending in MongoDB. |
| 14 | Both list endpoints | Pagination bounds are unchecked. | Submit page 0 or page size 101. | Validate page >= 1 and page size 1–100. |
| 15 | Department/employee filters | Truthiness ignores an explicitly supplied empty filter. | Request `department=`. | Distinguish omission from an empty exact-match value. |
| 16 | `PunchInIn` | Integer coercion and missing bounds accept invalid epochs. | Supply seconds, floats, numeric strings, booleans, or out-of-range integers. | Strict bounded integers; omission alone defaults to now. |
| 17 | `PunchInIn` | Any status string is accepted. | Punch with `LEAVE`, `ABSENT`, or an unknown status. | Restrict punch-in to the three presence statuses. |
| 18 | `punch_in` | Missing employees cause a subscript error and 500. | Punch with an unknown code. | Resolve the employee or return 404. |
| 19 | `punch_in` | `fromtimestamp()` and `now()` use the host timezone. | Punch at 00:01 IST, still the previous UTC date. | Interpret epochs in UTC; derive attendance dates in IST. |
| 20 | `punch_in` | No overnight attendance-day adjustment. | For a 22:00–06:00 shift, punch at 01:00 next morning. | Times strictly before shift end belong to the preceding day. |
| 21 | Time helpers / punch-in | Milliseconds are preserved, affecting boundaries and storage. | Punch at 09:40:00.900. | Truncate before comparisons and storage. |
| 22 | `compute_late_minutes` | Floors before testing grace, granting nearly an extra minute. | A 09:40:01 punch for a 09:30 shift incorrectly returns 0. | Compare seconds with 600 first, then floor minutes; return 10. |
| 23 | `compute_late_minutes` | Anchors shift start to the punch's date, wrong after midnight. | Punch at 01:00 for the previous day's 22:00 shift. | Build shift start from the attendance day. |
| 24 | `compute_work_hours` | Binary float / half-even rounding violates half-up. | A 00:30:18 duration must produce 0.51 hours. | Integer seconds and `Decimal` with `ROUND_HALF_UP`. |
| 25 | `compute_work_hours` / closing records | No positive-duration or 24-hour validation. | Punch out earlier, at the same instant, or 24 hours and one second later. | Validate truncated final times before updating. |
| 26 | `compute_overtime` | Counts overtime below the 30-minute minimum. | Punch out 29:59 after shift end. | Floor elapsed minutes, then apply the threshold. |
| 27 | `compute_overtime` | Overnight end is placed on the attendance day. | Close a 22:00–06:00 shift at 06:40 next morning. | Advance overnight end one day; return 40 minutes. |
| 28 | Attendance response construction | Exposes an invented `id`, contrary to the natural-key contract. | Inspect punch-in and list responses. | Omit `_id`; do not generate a public attendance ID. |
| 29 | `list_attendance` | Fetches, sorts, counts and paginates all matches in Python. | Request one page from 100,000 records. | Indexed database sort, skip, limit and filtered count. |
| 30 | `list_attendance` | Sorts only by date; same-day ordering is unstable. | Insert same-day employees in reverse order. | Date descending, then employee code ascending. |
| 31 | `list_attendance` | Status and reversed date ranges are unchecked. | Request an invalid status or a start after the end. | Validate enum, dates and inclusive range ordering. |
| 32 | `list_attendance` | Legacy fields remain missing in responses. | Read a seeded record without `history` or `half_day`. | Supply documented defaults without rewriting stored data. |

The starter also leaves punch-out, regularization, all four analytics endpoints and explain unimplemented. These now have explicit validation and real MongoDB integration tests. Punch-out atomically closes the selected record. Corrections recompute derived values and append history while comparing the previously read state. Analytics consume stored derived values, retain no-log employees, rank before applying a cutoff, and generate calendar gaps and moving averages in MongoDB. Explain shares the production query builders.

Examined and retained: environment variables already override `.env` by default (now explicit); `emp_code` is the correct public identity; BSON datetimes are appropriate for instants; ignoring extra input fields is appropriate for read-only derived fields; `work_hours: null` distinguishes an open record from zero hours; inclusive string comparisons work for validated ISO dates; synchronous PyMongo in synchronous FastAPI routes runs in the framework's worker threads.
