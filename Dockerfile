FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py zendesk_client.py summarizer.py visual_client.py rtm_db.py rtm_views.py rtm_reports.py ./
COPY templates/ templates/
COPY static/ static/
COPY migrations/ migrations/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 5000

# IO-bound workload (Zendesk + OpenAI + DB calls); generous timeout for long tickets
ENTRYPOINT ["./entrypoint.sh"]
