# Multi-stage Dockerfile for the VPN Telegram bot
#
# This Dockerfile builds a minimal runtime image for the Telegram bot.
# A separate builder stage is used to install dependencies, and only
# the necessary application files are copied into the final image.

# ------ Builder stage: install Python and dependencies ------
FROM python:3.11-slim AS builder

# Create working directory
WORKDIR /app

# Disable Python cache generation and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies required for building Python packages.
# Pillow needs libjpeg-dev and zlib1g-dev for JPEG/PNG support.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libjpeg-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies. Upgrading pip
# ensures the latest packaging features, and --prefix installs to
# /usr/local so that we can copy them later.
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install --no-cache-dir --prefix=/usr/local -r requirements.txt

# Copy the entire application source. This includes bot_main.py,
# the handlers package, and all helper modules. We copy at the end
# to avoid invalidating the dependency layer when only code changes.
COPY . /app

# ------ Runtime stage: create lean image with installed deps ------
FROM python:3.11-slim
WORKDIR /app

# Disable Python cache generation and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Copy installed Python packages from builder stage
COPY --from=builder /usr/local /usr/local

# Copy application code from the builder stage
COPY --from=builder /app /app

# Create a non-root user for running the bot and switch to it
RUN useradd --create-home botuser
USER botuser

# Default command to run the Telegram bot
CMD ["python", "bot_main.py"]