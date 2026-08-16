FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates
COPY static ./static

# folder data (database + foto) disediakan lewat volume
ENV DB_PATH=/data/pamsimas.db \
    UPLOAD_DIR=/data/uploads \
    FLASK_DEBUG=0

RUN mkdir -p /data/uploads

EXPOSE 5000

CMD ["python", "app.py"]
