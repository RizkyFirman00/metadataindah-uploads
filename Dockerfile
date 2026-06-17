FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY . /app

EXPOSE 8765

CMD ["python", "indah_ui.py", "--host", "0.0.0.0", "--port", "8765"]
