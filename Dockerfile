# Sovereign Mathematical Synthesizer — real backend container.
#
# The engine runs Z3/CVC5 for the safety proof and snarkjs for the settlement
# proof, so the image needs both a Python and a Node toolchain. The Groth16
# proving key and circuit are baked in rather than rebuilt at boot: rebuilding
# them would produce a different key than the one the verifier bytecode encodes,
# and every proof would then be rejected on chain.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-eth.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-eth.txt

# Only the proving stack: circom/snarkjs/solc. The full circomlib source tree is
# needed because the circuit includes its comparator and Poseidon gadgets.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

COPY synthesizer ./synthesizer
COPY contracts ./contracts
COPY circuits ./circuits
COPY build/zk/settlement.r1cs build/zk/settlement_0.zkey ./build/zk/
# The whole js bundle is needed: generate_witness.js drives the wasm, and the
# wasm alone cannot produce a witness.
COPY build/zk/settlement_js ./build/zk/settlement_js
COPY web ./web
COPY scripts ./scripts
COPY config ./config
COPY api ./api

ENV SYNTH_HOST=0.0.0.0 \
    SYNTH_PORT=8000 \
    SYNTH_ALWAYS_ON=true \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "synthesizer.api:app", "--host", "0.0.0.0", "--port", "8000"]
