FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt

COPY app ./app
# --create-home: the README's in-container test command pip-installs pytest,
# which needs a writable user site under $HOME once we drop root.
RUN useradd --uid 10001 --create-home appuser
USER appuser
ENV PYTHONUNBUFFERED=1
EXPOSE 9630
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9630"]
