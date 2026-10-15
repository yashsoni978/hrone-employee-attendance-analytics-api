import pytest
from fastapi.testclient import TestClient
from app.main import app, db, init_indexes
import sample_seed
from datetime import datetime, timezone, timedelta

client = TestClient(app)
IST = timezone(timedelta(hours=5, minutes=30))

def setup_module():
    import subprocess
    import sys
    subprocess.run([sys.executable, "sample_seed.py"], check=True)
    init_indexes()

def test_overnight_shift_punch_in():
    # EMP0005 has overnight shift 22:00 - 06:00
    # Punch in at 05:45 IST on 2026-07-08 -> belongs to 2026-07-07!
    p_in_dt = datetime(2026, 7, 8, 5, 45, 0, tzinfo=IST)
    p_in_ms = int(p_in_dt.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": p_in_ms})
    assert res.status_code == 201
    data = res.json()
    assert data["date"] == "2026-07-07"
    assert data["late_minutes"] == int((datetime(2026, 7, 8, 5, 45, 0, tzinfo=IST) - datetime(2026, 7, 7, 22, 0, 0, tzinfo=IST)).total_seconds() // 60)

    # Punch out at 06:45 IST on 2026-07-08 -> overtime = 45 min
    p_out_dt = datetime(2026, 7, 8, 6, 45, 0, tzinfo=IST)
    p_out_ms = int(p_out_dt.timestamp() * 1000)

    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": p_out_ms})
    assert res_out.status_code == 200
    data_out = res_out.json()
    assert data_out["overtime_minutes"] == 45
    assert data_out["work_hours"] == 1.0
    assert data_out["half_day"] is True  # 1.0 < 4.50

def test_leaderboard_competition_ranking_and_ties():
    # Let's seed two tied employees on late minutes
    emp_a = {
        "emp_code": "EMP9001",
        "name": "Tied Alice",
        "email": "alice@tied.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    emp_b = {
        "emp_code": "EMP9002",
        "name": "Tied Bob",
        "email": "bob@tied.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    client.post("/employees", json=emp_a)
    client.post("/employees", json=emp_b)

    # Both punch in at 10:00:00 IST on 2026-08-03 -> 30 min late
    p_dt = datetime(2026, 8, 3, 10, 0, 0, tzinfo=IST)
    p_ms = int(p_dt.timestamp() * 1000)

    client.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": p_ms})
    client.post("/attendance/punch-in", json={"emp_code": "EMP9002", "punched_at": p_ms})

    res = client.get("/analytics/leaderboard/late?month=2026-08&limit=1")
    assert res.status_code == 200
    data = res.json()
    # Both have rank 1, so both should be returned even though limit=1!
    assert len(data["items"]) == 2
    assert data["items"][0]["rank"] == 1
    assert data["items"][1]["rank"] == 1
    # Sorted by emp_code asc when tied: EMP9001 then EMP9002
    assert data["items"][0]["emp_code"] == "EMP9001"
    assert data["items"][1]["emp_code"] == "EMP9002"

def test_department_summary_zero_log_headcount():
    # An employee with 0 logs who joined on or before month end must be in headcount
    res = client.get("/analytics/departments/summary?month=2026-08&department=Engineering")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    # Engineering has EMP0001, EMP0002, EMP9001, EMP9002 (all joined <= 2026-08-31)
    assert items[0]["headcount"] >= 4

def test_department_trend_gap_filling_densify():
    # From 2026-08-01 to 2026-08-05 (5 days)
    res = client.get("/analytics/departments/Engineering/trend?from=2026-08-01&to=2026-08-05")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 5
    dates = [x["date"] for x in items]
    assert dates == ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"]
    # 2026-08-01 is Saturday (is_working_day = False)
    assert items[0]["is_working_day"] is False
    assert items[0]["attendance_rate"] is None
    # 2026-08-03 is Monday (is_working_day = True)
    assert items[2]["is_working_day"] is True
    assert items[2]["attendance_rate"] is not None
