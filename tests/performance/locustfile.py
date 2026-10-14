"""Load profile for a deployed lab-result-integrity service.

Run against a staging deployment with synthetic data, never production:

    LRI_LOAD_TOKEN=<agent access token with results:screen cases:read> \
    locust -f tests/performance/locustfile.py --host https://<staging host> \
           --users 50 --spawn-rate 5 --run-time 10m --headless

Middleware submits small batches continuously; reviewers poll the queue.
Patient identifiers are random synthetic values so delta checks see history.
"""

from __future__ import annotations

import os
import random
import uuid
from datetime import UTC, datetime, timedelta

from locust import HttpUser, between, task

TOKEN = os.environ.get("LRI_LOAD_TOKEN", "")
ANALYTES = (
    ("2823-3", "mmol/L", 3.4, 5.4),
    ("2951-2", "mmol/L", 133, 147),
    ("2345-7", "mg/dL", 65, 180),
)


def synthetic_result() -> dict[str, object]:
    code, unit, low, high = random.choice(ANALYTES)
    collected = datetime.now(UTC) - timedelta(minutes=random.randint(30, 120))
    ident = uuid.uuid4().hex[:12]
    return {
        "result_id": f"R-{ident}",
        "accession": f"A-{ident}",
        "specimen_id": f"S-{ident}",
        "patient": {
            "patient_id": f"P-{random.randint(1, 5000):05d}",
            "sex": random.choice(["male", "female"]),
            "birth_date": "1968-03-14",
        },
        "analyte_code": code,
        "value": round(random.uniform(low, high), 1),
        "unit": unit,
        "collected_at": collected.isoformat(),
        "resulted_at": (collected + timedelta(minutes=25)).isoformat(),
        "instrument_id": f"CHEM-{random.randint(1, 4):02d}",
        "indices": {"hemolysis": random.choice([None, 10, 30, 80])},
    }


class LabTraffic(HttpUser):
    wait_time = between(0.1, 0.5)

    @task(10)
    def screen(self) -> None:
        self.client.post(
            "/v1/screenings",
            json={"results": [synthetic_result() for _ in range(random.randint(1, 10))]},
            headers={"Authorization": f"Bearer {TOKEN}"},
            name="/v1/screenings",
        )

    @task(3)
    def queue(self) -> None:
        self.client.get(
            "/v1/review-queue?limit=20",
            headers={"Authorization": f"Bearer {TOKEN}"},
            name="/v1/review-queue",
        )
