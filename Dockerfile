# Same base image the organizers labeled with (its Python and SQLite decide test behavior).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV SKIP_INTEGRATION=0 \
    PYTHONUNBUFFERED=1 \
    BR_SRC=/app/sqlglot

WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

# sqlglot at the pinned commit, straight from its repository.
RUN python -c "import urllib.request,tarfile,io,os; \
data=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/app', filter='data'); \
os.rename('/app/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa','/app/sqlglot')"

COPY br /app/br
COPY data /app/data
# Build-time work is free: index the coverage map and precompile bytecode.
RUN python /app/br/index_build.py /app/sqlglot /app/data/map.json.gz /app/br/index.pkl /app/data/units.json.gz  && cd /app/sqlglot && python /app/br/exec_cache.py /app/data/exec_rows.pkl \
 && python -m compileall -q /app/sqlglot/sqlglot /app/sqlglot/tests /app/br \
 && chmod -R a+rX /app

CMD ["python", "/app/br/main.py"]
