# Architectural & Design Decisions

### 1. Indexes Created & Rationale
We created the following strategic compound and unique indexes at application startup:
- `employees`: `{ emp_code: 1 }` (unique) to enforce natural business entity uniqueness and guarantee O(1) employee identity resolution; `{ department: 1, joined_on: 1 }`, `{ joined_on: 1 }`, and `{ department: 1 }` to support department-level aggregations and headcount eligibility filtering without collection scans.
- `attendance_logs`: `{ emp_code: 1, date: 1 }` (unique composite) to enforce the business invariant of at most one attendance document per employee per calendar day and eliminate race conditions on punch-in; `{ date: -1, emp_code: 1 }` to back pagination and deterministic secondary sorting; `{ date: 1, late_minutes: 1 }` to optimize late leaderboard and monthly window queries; `{ emp_code: 1, date: 1, status: 1 }` to accelerate monthly employee aggregations.

### 2. Concurrent Punch-in Race Resolution
When two identical punch-in requests arrive simultaneously for the same employee and date, both evaluate date rules (R1) and derived properties before executing an atomic `insert_one`. Because MongoDB enforces the unique compound index on `(emp_code, date)`, the database atomically accepts the first document and rejects the second with a DuplicateKeyError (`code 11000`). The service intercepts `DuplicateKeyError` and returns an HTTP 409 Conflict. This guarantees exactly one 201 response and zero corrupted duplicates without relying on fragile application-level check-then-act logic.

### 3. Leaderboard Tie Resolution at Cutoff
The leaderboard pipeline calculates standard competition ranks (`$rank`: 1, 2, 2, 4) across all late employees within the period using MongoDB's `$setWindowFields`. Because `limit` is evaluated *after* ranking (`rank <= limit`), all employees sharing a qualifying rank up to the limit are retained, and tied rows are ordered deterministically by `emp_code` ascending. Consequently, if a tie occurs across the cutoff boundary, all tied employees are returned rather than being arbitrarily truncated.

### 4. Headcount Handling for Zero-Log Employees
The `/analytics/departments/summary` pipeline roots its aggregation on the `employees` collection rather than `attendance_logs`. It first matches employees where `joined_on <= month_end`, grouping them by department to calculate the true active `headcount`. It subsequently executes a `$lookup` against `attendance_logs` to collect logs for that month. Employees who have logged zero attendance days remain in the department grouping and are accurately counted in `headcount`.

### 5. Strategy for 100x Scale
At 100x data volume (~10 million records), executing multi-stage `$lookup` and `$densify` pipelines on request would degrade latency and strain server memory. We would adopt a write-time pre-aggregation pattern: MongoDB Change Streams would asynchronously update pre-computed daily department rollups and employee monthly summary collections whenever punch or regularization writes occur. This shifts aggregation workloads from read-time to write-time, enabling constant-time O(1) analytics retrieval backed by covered index scans.
