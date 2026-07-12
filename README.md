# LOD Tracker

A Flask app for logging and reviewing daily LOD (learnings-of-the-day) entries, tagged and filterable by date, title, and tags.

## Stack

- Backend: Python / Flask, served by Gunicorn on port `8090`
- Frontend: server-rendered Jinja templates (`templates/`), styled via `static/style.css`
- Storage: SQLite (`lod.db`)
- Frontend and backend run in one container; nginx listens on port `9080`, proxies all traffic to Flask on `8090`, and exposes `GET /api/health` for health checks.

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Visit `http://localhost:5050`.

## Run the container

```bash
docker build --platform linux/amd64 -t hackathon-app:final .
docker run --rm -p 9080:9080 -p 8090:8090 hackathon-app:final
```

Visit `http://localhost:9080`. The database starts empty (clean-start mode) and is created fresh at `/app/data/lod.db` inside the container.
