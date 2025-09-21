FROM ghcr.io/prefix-dev/pixi:0.51.0-noble

# Set working directory
WORKDIR /app

# Copy pixi configuration files first for better layer caching
COPY pyproject.toml pixi.lock ./

# Install dependencies using pixi
RUN pixi install --frozen

# Copy the rest of the application
COPY . .

# Make entrypoint script executable
RUN chmod +x docker-entrypoint.sh

# Use our custom entrypoint
ENTRYPOINT ["./docker-entrypoint.sh"]