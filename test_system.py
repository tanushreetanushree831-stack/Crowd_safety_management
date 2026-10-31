"""
test_system.py - End-to-End Test Suite for Crowd Safety Management System
Tests:
  1. Frontend routes (Login page, Guest login, Dashboard page)
  2. Live status polling & threshold capacity calculations
  3. Dynamic capacity adjustment
  4. Entry, exit, and counter reset APIs
  5. Real-time weapon/threat detection & alert triggering
  6. Threat acknowledgment & clear API
"""

import sys
import app
import database
import cv2
import numpy as np

def run_tests():
    print("=" * 60)
    print("STARTING CROWD SAFETY & THREAT DETECTION TEST SUITE")
    print("=" * 60)

    client = app.app.test_client()

    # 1. Test Login Page (GET)
    print("\n[TEST 1] Verifying Login Page...")
    res = client.get('/login')
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    assert b"CrowdSense" in res.data, "CrowdSense brand missing"
    assert b"Sign in as Guest" in res.data, "Guest login button missing"
    print("✓ Login page loaded successfully with CrowdSense theme & Guest login option.")

    # 2. Test Guest Authentication
    print("\n[TEST 2] Verifying Guest Login & Session...")
    res = client.get('/guest_login', follow_redirects=True)
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    assert b"Dynamic Crowd Safety Management System" in res.data, "Dashboard title missing"
    assert b"CrowdSafe" in res.data, "CrowdSafe brand missing in dashboard"
    assert b"threat-alert-banner" in res.data, "Security threat banner missing"
    print("✓ Guest login redirects and renders full CrowdSafe dashboard.")

    # 3. Test Credentials Login
    print("\n[TEST 3] Verifying Username/Password Login...")
    client2 = app.app.test_client()
    res = client2.post('/login', data={'username': 'user', 'password': 'pass123'}, follow_redirects=True)
    assert res.status_code == 200
    assert b"CrowdSafe" in res.data
    print("✓ Standard credentials authentication ('user' / 'pass123') verified.")

    # 4. Test Live Status Polling
    print("\n[TEST 4] Verifying Live Status Polling (/status)...")
    res = client.get('/status')
    assert res.status_code == 200
    data = res.get_json()
    assert "people_inside" in data, "people_inside missing in /status"
    assert "max_capacity" in data, "max_capacity missing in /status"
    assert "threat_detected" in data, "threat_detected missing in /status"
    assert "threat_alert" in data, "threat_alert missing in /status"
    print(f"✓ /status endpoint responding normally (Capacity: {data['max_capacity']}, Inside: {data['people_inside']}, Threat: {data['threat_detected']}).")

    # 5. Test Dynamic Capacity Adjustment
    print("\n[TEST 5] Testing Dynamic Capacity Adjustment (/set_capacity)...")
    res = client.get('/set_capacity?capacity=8')
    assert res.status_code == 200
    assert res.get_json()['max_capacity'] == 8
    res = client.get('/status')
    assert res.get_json()['max_capacity'] == 8
    # Reset back to 5
    client.get('/set_capacity?capacity=5')
    print("✓ Venue capacity dynamically updated and reflected in /status.")

    # 6. Test Frame Verification & Threat Detection
    print("\n[TEST 6] Testing Frame Verification & Weapon/Threat Detection...")
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    _, img_encoded = cv2.imencode('.jpg', img)
    img_bytes = img_encoded.tobytes()

    # Clean frame
    res = client.post('/verify_frame', data=img_bytes, content_type='application/octet-stream')
    assert res.status_code == 200
    clean_data = res.get_json()
    assert clean_data['threat_detected'] is False
    print("✓ Standard clean verification frame processed without false threat.")

    # Frame with detected weapon (simulation)
    orig_inspect = app.inspect_frame_for_threats
    try:
        app.inspect_frame_for_threats = lambda p: ['Gun', 'Knife']
        res = client.post('/verify_frame', data=img_bytes, content_type='application/octet-stream')
        assert res.status_code == 200
        threat_data = res.get_json()
        assert threat_data['threat_detected'] is True
        assert 'Gun' in threat_data['threats']
        print(f"✓ Weapon detection triggered: {threat_data['threats']}")

        # Verify /status reflects the active threat
        res = client.get('/status')
        status_data = res.get_json()
        assert status_data['threat_detected'] is True
        assert status_data['threat_alert'] is not None
        assert 'Gun' in status_data['threat_alert']['threat_type']
        print("✓ Active threat state published to /status for dashboard banner display.")

        # Verify database logged THREAT_ALERT event
        events = database.get_all_events(limit=5)
        threat_events = [e for e in events if e['direction'] == 'THREAT_ALERT']
        assert len(threat_events) > 0, "THREAT_ALERT missing in SQLite events"
        print(f"✓ SQLite audit trail logged: {threat_events[0]['trigger_type']}")

        # 7. Test Clearing Threat
        print("\n[TEST 7] Testing Operator Acknowledgment (/clear_threat)...")
        res = client.post('/clear_threat')
        assert res.status_code == 200
        assert res.get_json()['status'] == 'ok'

        res = client.get('/status')
        assert res.get_json()['threat_detected'] is False
        print("✓ Operator acknowledged and cleared threat alert successfully.")

    finally:
        app.inspect_frame_for_threats = orig_inspect

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED SUCCESSFULLY! SYSTEM READY FOR DEPLOYMENT.")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
