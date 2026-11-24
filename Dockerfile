FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy every top-level module so new ones (e.g. atf_log.py) can't be forgotten.
COPY *.py ./
COPY templates/ templates/
COPY static/ static/
COPY migrations/ migrations/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 5000

# IO-bound workload (Zendesk + OpenAI + DB calls); generous timeout for long tickets
ENTRYPOINT ["./entrypoint.sh"]
