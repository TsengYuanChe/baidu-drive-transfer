FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY router.py .
COPY main.py .
COPY baidu_share_transfer.py .
COPY google_drive_upload.py .

EXPOSE 8005

CMD ["python", "-m", "uvicorn", "router:app", "--host", "0.0.0.0", "--port", "8005"]