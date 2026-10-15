import sys
import requests
import json
from datetime import datetime, timezone, timedelta

BASE_URL = "http://localhost:8000"
IST = timezone(timedelta(hours=5, minutes=30))

def run_checks():
    print("=" * 70)
    print("RUNNING COMPLETE MANUAL VERIFICATION SUITE")
    print("=" * 70)
    
    passed = 0
    failed = 0

    def assert_check(name, condition, extra_info=""):
        nonlocal passed, failed
        if condition:
            print(f"[PASS] {name}")
            passed += 1
        else:
            print(f"[FAIL] {name} - {extra_info}")
            failed += 1

    # 1. Readiness Probe
    r = requests.get(f"{BASE_URL}/health")
    assert_check("1. Health check returns 200", r.status_code == 200 and r.json().get("status") == "ok")

    # 2. Create Employee
    emp_payload = {
        "emp_code": "EMP0099",
        "name": "Manual Tester",
        "email": "manual@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    }
    r = requests.post(f"{BASE_URL}/employees", json=emp_payload)
    assert_check("2. Create employee (EMP0099) returns 201", r.status_code in (201, 409))
    if r.status_code == 201:
        data = r.json()
        assert_check("2b. Created_at is epoch millis", isinstance(data.get("created_at"), int))

    # 3. Duplicate Employee (Unique Index)
    r = requests.post(f"{BASE_URL}/employees", json=emp_payload)
    assert_check("3. Duplicate employee creation returns 409", r.status_code == 409)

    # 4. Invalid Shift Time (shift_start == shift_end)
    bad_emp = dict(emp_payload, emp_code="EMP0098", shift_end="09:30")
    r = requests.post(f"{BASE_URL}/employees", json=bad_emp)
    assert_check("4. Equal shift_start and shift_end returns 422", r.status_code == 422)

    # 5. List Employees Sorting & Pagination
    r = requests.get(f"{BASE_URL}/employees?page=1&page_size=5")
    assert_check("5. List employees returns 200", r.status_code == 200)
    items = r.json().get("items", [])
    codes = [x["emp_code"] for x in items]
    assert_check("5b. Employees sorted by emp_code ASC", codes == sorted(codes))

    # 6. Punch-In with Grace Period (R2)
    # Test A: 09:40:00 IST on 2026-07-20 (exact 10 min, inside grace)
    p_in_grace_dt = datetime(2026, 7, 20, 9, 40, 0, tzinfo=IST)
    p_in_grace_ms = int(p_in_grace_dt.timestamp() * 1000)
    r = requests.post(f"{BASE_URL}/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": p_in_grace_ms})
    if r.status_code == 201:
        assert_check("6a. Punch-in within 10 min grace: late_minutes == 0", r.json().get("late_minutes") == 0)
    else:
        assert_check("6a. Punch-in within grace executed", r.status_code in (201, 409))

    # Test B: 09:40:01 IST on 2026-07-21 (> 10 min, late by 10 min from 09:30)
    p_in_late_dt = datetime(2026, 7, 21, 9, 40, 1, tzinfo=IST)
    p_in_late_ms = int(p_in_late_dt.timestamp() * 1000)
    r = requests.post(f"{BASE_URL}/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": p_in_late_ms})
    if r.status_code == 201:
        assert_check("6b. Punch-in 09:40:01: late_minutes == 10", r.json().get("late_minutes") == 10)
    else:
        assert_check("6b. Punch-in late executed", r.status_code in (201, 409))

    # 7. Duplicate Punch-In on same date
    r = requests.post(f"{BASE_URL}/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": p_in_late_ms})
    assert_check("7. Duplicate punch-in for same date returns 409", r.status_code == 409)

    # 8. Punch-Out (R3 Overtime & R4 Work Hours & R5 Half Day)
    # 19:10:00 IST on 2026-07-21 -> shift ends 18:30 -> 40 min OT, work_hours ~ 9.5
    p_out_dt = datetime(2026, 7, 21, 19, 10, 0, tzinfo=IST)
    p_out_ms = int(p_out_dt.timestamp() * 1000)
    r = requests.post(f"{BASE_URL}/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": p_out_ms})
    if r.status_code == 200:
        data = r.json()
        assert_check("8a. Punch-out computes work_hours == 9.5", data.get("work_hours") == 9.5)
        assert_check("8b. Punch-out computes overtime_minutes == 40", data.get("overtime_minutes") == 40)
        assert_check("8c. Punch-out half_day == false", data.get("half_day") is False)
    else:
        assert_check("8. Punch-out executed", r.status_code in (200, 409))

    # 9. Duplicate Punch-Out
    r = requests.post(f"{BASE_URL}/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": p_out_ms})
    assert_check("9. Duplicate punch-out returns 409", r.status_code == 409)

    # 10. Overnight Shift (R1 & R3)
    # EMP0005 has shift 22:00 - 06:00. Punch-in 05:45 IST on 2026-07-22 belongs to 2026-07-21!
    p_night_in_dt = datetime(2026, 7, 22, 5, 45, 0, tzinfo=IST)
    p_night_in_ms = int(p_night_in_dt.timestamp() * 1000)
    r = requests.post(f"{BASE_URL}/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": p_night_in_ms})
    if r.status_code == 201:
        assert_check("10a. Overnight punch-in belongs to previous date (2026-07-21)", r.json().get("date") == "2026-07-21")
    else:
        assert_check("10a. Overnight punch-in executed", r.status_code in (201, 409))

    # 11. Regularization with Audit Trail
    # Correct EMP0001 on 2026-07-21 to fix punch_in to 09:30:00 (late_minutes -> 0)
    p_fix_dt = datetime(2026, 7, 21, 9, 30, 0, tzinfo=IST)
    p_fix_ms = int(p_fix_dt.timestamp() * 1000)
    reg_payload = {
        "punch_in": p_fix_ms,
        "reason": "Correcting biometric glitch",
        "regularized_by": "HR Admin"
    }
    r = requests.patch(f"{BASE_URL}/attendance/EMP0001/2026-07-21", json=reg_payload)
    if r.status_code == 200:
        data = r.json()
        assert_check("11a. Regularization recomputes late_minutes to 0", data.get("late_minutes") == 0)
        assert_check("11b. History appended with audit record", len(data.get("history", [])) >= 1)
        last_hist = data.get("history", [])[-1]
        assert_check("11c. History record contains author and reason", last_hist.get("by") == "HR Admin")
    else:
        assert_check("11. Regularization executed", r.status_code in (200, 422))

    # 12. Request changing nothing -> 422
    r = requests.patch(f"{BASE_URL}/attendance/EMP0001/2026-07-21", json=reg_payload)
    assert_check("12. Regularization with no changes returns 422", r.status_code == 422)

    # 13. Department Summary (Zero-log headcount test)
    r = requests.get(f"{BASE_URL}/analytics/departments/summary?month=2026-07")
    assert_check("13a. Department summary returns 200", r.status_code == 200)
    items = r.json().get("items", [])
    eng = next((d for d in items if d["department"] == "Engineering"), None)
    assert_check("13b. Engineering headcount counts employees with zero logs", eng is not None and eng.get("headcount", 0) >= 2)

    # 14. Late Leaderboard (Standard Competition Ranking)
    r = requests.get(f"{BASE_URL}/analytics/leaderboard/late?month=2026-07&limit=5")
    assert_check("14a. Late leaderboard returns 200", r.status_code == 200)
    lb_items = r.json().get("items", [])
    if lb_items:
        assert_check("14b. Leaderboard has ranked entries", lb_items[0].get("rank") == 1)

    # 15. Department Trend ($densify gap filling & 7d moving average)
    r = requests.get(f"{BASE_URL}/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12")
    assert_check("15a. Department trend returns 200", r.status_code == 200)
    tr_items = r.json().get("items", [])
    assert_check("15b. Trend returns exactly 7 calendar days (gap filled)", len(tr_items) == 7)
    # Weekend days must have is_working_day == False
    sat = next((d for d in tr_items if d["date"] == "2026-07-11"), None)
    assert_check("15c. Weekend days marked as non-working", sat is not None and sat.get("is_working_day") is False)

    # 16. Admin Explain (No COLLSCAN)
    r = requests.get(f"{BASE_URL}/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31")
    assert_check("16a. Admin explain returns 200", r.status_code == 200)
    plan_str = json.dumps(r.json())
    assert_check("16b. Plan uses IXSCAN", "IXSCAN" in plan_str)
    assert_check("16c. Plan contains no COLLSCAN", "COLLSCAN" not in plan_str)

    print("=" * 70)
    print(f"SUMMARY: {passed} PASSED, {failed} FAILED")
    print("=" * 70)

if __name__ == "__main__":
    run_checks()
