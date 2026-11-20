# Sovereign Mathematical Synthesizer — real backend container.
#
# The engine runs Z3/CVC5 for the safety proof and snarkjs for the settlement
# proof, so the image needs both a Python and a Node toolchain. The Groth16
# proving key and circuit are baked in rather than rebuilt at boot: rebuilding
# them would produce a different key than the one the verifier bytecode encodes,
# and every proof would then be rejected on chain.
# The settlement leg runs its own fuel-core node, so the node binaries are
# fetched in a throwaway stage and copied in. Keeping them out of the final
# layer keeps the runtime image from carrying the download tooling.
FROM debian:bookworm-slim AS fuel

ARG FUEL_CORE_VERSION=0.47.1

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tar \
 && curl -sSL -o /tmp/fuel.tar.gz \
      "https://github.com/FuelLabs/fuel-core/releases/download/v${FUEL_CORE_VERSION}/fuel-core-${FUEL_CORE_VERSION}-x86_64-unknown-linux-gnu.tar.gz" \
 && tar xzf /tmp/fuel.tar.gz -C /tmp \
 && mkdir -p /opt/fuel \
 && cp /tmp/fuel-core-${FUEL_CORE_VERSION}-x86_64-unknown-linux-gnu/fuel-core* /opt/fuel/ \
 && chmod 0755 /opt/fuel/fuel-core*

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

# fuel-core plus its keygen; the settlement leg starts the node itself, so the
# binaries have to be present or that leg can only ever report a fallback.
COPY --from=fuel /opt/fuel/ /usr/local/bin/

COPY synthesizer ./synthesizer
COPY contracts ./contracts
COPY circuits ./circuits
COPY build/zk/settlement.r1cs build/zk/settlement_0.zkey ./build/zk/
# The whole js bundle is needed: generate_witness.js drives the wasm, and the
# wasm alone cannot produce a witness.
COPY build/zk/settlement_js ./build/zk/settlement_js
# The Sway ledger, its compiled artifacts, and the runner script the settlement
# leg shells out to. Without these the leg has a node but nothing to deploy.
COPY sway ./sway
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
