# HROne - Employee Attendance & Analytics API

FastAPI and MongoDB backend service for managing employee attendance, manual regularization with audit trails, and complex analytics reporting.

## Overview
All requirements, endpoints, business rules (R1–R10), and aggregation pipelines from `openapi.yaml` and `PROBLEM_STATEMENT.docx` are fully implemented with zero missing components.

### Core Features
- **Attendance Mechanics (R1–R5):** Punch-in and punch-out with race safety, IST timezone attribution, overnight shift calculations across midnight, late minutes calculation with 10-minute grace period, overtime thresholding ($\ge 30$ mins), and half-day determinations.
- **Regularization & Audit Trail:** `PATCH /attendance/{emp_code}/{date}` with optimistic concurrency locking to prevent losing audit histories, diffing modified fields, and recomputing derived statistics.
- **Advanced Aggregation Analytics:**
  - `GET /analytics/employees/{emp_code}/monthly`: Monthly attendance summary with mid-month joiner working days.
  - `GET /analytics/departments/summary`: Department monthly summary counting employees with zero logs.
  - `GET /analytics/leaderboard/late`: Standard competition ranking (`$rank`: 1, 2, 2, 4) with ties and limit cutoff.
  - `GET /analytics/departments/{department}/trend`: Daily timeline with gap-filling (`$densify`) and 7-day moving averages (`$setWindowFields`).
- **Explain Endpoint (`GET /admin/explain/{endpoint}`):** Runs `.explain("executionStats")` to verify index scans (`IXSCAN`) and zero collection scans (`COLLSCAN`).

## Local Setup & Running

### Requirements
- Python 3.11+
- MongoDB 6.0+ (Tested against MongoDB 7.0)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Seed sample data (optional, connects to localhost:27017/attendance_db by default)
python sample_seed.py

# 3. Start API server
uvicorn app.main:app --port 8000
```

Environment variables `MONGO_URI` and `MONGO_DB` are dynamically read if provided (system environment variables take precedence). All indexes are idempotently created at startup.

## Submission Files
- `app/main.py`: Complete application code and pipelines.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Detailed defect analysis and fixes from the initial starter application.
- `DECISIONS.md`: Concise architectural rationale and design choices.
- `README.md`: Project documentation and setup instructions.
