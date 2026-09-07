# Use NVIDIA CUDA base image for GPU support
FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Set work directory
WORKDIR /app

# Install system dependencies
# - python3-pip / python3-dev: Core Python
# - build-essential: For compiling libs
# - libgl1-mesa-glx: For OpenCV ( PaddleOCR dependency)
# - poppler-utils: For pdf2image / fitz
# - git: For installing git-based pip packages
RUN apt-get update && apt-get install -y \
    python3.10 \
    python3-pip \
    python3-dev \
    build-essential \
    libgl1-mesa-glx \
    libglib2.0-0 \
    poppler-utils \
    git \
    && rm -rf /var/lib/apt/lists/*

# Alias python3 to python
RUN ln -s /usr/bin/python3.10 /usr/bin/python

# Copy requirements first to leverage caching
COPY requirements.txt .

# Install Python dependencies
# Upgrade pip first
RUN python -m pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Manually ensure paddlepaddle-gpu is installed correctly (often requires specific index)
# RUN pip install paddlepaddle-gpu==2.6.0 -f https://www.paddlepaddle.org.cn/whl/linux/mkl/avx/stable.html

# Copy application code
COPY smart_po_extraction.py .
COPY run_ocr_tool.py .
COPY setup_knowledge_base.py .
COPY knowledge_base.db . 

# Create directories
RUN mkdir -p test_data

# Set entrypoint (Default to help, or could be a server)
CMD ["python", "azure_worker.py"]
