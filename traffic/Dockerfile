FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir httpx==0.27.2
COPY generator.py .
CMD ["python", "-u", "generator.py"]
