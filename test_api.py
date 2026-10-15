import pytest
from fastapi.testclient import TestClient
from app.main import app, db, init_indexes
import sample_seed

client = TestClient(app)

def setup_module():
    # Seed sample database
    import subprocess
    import sys
    subprocess.run([sys.executable, "sample_seed.py"], check=True)
    init_indexes()

def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_list_employees():
    res = client.get("/employees")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 6
    assert len(data["items"]) == 6
    assert data["page"] == 1
    # Check sorting by emp_code asc
    codes = [item["emp_code"] for item in data["items"]]
    assert codes == sorted(codes)
    # Check epoch millis format for created_at
    assert isinstance(data["items"][0]["created_at"], int)

def test_create_employee():
    payload = {
        "emp_code": "EMP0099",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    assert res.json()["emp_code"] == "EMP0099"
    assert isinstance(res.json()["created_at"], int)

    # Duplicate emp_code -> 409
    res_dup = client.post("/employees", json=payload)
    assert res_dup.status_code == 409

    # Equal shift_start and shift_end -> 422
    payload_bad = dict(payload, emp_code="EMP0098", shift_end="09:30")
    res_bad = client.post("/employees", json=payload_bad)
    assert res_bad.status_code == 422

def test_punch_in_and_out():
    # Create employee with normal shift
    emp_payload = {
        "emp_code": "EMP0088",
        "name": "Punch Tester",
        "email": "punch@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    }
    client.post("/employees", json=emp_payload)

    # Punch in at 09:40:01 IST on 2026-07-20 -> 10 minutes late
    # 2026-07-20 09:40:01 IST = 2026-07-20 04:10:01 UTC = 1784520601000 ms
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    p_in_dt = datetime(2026, 7, 20, 9, 40, 1, tzinfo=IST)
    p_in_ms = int(p_in_dt.timestamp() * 1000)

    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0088", "punched_at": p_in_ms})
    assert res_in.status_code == 201
    in_data = res_in.json()
    assert in_data["date"] == "2026-07-20"
    assert in_data["late_minutes"] == 10
    assert in_data["work_hours"] is None
    assert in_data["punch_out"] is None

    # Duplicate punch in for same day -> 409
    res_in_dup = client.post("/attendance/punch-in", json={"emp_code": "EMP0088", "punched_at": p_in_ms})
    assert res_in_dup.status_code == 409

    # Punch out at 19:10:00 IST on 2026-07-20 -> 40 min overtime, work_hours ~ 9.5
    p_out_dt = datetime(2026, 7, 20, 19, 10, 0, tzinfo=IST)
    p_out_ms = int(p_out_dt.timestamp() * 1000)

    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0088", "punched_at": p_out_ms})
    assert res_out.status_code == 200
    out_data = res_out.json()
    assert out_data["overtime_minutes"] == 40
    assert out_data["work_hours"] == 9.5
    assert out_data["half_day"] is False

    # Second punch out -> 409
    res_out_dup = client.post("/attendance/punch-out", json={"emp_code": "EMP0088", "punched_at": p_out_ms})
    assert res_out_dup.status_code == 409

def test_regularize():
    # Regularize EMP0088 on 2026-07-20 to fix punch_in to 09:30:00 IST (no late)
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    p_fix_dt = datetime(2026, 7, 20, 9, 30, 0, tzinfo=IST)
    p_fix_ms = int(p_fix_dt.timestamp() * 1000)

    reg_payload = {
        "punch_in": p_fix_ms,
        "reason": "Correcting biometric device delay",
        "regularized_by": "admin.hr"
    }
    res = client.patch("/attendance/EMP0088/2026-07-20", json=reg_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 0
    assert len(data["history"]) == 1
    assert data["history"][0]["by"] == "admin.hr"
    assert "late_minutes" in data["history"][0]["changes"]
    assert data["history"][0]["changes"]["late_minutes"]["from"] == 10
    assert data["history"][0]["changes"]["late_minutes"]["to"] == 0

    # Changing nothing -> 422
    res_same = client.patch("/attendance/EMP0088/2026-07-20", json=reg_payload)
    assert res_same.status_code == 422

def test_analytics_endpoints():
    # Employee monthly for EMP0003
    res_m = client.get("/analytics/employees/EMP0003/monthly?month=2026-07")
    assert res_m.status_code == 200
    data_m = res_m.json()
    assert data_m["emp_code"] == "EMP0003"
    assert data_m["working_days"] == 23
    assert data_m["leave_days"] == 1
    assert data_m["late_count"] == 1
    assert data_m["total_late_minutes"] == 35

    # Department summary for 2026-07
    res_ds = client.get("/analytics/departments/summary?month=2026-07")
    assert res_ds.status_code == 200
    items = res_ds.json()["items"]
    assert len(items) >= 3

    # Late leaderboard for 2026-07
    res_lb = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res_lb.status_code == 200
    lb_items = res_lb.json()["items"]
    assert len(lb_items) >= 1
    assert lb_items[0]["rank"] == 1

    # Department trend for Engineering
    res_tr = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-08")
    assert res_tr.status_code == 200
    tr_items = res_tr.json()["items"]
    assert len(tr_items) == 3
    assert [d["date"] for d in tr_items] == ["2026-07-06", "2026-07-07", "2026-07-08"]

def test_explain_endpoints():
    for ep in ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]:
        params = {
            "emp_code": "EMP0001",
            "month": "2026-07",
            "department": "Engineering",
            "from": "2026-07-06",
            "to": "2026-07-08"
        }
        res = client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200, f"Explain failed for {ep}: {res.text}"
        data = res.json()
        assert "explain" in data and "endpoint" in data and "collection" in data
        assert data["endpoint"] == ep
        explain_doc = data["explain"]
        assert "queryPlanner" in explain_doc or "stages" in explain_doc or "ok" in explain_doc
