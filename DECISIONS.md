# Design decisions

1. **Indexes.** Unique employee-code and employee/day indexes enforce identity. Department/code supports employee pages; joined-date/department and department/joined-date support headcount. Date/code and status/date/code support attendance pages and period scans. Employee/punch-in finds the latest eligible punch. I rejected a standalone status index: the compound index also supports ordering, avoiding another index on every write. Startup creates these idempotently; explain uses the same builders and hints as production.

2. **Punch-in race.** Both requests resolve the employee and calculate the same IST attendance day. Both attempt insertion. MongoDB's unique compound index accepts one and rejects the other. The winner returns 201; catching `DuplicateKeyError` gives the loser 409. No preliminary existence check decides correctness.

3. **Ties.** MongoDB's `$rank` assigns competition ranks before filtering `rank <= limit`. Everyone tied at an included rank is returned, ordered by employee code within the tie. The next rank skips those tied positions.

4. **Headcount.** Summary starts from eligible employees. A lookup aggregates each employee's logs, preserving employees with empty results before grouping departments.

5. **100x data.** I would measure explain statistics, then maintain daily employee aggregates for reporting, updating affected days after corrections. I would verify those summaries against original records before serving them.
