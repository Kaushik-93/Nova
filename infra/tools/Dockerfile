# Shared image for the seed job and the test runner, so a fresh machine needs
# only Docker and make.
FROM python:3.13-slim

WORKDIR /app
COPY tools/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY seed ./seed
COPY tests ./tests

# The tests run unprivileged, and pytest needs somewhere to write its cache.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTEST_ADDOPTS="-p no:cacheprovider"
