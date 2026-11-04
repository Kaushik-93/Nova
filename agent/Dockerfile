FROM python:3.13-slim

WORKDIR /app
# requirements.lock is generated from requirements.txt by `make lock`.
COPY requirements.lock ./
RUN pip install --no-cache-dir --root-user-action=ignore uv==0.13.7 \
    && uv pip install --system --no-cache -r requirements.lock \
    && uv pip install --system --no-cache pytest==9.2.0
COPY . ./agent

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTEST_ADDOPTS="-p no:cacheprovider"
