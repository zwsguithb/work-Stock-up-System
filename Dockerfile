FROM python:3.11-slim

WORKDIR /app

# 系统依赖（openpyxl/requests 等纯 Python，无需编译依赖）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 运行时数据目录（SQLite 库存/日志），容器内需可写
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
