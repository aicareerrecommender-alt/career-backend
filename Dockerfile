# Use a Python image that includes Node.js 
FROM nikolaik/python-nodejs:python3.11-nodejs22

# Install Chromium and system dependencies 
RUN apt-get update && apt-get install -y \
    chromium \
    fonts-liberation \
    libappindicator3-1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libnspr4 \
    libnss3 \
    lsb-release \
    xdg-utils \
    --no-install-recommends

# Install OpenClaw globally 
RUN npm install -g openclaw@latest

# Set up your work directory 
WORKDIR /app

# COPY . . to copy all files to the /app folder 
COPY . .

# Install Python dependencies 
RUN pip install -r requirements.txt

# FIX: Use 'npx' to ensure the shell finds the openclaw binary
# and use gunicorn for production stability.
CMD ["sh", "-c", "npx openclaw gateway start --port 18789 & sleep 10 && gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --timeout 300"]