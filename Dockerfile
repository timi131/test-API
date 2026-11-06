FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 REDSMS_DATA=/data
WORKDIR /app
COPY redsms_gui.py redsms_templates.py redsms_test.py ./
RUN mkdir -p /data && chown 65532:65532 /data

USER 65532:65532
EXPOSE 8501
STOPSIGNAL SIGINT
# Порт и адрес приходят из переменных PORT и APP_BASE_PATH, их задаёт REDSMS «Проекты»
CMD ["python", "redsms_gui.py", "--server"]
