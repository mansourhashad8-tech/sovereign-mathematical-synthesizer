# Sovereign Mathematical Synthesizer — real backend container.
# Runs the synthesis tools (Z3, CVC5) and the HTTP surface 24/7.
FROM python:3.12-slim

# cvc5 gives an independent second proof alongside Z3. z3 ships via pip.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-eth.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-eth.txt

COPY synthesizer ./synthesizer
COPY web ./web
COPY scripts ./scripts
COPY config ./config 2>/dev/null || true

ENV SYNTH_HOST=0.0.0.0 \
    SYNTH_PORT=8000 \
    SYNTH_ALWAYS_ON=true \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "synthesizer.api:app", "--host", "0.0.0.0", "--port", "8000"]
