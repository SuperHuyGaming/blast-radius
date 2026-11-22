# The digest is the image the organizers labeled every commit in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# The environment the tests were labeled in.
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock

# sqlglot at the pinned commit, straight from its own repository.
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py /src

COPY selector.py engine.py runner.py patchfmt.py pyscope.py blastkeys.py unitcheck.py /app/
COPY mapsgz/ /app/maps/
RUN gunzip /app/maps/*.gz \
 && PYTHONDONTWRITEBYTECODE= python -m compileall -q /app /src/sqlglot/sqlglot /src/sqlglot/tests \
 && chmod -R a+rX /app /src

ENV BLAST_SRC=/src/sqlglot BLAST_MAPS=/app/maps
CMD ["python", "/app/selector.py"]
