FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure output dirs exist
RUN mkdir -p resume/output

CMD ["python", "bot/main.py"]
