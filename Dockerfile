FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py zendesk_client.py summarizer.py ./
COPY templates/ templates/
COPY static/ static/

EXPOSE 5000

# IO-bound workload (Zendesk + OpenAI calls); generous timeout for long tickets
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "120", "app:app"]
