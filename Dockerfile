FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

ENV DATA_DIR=/data

EXPOSE 8080

CMD ["python", "web_app.py"]
