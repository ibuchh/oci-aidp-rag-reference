ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY client ./client
COPY app.py .
COPY .streamlit ./.streamlit
RUN chmod -R a=rX /app/client /app/.streamlit \
    && chmod 0444 /app/app.py /app/requirements.txt
USER 10001:10001
CMD ["python", "-m", "streamlit", "run", "app.py", "--server.address=127.0.0.1", "--server.port=8501", "--server.headless=true"]
