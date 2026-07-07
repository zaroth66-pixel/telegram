FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    openjdk-17-jdk \
    wget \
    zip \
    unzip \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN wget https://raw.githubusercontent.com/iBotPeaches/Apktool/master/scripts/linux/apktool \
    -O /usr/local/bin/apktool \
    && chmod +x /usr/local/bin/apktool

RUN wget https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar \
    -O /usr/local/bin/uber-apk-signer.jar

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
