# FastAPI Student Attendance Backend

## Features
- Student and course creation
- Attendance sessions with a configured GPS centre and radius
- Signed, expiring QR tokens (HMAC-SHA256)
- GPS distance validation using the Haversine formula
- Duplicate attendance prevention at the database level
- Session attendance and student attendance history

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Set a strong QR secret:

```bash
export QR_SECRET="use-a-long-random-secret-value-here"
# PowerShell:
# $env:QR_SECRET="use-a-long-random-secret-value-here"
```

Start the API:

```bash
uvicorn main:app --reload
```

Open http://127.0.0.1:8000/docs

## Frontend (React + TypeScript + Vite)

The frontend interface is located in the `frontend` folder.

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 to access:
- **Lecturer / Display View**: Manage courses, students, generate attendance sessions with classroom GPS coords, project live signed QR codes, and view live attendees.
- **Student Check-In View**: Mobile-ready interface with GPS auto-detection, live camera QR scanning, check-in submission, and past attendance history.


## Example workflow

1. Create a student:

```http
POST /students
Content-Type: application/json

{"student_code":"STU001","full_name":"Sample Student"}
```

2. Create a course:

```http
POST /courses
Content-Type: application/json

{"code":"CS101","name":"Introduction to Computing"}
```

3. Create an attendance session. Use the classroom's latitude and longitude:

```http
POST /sessions
Content-Type: application/json

{
  "course_id": 1,
  "latitude": 13.3611,
  "longitude": 103.8590,
  "radius_m": 100,
  "duration_minutes": 15
}
```

4. Retrieve the QR token:

```http
GET /sessions/1/qr
```

The frontend should encode the returned `qr_token` string as the QR code content.

5. After scanning, send the student's ID and device coordinates:

```http
POST /attendance/scan
Content-Type: application/json

{
  "student_id": 1,
  "qr_token": "TOKEN_FROM_STEP_4",
  "latitude": 13.3612,
  "longitude": 103.8591
}
```

6. View attendance:

```http
GET /sessions/1/attendance
GET /students/1/attendance
```

## Important security notes

- This is an authentication-free development scaffold, as requested. Do not expose session creation or student creation publicly in production.
- GPS coordinates supplied by a browser or phone can be spoofed. The radius check is useful for ordinary location validation, but it is not strong proof of physical presence. Consider device attestation, anti-spoofing signals, Wi-Fi/Bluetooth checks, or supervised checks for higher-assurance deployments.
- The QR token is signed and expires with its session. The `/sessions/{id}/qr` endpoint returns the same session token during that session; it is not a rotating QR code.
- Use HTTPS in deployment, keep `QR_SECRET` private, and use PostgreSQL for production/concurrent workloads.
- Add rate limiting and audit logging before public deployment.
