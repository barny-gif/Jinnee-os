FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl git nodejs npm && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["bash", "-c", "python core/jinnee.py & python dashboard/app.py"]
