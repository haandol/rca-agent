import pytest


@pytest.mark.asyncio
async def test_ingest_sensor_data(client):
    payload = {
        "readings": [
            {"patient_id": "P001", "reading_type": "heart_rate", "value": 75.0, "unit": "bpm"},
            {"patient_id": "P001", "reading_type": "temperature", "value": 36.5, "unit": "°C"},
        ]
    }
    resp = await client.post("/sensors/data", json=payload)
    assert resp.status_code == 200

    data = resp.json()
    assert len(data) == 2
    assert data[0]["patient_id"] == "P001"
    assert data[0]["is_abnormal"] is False


@pytest.mark.asyncio
async def test_abnormal_detection(client):
    payload = {
        "readings": [
            {"patient_id": "P002", "reading_type": "heart_rate", "value": 150.0, "unit": "bpm"},
            {"patient_id": "P002", "reading_type": "spo2", "value": 88.0, "unit": "%"},
            {"patient_id": "P002", "reading_type": "temperature", "value": 39.5, "unit": "°C"},
        ]
    }
    resp = await client.post("/sensors/data", json=payload)
    assert resp.status_code == 200

    data = resp.json()
    assert all(r["is_abnormal"] is True for r in data)


@pytest.mark.asyncio
async def test_get_patient_vitals(client):
    payload = {
        "readings": [
            {"patient_id": "P003", "reading_type": "heart_rate", "value": 80.0, "unit": "bpm"},
        ]
    }
    await client.post("/sensors/data", json=payload)

    resp = await client.get("/patients/P003/vitals")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_get_alerts(client):
    payload = {
        "readings": [
            {"patient_id": "P004", "reading_type": "heart_rate", "value": 40.0, "unit": "bpm"},
            {"patient_id": "P004", "reading_type": "heart_rate", "value": 75.0, "unit": "bpm"},
        ]
    }
    await client.post("/sensors/data", json=payload)

    resp = await client.get("/alerts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["is_abnormal"] is True


@pytest.mark.asyncio
async def test_invalid_payload(client):
    resp = await client.post("/sensors/data", json={"readings": [{"invalid": True}]})
    assert resp.status_code == 422
