FROM ghcr.io/prefix-dev/pixi:0.30.0

COPY . /app
WORKDIR /app
RUN pixi install

CMD ["pixi", "run", "start"]