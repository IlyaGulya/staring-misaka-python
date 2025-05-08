# Use an official pixi base image. Choose slim for smaller size.
# Pin the version for reproducible builds, e.g., prefixdev/pixi:v0.21.0-slim
# Using 'latest-slim' for simplicity here, but pinning is recommended.
FROM prefixdev/pixi:latest-slim AS builder

WORKDIR /app

# Copy the project definition and lock file
COPY pyproject.toml pixi.lock* ./
# If you have a separate pixi.toml, copy it too:
# COPY pixi.toml ./

# Install dependencies using the lock file for reproducibility
# This creates the .pixi environment with all dependencies
# Using --frozen ensures that pixi.lock is strictly followed.
# If system dependencies are needed (e.g. for psycopg2 if not from conda-forge with all libs),
# they might need to be installed via apt-get BEFORE this step if not handled by conda packages.
# However, conda-forge often provides fully-linked binaries.
# RUN apt-get update && apt-get install -y --no-install-recommends some-system-lib && rm -rf /var/lib/apt/lists/*
RUN pixi install --frozen

# Copy the rest of the application code
COPY src/staring_misaka ./src/staring_misaka

# --- Final Stage ---
# Use a minimal base image for the final stage if desired,
# but for pixi, it's often simpler to use the same pixi-enabled base
# and copy the populated .pixi environment and source code.
FROM prefixdev/pixi:latest-slim

WORKDIR /app

# Copy the populated .pixi environment from the builder stage
COPY --from=builder /app/.pixi ./.pixi

# Copy the application code
COPY --from=builder /app/src ./src
COPY --from=builder /app/pyproject.toml ./pyproject.toml

# Create a non-root user and switch to it
RUN useradd --create-home appuser
USER appuser

# Set the default environment path for pixi
ENV PATH="/app/.pixi/envs/default/bin:$PATH"

# Expose Prometheus port
EXPOSE 8000

# Command to run the application using pixi run and the script defined in pyproject.toml
# `pixi run` automatically activates the correct environment.
CMD ["pixi", "run", "staring-misaka"]