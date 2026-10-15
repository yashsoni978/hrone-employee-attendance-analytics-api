# Employee Attendance & Analytics API

A high-performance FastAPI service backed by MongoDB for employee attendance tracking, manual audit trail regularizations, and real-time aggregation analytics pipelines.

## Requirements
- Python 3.11+
- MongoDB 6.0+

## Setup & Running

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment variables:**
   Set `MONGO_URI` and `MONGO_DB` (or rely on default `mongodb://localhost:27017` and `attendance_db`):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Start the service:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   *Note: All MongoDB indexes are initialized idempotently during startup lifespan.*

## Key Capabilities & Endpoints
- **Readiness:** `GET /health` (validates MongoDB connectivity).
- **Employee Management:** `POST /employees`, `GET /employees` (paginated, sorted).
- **Attendance Logging:**
  - `POST /attendance/punch-in` (race-safe duplicate prevention, R1 overnight shifts, R2 grace period).
  - `POST /attendance/punch-out` (CAS concurrency handling, R3 overtime, R4 work hours, R5 half-day).
  - `GET /attendance` (indexed pagination & date/status filtering).
  - `PATCH /attendance/{emp_code}/{date}` (atomic regularizations with full history audit trail).
- **Analytics Pipelines:**
  - `GET /analytics/employees/{emp_code}/monthly` (Mon–Fri working day adjustments, R5 half-day weighting).
  - `GET /analytics/departments/summary` (zero-log employee headcount preservation, record-level work hour averages).
  - `GET /analytics/leaderboard/late` (standard competition ranking with window functions and post-rank limiting).
  - `GET /analytics/departments/{department}/trend` (gap-filled daily calendar series via `$densify`, 7-day moving averages via `$setWindowFields`).
- **Query Explain Plans:** `GET /admin/explain/{endpoint}` (inspects execution plans with `executionStats`).

## Testing
Run the test suites with pytest:
```bash
pytest test_api.py test_edge_cases.py -v
```
