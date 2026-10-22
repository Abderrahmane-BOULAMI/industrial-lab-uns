FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY labuns ./labuns
COPY config ./config
ENTRYPOINT ["python", "-m", "labuns"]
