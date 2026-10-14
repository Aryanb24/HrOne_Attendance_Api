# Employee Attendance & Analytics API

A high-performance FastAPI and MongoDB service implementing employee attendance tracking, manual regularization with immutable audit trails, and reporting analytics pipelines adhering strictly to business rules R1–R10 and the OpenAPI 3.0 contract.

## Tech Stack
* **Python 3.11+**
* **FastAPI** & **Uvicorn**
* **MongoDB 6.0+** (Local Docker or MongoDB Atlas)
* **PyMongo** & **Pydantic v2**

## Local Setup

1. **Clone the repository and create a virtual environment:**
   ```bash
   git clone <repo_url>
   cd <repo_dir>
   python -m venv .venv
   source .venv/bin/activate   # On Windows: .venv\Scripts\activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables:**
   Set `MONGO_URI` and `MONGO_DB` in your environment (defaults to `mongodb://localhost:27017` and `attendance_db`):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

4. **Seed Sample Data (Optional):**
   ```bash
   python sample_seed.py
   ```

5. **Start the API:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   Interactive Swagger docs are available at `http://localhost:8000/docs`.

## Key Capabilities & Business Logic
* **Punch In / Punch Out (R1–R5):** Concurrency-safe punch operations with strict 10-minute grace period (R2), 30-minute minimum overtime threshold (R3), Decimal half-up work hours (R4), half-day threshold (R5), and overnight shift calendar date resolution (R1).
* **Regularization & Audit Trail:** Natural-key addressing `(emp_code, date)` with optimistic concurrency control, automated recomputation of derived metrics, and immutable `history` tracking with epoch millisecond timestamps.
* **Analytics Aggregations:** MongoDB aggregation pipelines for monthly summaries, cross-collection department metrics, standard competition ranking leaderboard (`$rank`), and gap-filled daily trends with 7-day moving averages (`$densify` and `$setWindowFields`).
* **High Performance at Scale:** Covered by targeted compound indexes (`IXSCAN` verified with 0 `COLLSCAN` via `/admin/explain/{endpoint}`).

## Repository Structure
* `app/main.py` - Core application, schemas, endpoints, business helpers, and aggregation pipelines.
* `requirements.txt` - Python runtime dependencies.
* `REVIEW.md` - Defect review and remediation analysis of the starter kit code.
* `DECISIONS.md` - Architectural and design decisions covering indexing, concurrency, ties, headcount, and scalability.
* `README.md` - Documentation and setup instructions.