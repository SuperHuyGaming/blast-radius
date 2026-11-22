# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
ARG BASE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The base image has no git and apt mirrors are not an allowed source, so
# dulwich (PyPI) fetches the pinned commit from sqlglot's own repository.
FROM ${BASE} AS src
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN pip install --no-cache-dir --root-user-action=ignore dulwich==1.2.21
COPY fetch_sqlglot.py /fetch_sqlglot.py
WORKDIR /src
RUN python /fetch_sqlglot.py "$SQLGLOT_SHA" && rm -rf .git

FROM ${BASE}
# The run's disk is read-only, so bytecode is compiled here and never written.
ENV PYTHONDONTWRITEBYTECODE=1
# What test_execute_tpcds imports, at the versions the tests were labeled with:
# the selector runs part of that test against the patch (subrun.py).
RUN pip install --no-cache-dir --root-user-action=ignore duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1 python-dateutil==4.0.0 tzdata==2026.6
# What the rest of the suite imports, so a commit's selected tests can run under its patch with
# pytest (subrun.SuiteRunner). mypy_extensions changes sqlglot.helper's decorators when present.
RUN pip install --no-cache-dir --root-user-action=ignore --no-deps pytest==9.2.0 iniconfig==2.3.1 pluggy==1.7.0 packaging==26.3 Pygments==2.21.0 mypy_extensions==1.2.0 typing_extensions==4.16.0 pytz==2026.6
# The tests' process pools size themselves from this, as in the labeling runs: the run has 2 cores
ENV PYTHON_CPU_COUNT=2
COPY --from=src /src /app/sqlglot-src
COPY selector.py blastsel.py clsmap.py subrun.py /app/
COPY maps /app/maps
# The fixture rows TestExecutor.setUpClass parses, so a tpcds run skips that parse (subrun.save_rows)
RUN SKIP_INTEGRATION=0 python /app/subrun.py /app/sqlglot-src
# pytest caches its rewritten test modules beside them, so the runs read them instead of rewriting
RUN python -m compileall -q /app \
    && cd /app/sqlglot-src && PYTHONDONTWRITEBYTECODE= SKIP_INTEGRATION=0 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider --collect-only --continue-on-collection-errors tests | grep -E -A3 "^(ERROR|=)" | tail -12 \
    && chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
