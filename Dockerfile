# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
# Default output directory inside the container
ENV OUTPUT_DIR /app/data
# Default max pages to crawl
ENV MAX_PAGES 50

# Set the working directory in the container
WORKDIR /app

# Install system dependencies if needed (e.g., for Pillow if you uncomment image size checks)
# RUN apt-get update && apt-get install -y --no-install-recommends some-package && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
# Consider using --no-cache-dir to reduce layer size
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Make port 5000 available to the world outside this container (Flask default port)
EXPOSE 5000

# Define the command to run the application
# Use gunicorn for production Flask deployment (install via requirements.txt if you use it)
# CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:5000", "app:app"]
# For simplicity, just run the Flask dev server (NOT recommended for production)
CMD ["python", "app.py"]

# Or, if you primarily want to use the CLI:
# ENTRYPOINT ["python", "cli.py"]
# Example CLI Usage: docker run image-scraper https://example.com --output /app/data/example_com