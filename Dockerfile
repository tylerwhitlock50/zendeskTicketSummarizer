FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py zendesk_client.py summarizer.py barcodes.py db.py portal_views.py \
     repair_db.py auth.py admin_views.py report_views.py files.py ./
COPY templates/ templates/
COPY static/ static/
COPY migrations/ migrations/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 5000

# IO-bound workload (Zendesk + OpenAI + DB calls); generous timeout for long tickets
ENTRYPOINT ["./entrypoint.sh"]
