# Use an official Python runtime
FROM python:3.10-slim

# Install ffmpeg (required by spotDL)
RUN apt-get update && apt-get install -y ffmpeg

# Set up the working directory inside the server
WORKDIR /app

# Copy the requirements file and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all your Python files into the server
COPY . .

# Command to run your FastAPI app on Render's required port
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
