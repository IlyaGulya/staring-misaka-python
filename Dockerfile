FROM ghcr.io/prefix-dev/pixi:0.63.2-noble

# Set working directory
WORKDIR /app

# Copy pixi configuration files first for better layer caching
COPY pyproject.toml pixi.lock ./

# Install dependencies using pixi
RUN pixi install --frozen

# Copy the rest of the application
COPY . .

# Use our custom entrypoint via pixi
ENTRYPOINT ["pixi", "run", "docker-entrypoint"]