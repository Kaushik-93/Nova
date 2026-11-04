FROM python:3.13-slim

WORKDIR /app
COPY requirements-dev.txt requirements.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements-dev.txt
COPY . ./agent

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTEST_ADDOPTS="-p no:cacheprovider"
