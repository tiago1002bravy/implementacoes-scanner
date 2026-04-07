FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY implementacoes_index.json .
COPY implementacoes_aliases.json .

RUN mkdir -p /data

CMD ["python", "-u", "main.py"]
