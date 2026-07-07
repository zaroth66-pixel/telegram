FROM python:3.11

WORKDIR /app

# Install dependencies
RUN apt-get update && apt-get install -y \
    default-jdk \
    wget \
    zip \
    unzip \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install apktool wrapper + jar
RUN wget -q https://raw.githubusercontent.com/iBotPeaches/Apktool/master/scripts/linux/apktool \
    -O /usr/local/bin/apktool \
    && chmod +x /usr/local/bin/apktool \
    && wget -q https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar \
    -O /usr/local/share/apktool.jar

# Install uber-apk-signer
RUN wget -q https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar \
    -O /usr/local/bin/uber-apk-signer.jar

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Verify installations
RUN java -version && apktool --version

CMD ["python", "bot.py"]
