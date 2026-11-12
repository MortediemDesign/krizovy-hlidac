FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir tzdata
COPY hlidac.py config.json ./
COPY web ./web
ENV HLIDAC_DATA=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=5m --timeout=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5)" || exit 1
CMD ["python", "hlidac.py"]
