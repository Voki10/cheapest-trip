# Cheapest Trip — container for Render (free web service) or any Docker host.
# Secrets (TRAVELPAYOUTS_TOKEN, TELEGRAM_BOT_TOKEN, …) come from the host's environment, never from files in the image.
FROM python:3.11-slim

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TZ=MSK-3
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .

# The host passes its port in $PORT (Render: 10000); 7860 when run by hand.
EXPOSE 7860
CMD ["sh", "-c", "exec python -m uvicorn cheaptrip.api:app --host 0.0.0.0 --port ${PORT:-7860} --proxy-headers --forwarded-allow-ips '*' --log-level warning"]
