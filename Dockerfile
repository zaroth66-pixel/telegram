FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all files
COPY . .

# Create data directory
RUN mkdir -p /app/data /app/data/sessions /app/data/images

# Expose port (Railway uses PORT env)
EXPOSE 8080

# Run the bot
CMD ["python3", "bot.py"]
