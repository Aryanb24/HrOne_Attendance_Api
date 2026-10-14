# DECISIONS.md

1. **Indexes.**
I created `employees: { emp_code: 1 }` (unique) to enforce identity and provide fast lookups during punches, and `{ department: 1 }` for department filtering and aggregations. For `attendance_logs`, I created `{ emp_code: 1, date: 1 }` (unique) to guarantee at most one record per employee-day and enable instantaneous natural-key lookups, `{ date: -1, emp_code: 1 }` to serve sorted paginated listing without in-memory sorting, and `{ status: 1 }` for status filtering. I considered an index on `{ "history.at": 1 }` but rejected it because history is an append-only audit trail that is never queried directly by any endpoint.

2. **Punch-in race.**
Both requests validate against Pydantic, convert epoch milliseconds to IST attendance dates, and attempt an atomic `insert_one`. Because of the unique compound index on `(emp_code, date)`, MongoDB atomically accepts the first document and throws a `DuplicateKeyError` (E11000) on the second. The exception handler catches this and returns an immediate HTTP 409, guaranteeing no duplicate records without relying on vulnerable check-then-insert logic.

3. **Ties.**
When employees tie at the cutoff, the leaderboard returns all tied employees, exceeding `limit` if necessary. Following standard competition ranking (`$rank` window operator), tied individuals share the same rank and subsequent ranks are skipped (e.g., 1, 2, 2, 4). The contract specifies that `limit` filters by `rank <= limit`, not by row count, and orders tied rows by `emp_code` ascending.

4. **Headcount.**
The pipeline begins on the `employees` collection filtered by `joined_on <= month_end`, and uses `$lookup` to join `attendance_logs` for that month. By keeping `preserveNullAndEmptyArrays: true` on `$unwind`, employees with zero attendance records are preserved and grouped into their department's `$addToSet: "$emp_code"`, accurately reflecting full headcount.

5. **One thing you would change for 100x data.**
At 100x scale, running heavy multi-stage aggregations (such as `$lookup` with `$densify` and dynamic date parsing) on live operational collections would cause memory and CPU saturation. I would pre-aggregate daily department metrics and monthly rollups asynchronously via change streams or an out-of-band worker into dedicated summary collections, shard `attendance_logs` on `{ emp_code: "hashed", date: 1 }`, and serve analytics via point queries directly against pre-computed read models.