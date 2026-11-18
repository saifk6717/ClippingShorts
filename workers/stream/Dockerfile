# Stream worker: one isolated process per live stream (FFmpeg capture, chat, audio analysis, rolling buffer, local detection) plus clip extraction and storage housekeeping.
# Built from the shared backend image (same code, FFmpeg and fonts everywhere): see docker-compose.yml.
FROM backend_image
CMD ["python", "-m", "clippingshorts.workers.stream_supervisor"]
HEALTHCHECK --interval=20s --timeout=10s --start-period=60s --retries=3 CMD ["python", "-m", "clippingshorts.workers.healthcheck", "stream"]
