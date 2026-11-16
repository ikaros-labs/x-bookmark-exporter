FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    X_BOOKMARK_DATA_DIR=/data/state \
    X_BOOKMARK_FOLDER="3. Resources/X/inbox" \
    X_BOOKMARK_ASSETS_FOLDER="3. Resources/X/assets"

RUN useradd --create-home --uid 10001 xbm && mkdir /data && chown xbm:xbm /data
WORKDIR /app
COPY x_bookmarks ./x_bookmarks
USER xbm
VOLUME /data

# Healthy while polls keep succeeding: the monitor file is rewritten on every poll.
HEALTHCHECK --interval=5m --timeout=10s --start-period=10m \
    CMD ["python", "-c", "import glob,os,sys,time; f=glob.glob(os.path.join(os.environ['X_BOOKMARK_DATA_DIR'],'monitor-*.json')); sys.exit(0 if f and time.time()-max(map(os.path.getmtime,f)) < 900 else 1)"]

# API mode: set X_VAULT_API_URL, X_VAULT_API_TOKEN and X_CLIENT_ID (plus X_CLIENT_SECRET for confidential clients).
ENTRYPOINT ["python", "-m", "x_bookmarks", "--credentials", "/data/credentials.json"]
CMD ["watch", "--interval", "300"]
