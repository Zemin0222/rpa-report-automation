FROM python:3.12-slim

ARG APT_MIRROR=mirrors.aliyun.com

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    RPA_HEADLESS=true

WORKDIR /app

# Switch Debian sources to fast mirror (override via --build-arg APT_MIRROR=deb.debian.org if needed)
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
    && echo 'Acquire::http::Timeout "30";' >> /etc/apt/apt.conf.d/80-retries \
    && apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-cjk \
    tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple

COPY requirements.txt .
RUN pip install --no-cache-dir --retries 5 --timeout 30 -i "${PIP_INDEX_URL}" -r requirements.txt

RUN playwright install-deps chromium \
    && playwright install chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

CMD ["python", "main.py", "schedule"]
