# Decisions

1. **Indexes.** `employees.emp_code` is unique so employee identity is enforced by MongoDB during concurrent writes. The department/join-date index supports department headcount filters. Attendance has a unique (`emp_code`, `date`) index for one record per employee day, a date/employee index for sorted date listings, an employee/date/status index for employee summaries, and a punch-in index for finding the latest open shift. I would avoid a standalone status index: its low selectivity is unlikely to help the main queries.

2. **Punch-in race.** Both requests derive the same natural key and try to insert it. MongoDB's unique index accepts one insert. The other insert raises `DuplicateKeyError`, which the endpoint converts to 409. The preliminary employee lookup does not decide the winner.

3. **Ties.** MongoDB `$rank` assigns equal late-minute totals the same rank and skips the next rank. We filter on rank after assigning it, so a tie at the limit is returned in full; `emp_code` orders tied rows consistently.

4. **Headcount.** The summary starts from employees joined by month end and looks up that employee's logs. Employees with no matching logs remain in the employee stream, so they contribute to headcount and zero-valued statistics.

5. **100x data.** I would move recurring analytics to pre-aggregated daily summaries, keep raw records for audit, and benchmark shard or partition choices with the real query mix before changing indexes.
