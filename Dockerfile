FROM python:3.12-slim

WORKDIR /app

# Prevent Python from writing .pyc files & enable real-time logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install git and clean up apt cache to keep image small
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory /app

# Install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY . .

# Run bot
CMD ["python3", "-m", "bot"]
