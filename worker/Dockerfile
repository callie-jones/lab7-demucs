# Extend DEMUCS image so we don't rebuild the large model stack
FROM xserrat/facebook-demucs:latest

USER root
WORKDIR /app
RUN pip3 install --no-cache-dir minio redis requests
COPY worker-server.py .

# Base image uses ENTRYPOINT ["/bin/bash", "--login", "-c"]; CMD is passed as the command string
CMD ["python3 worker-server.py"]
