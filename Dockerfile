FROM public.ecr.aws/docker/library/python:trixie

WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements-test.txt .
RUN pip install --no-cache-dir -r requirements-test.txt

COPY custom_components/ ./custom_components/
COPY tests/ ./tests/
COPY pytest.ini .

CMD ["pytest", "--cov=custom_components/wholesale_ev_schedule", "--cov-report=term-missing"]
