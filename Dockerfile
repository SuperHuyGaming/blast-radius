# The digest is the image the commits were labeled in (from the starter kit).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot at the pinned commit: the selector reads the original source to
# apply each patch and look at the changed statements.
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app/src \
 && tar -xzf /tmp/sqlglot.tar.gz -C /app/src --strip-components=1 \
      sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa/sqlglot \
      sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa/tests \
 && rm /tmp/sqlglot.tar.gz

COPY selector.py map.json.gz /app/
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
