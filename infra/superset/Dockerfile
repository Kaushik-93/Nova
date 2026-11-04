FROM apache/superset:6.1.0

USER root
COPY requirements.txt /tmp/nova-requirements.txt
RUN uv pip install --python /app/.venv/bin/python --no-cache -r /tmp/nova-requirements.txt \
    && rm /tmp/nova-requirements.txt

COPY superset_config.py /app/pythonpath/superset_config.py
COPY init.sh bootstrap.py /app/nova/
RUN chmod +x /app/nova/init.sh

USER superset
