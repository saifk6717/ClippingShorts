# Renderer: FFmpeg trim/crop/captions/export with a fixed number of slots.
# Built from the shared backend image (same code, FFmpeg and fonts everywhere): see docker-compose.yml.
FROM backend_image
CMD ["python", "-m", "clippingshorts.workers.renderer"]
HEALTHCHECK --interval=20s --timeout=10s --start-period=60s --retries=3 CMD ["python", "-m", "clippingshorts.workers.healthcheck", "renderer"]
