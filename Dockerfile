# Just pure Python (No Node.js needed anymore!)
FROM python:3.11

# Set up your work directory
WORKDIR /app

# Copy files and install Python dependencies
COPY . .
RUN pip install -r requirements.txt

# Start Gunicorn directly
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --timeout 300"]