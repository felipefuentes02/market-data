# 1. Definimos la imagen base oficial y ligera de Python
FROM python:3.12-slim

# 2. Variables de entorno analíticas para optimizar el rendimiento
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 3. Directorio de trabajo dentro del contenedor
WORKDIR /app

# 4. Instalación de dependencias del sistema operativo para PostgreSQL
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 5. Copiar requerimientos e instalarlos de forma aislada
# (Ajustado al nombre que generaste)
COPY requerimientos.txt /app/
RUN pip install --upgrade pip
RUN pip install -r requerimientos.txt

# 6. Copiar el resto del ecosistema del proyecto
COPY . /app/

# INYECTAR ESTO: Recopilar archivos estáticos automáticamente sin pedir confirmación
RUN python manage.py collectstatic --noinput

# 7. Exponer el puerto estándar
EXPOSE 8000

# 8. Motor de arranque en producción con inyección previa de migración
CMD python manage.py migrate && gunicorn configuracion.wsgi:application --bind 0.0.0.0:8000