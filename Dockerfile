FROM python:3.12-alpine

COPY pyproject.toml /app/
WORKDIR /app

RUN python -m pip install .

COPY . .

CMD ["python", "main.py"]