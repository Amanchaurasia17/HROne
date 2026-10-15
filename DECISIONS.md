# DECISIONS.md

1. **Indexes.** We created `employees.emp_code` (unique) and `attendance_logs.(emp_code, date)` (unique) to guarantee identity and prevent duplicate punches. Supporting indexes include `attendance_logs.(date DESC, emp_code ASC)` for fast list pagination, `(emp_code, punch_in DESC)` for punch-out matching, `employees.(department, joined_on)` for summary lookups, and `attendance_logs.(date, late_minutes, emp_code)` for leaderboard filtering. We considered and rejected indexing the nested `history` array because it is strictly append-only audit data and never filtered.

2. **Punch-in race.** Both concurrent requests calculate the same IST attendance date and attempt `insert_one()`. MongoDB's compound unique index on `(emp_code, date)` admits exactly one insert. The second insert raises a `DuplicateKeyError`, which our exception handler intercepts to immediately return HTTP 409 Conflict.

3. **Ties.** We apply standard competition ranking (`$rank`), where ties share a rank and skip subsequent ranks (e.g., 1, 2, 2, 4). Because `limit` filters on `rank <= limit`, all employees tied at or below the cutoff are included, sorted by `total_late_minutes` descending and then `emp_code` ascending.

4. **Headcount.** The aggregation pipeline starts from `employees` matching `joined_on <= month_end`, grouping active employees by department first and using an outer `$lookup` (`preserveNullAndEmptyArrays: true`) into `attendance_logs`. Employees with zero logs still increment headcount.

5. **One thing you would change.** At 100x scale, I would maintain pre-aggregated monthly and daily rollup collections updated via MongoDB Change Streams, avoiding expensive on-demand cross-collection aggregations.
