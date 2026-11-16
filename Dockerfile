FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    X2VAULT_DATA_DIR=/data/state \
    X2VAULT_BOOKMARKS_FOLDER="3. Resources/X/inbox" \
    X2VAULT_ASSETS_FOLDER="3. Resources/X/assets"

RUN useradd --create-home --uid 10001 x2vault && mkdir /data && chown x2vault:x2vault /data
WORKDIR /app
COPY x2vault ./x2vault
USER x2vault
VOLUME /data

# Healthy while waiting for login (no credentials yet, same path as ENTRYPOINT), then while
# polls keep succeeding: the monitor file is rewritten on every poll.
HEALTHCHECK --interval=1m --timeout=10s --start-period=10m \
    CMD ["python", "-c", "import glob,os,sys,time; f=glob.glob(os.path.join(os.environ['X2VAULT_DATA_DIR'],'monitor-*.json')); sys.exit(0 if not os.path.exists('/data/credentials.json') or (f and time.time()-max(map(os.path.getmtime,f)) < 900) else 1)"]

# Spall mode: set X2VAULT_SPALL_URL, X2VAULT_SPALL_TOKEN and X2VAULT_X_CLIENT_ID (plus X2VAULT_X_CLIENT_SECRET for confidential clients).
ENTRYPOINT ["python", "-m", "x2vault", "--credentials", "/data/credentials.json"]
CMD ["watch", "--interval", "300"]
