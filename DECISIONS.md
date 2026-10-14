# DECISIONS.md

1. **Indexes.**
I created unique `employees: {emp_code: 1}` for identity and punch lookups, `employees: {department: 1, joined_on: 1}` and `{joined_on: 1}` to eliminate COLLSCAN on department queries, unique `attendance_logs: {emp_code: 1, date: 1}` for natural-key lookups and punch-in idempotency, `{date: -1, emp_code: 1}` for indexed pagination, and `{date: 1, late_minutes: 1}` for the late leaderboard. I considered and rejected `{ "history.at": 1 }` because `history` is an append-only audit trail never queried directly.

2. **Punch-in race.**
Both concurrent requests pass validation and attempt atomic `insert_one`. MongoDB accepts the first document and rejects the second with a unique index violation (`DuplicateKeyError` on `emp_code, date`). FastAPI catches this exception and returns HTTP 409 to the second request, preventing race conditions without check-then-insert locking.

3. **Ties.**
The pipeline uses MongoDB's `$rank` window function for standard competition ranking (e.g. 1, 2, 2, 4) and filters by `rank <= limit`. Any employees tied at the cutoff boundary are returned, exceeding `limit` if necessary, ordered by `emp_code` ascending per specification.

4. **Headcount.**
The pipeline starts on `employees` filtered by `joined_on <= month_end`, and joins `attendance_logs` with `preserveNullAndEmptyArrays: true`. Employees without attendance logs remain in the stream with null logs and are counted in their department's distinct `$addToSet: "$emp_code"`.

5. **One thing you would change for 100x data.**
I would maintain pre-aggregated daily summaries asynchronously using change streams or background workers and shard `attendance_logs` by `{emp_code: "hashed", date: 1}`, avoiding heavy real-time `$lookup` and `$densify` aggregations over 10M+ documents.