FROM python:3.12-slim
WORKDIR /app
COPY server.py /app/server.py
USER 65534:65534
EXPOSE 8082
CMD ["python", "/app/server.py"]
