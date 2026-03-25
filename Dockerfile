# Use a Python image that includes Node.js (OpenClaw needs both)
FROM nikolaik/python-nodejs:python3.11-nodejs22

# Install Chromium for the browser tool
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

# Install OpenClaw
RUN npm install -g openclaw@latest

# Set up your work directory
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt

# Start the OpenClaw Gateway and your Python app
CMD ["sh", "-c", "openclaw gateway start & python app.py"]