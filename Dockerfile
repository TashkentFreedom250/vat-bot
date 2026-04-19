FROM python:3.11-slim

# System deps: libzbar for pyzbar, libGL for opencv
RUN apt-get update && apt-get install -y --no-install-recommends \
    libzbar0 \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python - <<'PY'
from rapidocr import RapidOCR
RapidOCR(params={"Global.log_level": "WARNING"})
print("RapidOCR models ready")
PY

COPY . .

CMD ["python", "-m", "src.bot"]
