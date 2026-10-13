# Starter review

| # | Where | What was wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Compared a naive timestamp to the shift clock on its UTC date; overnight shifts and timezone offsets were wrong. | Punch at 09:40 IST or an overnight punch after midnight. | Convert the instant to IST, use the appropriate shift start, and apply the strict 10-minute grace rule. |
| 2 | `compute_work_hours` | Python `round` uses ties-to-even, unlike required half-up rounding. | A duration exactly halfway at two decimal places. | Decimal arithmetic with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | End time did not roll to tomorrow for overnight shifts and omitted the 30-minute minimum. | 22:00–06:00 shift, or 29 versus 30 overtime minutes. | Calculate the IST shift end on the correct date and apply the threshold. |
| 4 | `create_employee` | Check-then-insert allowed duplicate employee codes under concurrency. | Send simultaneous creates with one `emp_code`. | Unique database index; map duplicate-key errors to 409. |
| 5 | `create_employee` | `created_at` was naive and returned as a datetime rather than epoch milliseconds. | Inspect response timestamp and UTC offset. | Store an aware UTC instant and serialize milliseconds. |
| 6 | `list_employees` | `skip` used `page * page_size`, skipping page one; total ignored department filter; no stable sort or bounds. | Request page 1 or a department filter. | Correct offset, filtered count, sorted results, validated pagination. |
| 7 | `punch_in` | Unknown employees caused a 500; missing employee was not checked. | Punch in with an unregistered code. | Return 404. |
| 8 | `punch_in` | Falsy timestamp handling rejected valid epoch values and used local naive time; timestamps were not truncated or validated. | Omit timestamp, send invalid seconds/float, or fractional second. | Validate integer milliseconds, use UTC, truncate to seconds. |
| 9 | `punch_in` | Attendance date was UTC date rather than IST/overnight attendance date. | Punch shortly after midnight IST on an overnight shift. | Derive the attendance date under R1. |
| 10 | `punch_in` | Status was not restricted and did not return the contract shape. | Send `ABSENT`; inspect response for `id` / missing fields. | Accept presence statuses only, rely on unique natural key, return no Mongo id. |
| 11 | `list_attendance` | Loaded all matching records into Python, making 100k records slow and memory-heavy. | Run with a large collection. | Count and page in MongoDB using indexed query and deterministic sort. |
| 12 | `list_attendance` | Pagination, date range validation, status validation and inclusive filter correctness were missing or incomplete. | Page 1, reversed dates, malformed date/status. | Validate inputs and apply filters before count and page. |
| 13 | Starter omissions | Punch-out, regularization, analytics, and explain endpoints were absent. | Call any contract path marked for implementation. | Implemented the remaining contract paths and aggregation pipelines. |

Reviewed and retained: BSON datetimes are the right storage type for instants; employee and attendance identity use `emp_code` and (`emp_code`, `date`), not `_id`; stored derived values are read directly by analytics.
