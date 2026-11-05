FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY deskbot ./deskbot
COPY config ./config
RUN useradd -m desk && mkdir -p var && chown desk var
USER desk
ENTRYPOINT ["python", "-m", "deskbot.cli"]
CMD ["run", "--role", "all"]
